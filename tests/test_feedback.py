"""Feedback store, trace lookup and the bot's message map (no network, synthetic only)."""

import json
from pathlib import Path

import duckdb
import pytest

from scrollback.feedback import (
    MessageMap,
    feedback_path,
    find_trace,
    latest_by_trace,
    load_feedback,
    record_feedback,
    trace_files,
)


def _trace(traces: Path, trace_id: str, day: str, question: str = "q") -> None:
    traces.mkdir(parents=True, exist_ok=True)
    with (traces / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps({"trace_id": trace_id, "question": question}) + "\n")


def test_record_feedback_writes_jsonl_and_table(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    fb, err = record_feedback(tmp_path / "traces", db, trace_id="t1", rating="bad", note=" off ")
    assert err is None and fb.note == "off"
    assert load_feedback(tmp_path / "traces") == [fb]
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT trace_id, rating, note FROM agent_feedback").fetchall() == [
        ("t1", "bad", "off")
    ]
    con.close()


def test_record_feedback_locked_db_still_writes_jsonl(tmp_path: Path) -> None:
    db = tmp_path / "wh.duckdb"
    db.write_text("not a duckdb file")
    fb, err = record_feedback(tmp_path / "t", db, trace_id="t1", rating="good")
    assert err and "agent_feedback not written" in err
    assert load_feedback(tmp_path / "t") == [fb]


def test_record_feedback_rejects_other_ratings(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_feedback(tmp_path, None, trace_id="t", rating="meh")  # type: ignore[arg-type]


def test_latest_rating_wins(tmp_path: Path) -> None:
    t = tmp_path / "traces"
    record_feedback(t, None, trace_id="a", rating="good")
    record_feedback(t, None, trace_id="a", rating="bad", note="changed my mind")
    latest = latest_by_trace(load_feedback(t))
    assert latest["a"].rating == "bad"


def test_find_trace_and_trace_files_skip_feedback(tmp_path: Path) -> None:
    t = tmp_path / "traces"
    _trace(t, "20261121T235959-aaaaaaaa", "2026-11-21")
    _trace(t, "20261122T010101-bbbbbbbb", "2026-11-22", question="second")
    record_feedback(t, None, trace_id="x", rating="bad")
    MessageMap(t).add(1, 2, "x")
    assert [p.name for p in trace_files(t)] == ["2026-11-21.jsonl", "2026-11-22.jsonl"]
    assert feedback_path(t).exists()
    found = find_trace(t, "20261122T010101-bbbbbbbb")
    assert found and found["question"] == "second"
    assert find_trace(t, "nope") is None


def test_message_map_survives_restart(tmp_path: Path) -> None:
    m = MessageMap(tmp_path)
    m.add(10, 100, "t1")
    m.add(10, 101, "t1")
    m.add(10, 200, "t2")
    m.add(99, 300, "t3")
    again = MessageMap(tmp_path)
    assert again.trace_for(10, 101) == "t1"
    assert again.last_trace(10) == "t2" and again.last_trace(99) == "t3"
    assert again.trace_for(10, 999) is None and again.last_trace(5) is None
