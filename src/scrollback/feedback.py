"""Feedback on agent answers, and the bot's message -> trace map (DESIGN §8).

Everything lives under ``paths.traces_dir`` (git-ignored), next to the trace JSONL files:

- ``feedback/feedback.jsonl``: one line per ``/feedback`` (the source of truth). Each line is
  also written, best effort, to an ``agent_feedback`` table next to ``agent_traces``, so the two
  join on ``trace_id``. As with traces, a locked warehouse only skips the table row.
- ``bot/messages.jsonl``: which bot message carried which trace, so ``/feedback`` sent as a
  reply (or just after an answer) finds its trace after a restart too.

Subfolders keep these lines out of ``traces/*.jsonl``, which holds traces only.
Privacy: feedback holds the owner's note and a trace ID; no chat ID is stored with it.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

Rating = Literal["good", "bad"]
RATINGS: tuple[Rating, ...] = ("good", "bad")
TRACE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
TRACE_ID_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T\d{6}-[0-9a-f]{8}$")

AGENT_FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS agent_feedback (
    feedback_id VARCHAR PRIMARY KEY,
    trace_id VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    rating VARCHAR NOT NULL,
    note VARCHAR,
    source VARCHAR NOT NULL
)
"""


def feedback_path(traces_dir: Path) -> Path:
    return traces_dir / "feedback" / "feedback.jsonl"


def messages_path(traces_dir: Path) -> Path:
    return traces_dir / "bot" / "messages.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


# --------------------------------------------------------------------------- feedback


@dataclass(frozen=True)
class Feedback:
    feedback_id: str
    trace_id: str
    created_at: str
    rating: Rating
    note: str
    source: str


def record_feedback(
    traces_dir: Path,
    db_path: Path | None,
    *,
    trace_id: str,
    rating: Rating,
    note: str = "",
    source: str = "telegram",
) -> tuple[Feedback, str | None]:
    """Append the feedback line, then the table row. Returns (feedback, db error or None)."""
    if rating not in RATINGS:
        raise ValueError(f"rating must be good or bad, got {rating!r}")
    fb = Feedback(
        feedback_id=uuid.uuid4().hex[:12],
        trace_id=trace_id,
        created_at=_now(),
        rating=rating,
        note=note.strip(),
        source=source,
    )
    _append(feedback_path(traces_dir), asdict(fb))
    if db_path is None:
        return fb, None
    try:
        con = duckdb.connect(str(db_path))
        try:
            con.execute(AGENT_FEEDBACK_DDL)
            con.execute(
                "INSERT INTO agent_feedback VALUES (?, ?, ?, ?, ?, ?)",
                [fb.feedback_id, fb.trace_id, fb.created_at, fb.rating, fb.note or None, source],
            )
        finally:
            con.close()
    except duckdb.Error as exc:  # e.g. the warehouse is locked by another process
        return fb, f"agent_feedback not written: {type(exc).__name__}"
    return fb, None


def load_feedback(traces_dir: Path) -> list[Feedback]:
    return [Feedback(**r) for r in _read(feedback_path(traces_dir))]


def latest_by_trace(feedback: list[Feedback]) -> dict[str, Feedback]:
    """The most recent rating per trace (a later /feedback replaces an earlier one)."""
    latest: dict[str, Feedback] = {}
    for fb in sorted(feedback, key=lambda f: f.created_at):
        latest[fb.trace_id] = fb
    return latest


# --------------------------------------------------------------------------- traces


def trace_files(traces_dir: Path) -> list[Path]:
    if not traces_dir.exists():
        return []
    return sorted(p for p in traces_dir.iterdir() if TRACE_FILE_RE.match(p.name))


def find_trace(traces_dir: Path, trace_id: str) -> dict[str, Any] | None:
    """The trace record with this ID from ``traces/YYYY-MM-DD.jsonl`` (its own day first)."""
    files = trace_files(traces_dir)
    m = TRACE_ID_RE.match(trace_id)
    if m:
        day = traces_dir / f"{m.group(1)}-{m.group(2)}-{m.group(3)}.jsonl"
        files = [day, *(f for f in files if f != day)] if day.exists() else files
    for path in files:
        for record in _read(path):
            if record.get("trace_id") == trace_id:
                return record
    return None


# --------------------------------------------------------------------------- message map


class MessageMap:
    """Bot message -> trace ID, persisted as JSONL so it survives restarts."""

    def __init__(self, traces_dir: Path) -> None:
        self.path = messages_path(traces_dir)
        self._by_message: dict[tuple[int, int], str] = {}
        self._last: dict[int, str] = {}
        self._note_prompts: dict[tuple[int, int], str] = {}
        for r in _read(self.path):
            chat, mid, trace = int(r["chat_id"]), int(r["message_id"]), str(r["trace_id"])
            if r.get("kind") == "note_prompt":
                self._note_prompts[(chat, mid)] = trace
            else:
                self._remember(chat, mid, trace)

    def _remember(self, chat_id: int, message_id: int, trace_id: str) -> None:
        self._by_message[(chat_id, message_id)] = trace_id
        self._last[chat_id] = trace_id

    def add(self, chat_id: int, message_id: int, trace_id: str) -> None:
        self._remember(chat_id, message_id, trace_id)
        _append(
            self.path,
            {"chat_id": chat_id, "message_id": message_id, "trace_id": trace_id, "at": _now()},
        )

    def add_note_prompt(self, chat_id: int, message_id: int, trace_id: str) -> None:
        """A "what was wrong?" message after 👎: a reply to it is that answer's note."""
        self._note_prompts[(chat_id, message_id)] = trace_id
        _append(
            self.path,
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "kind": "note_prompt",
                "at": _now(),
            },
        )

    def note_prompt_trace(self, chat_id: int, message_id: int) -> str | None:
        return self._note_prompts.get((chat_id, message_id))

    def trace_for(self, chat_id: int, message_id: int) -> str | None:
        return self._by_message.get((chat_id, message_id))

    def last_trace(self, chat_id: int) -> str | None:
        return self._last.get(chat_id)
