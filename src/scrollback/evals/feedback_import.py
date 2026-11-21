"""Bad bot feedback -> candidate eval questions for review (DESIGN §8).

``scrollback eval import-feedback`` reads ``traces/feedback/feedback.jsonl`` and the trace of
every answer whose latest rating is ``bad``, and appends one candidate per trace to
``eval/candidates/feedback.yaml``. It never touches ``questions.yaml``: a candidate becomes an
eval question only when the owner copies it there by hand, picks a synthetic ``today`` anchor
and writes the gold SQL from the definitions (DESIGN §7.1 writing rules).

The candidates file is **git-ignored**: it holds real questions and the agent's answers on
real data. Re-running adds only traces not already in the file, and keeps the owner's edits
to existing entries (YAML comments inside entries are not kept).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scrollback.evals.questions import QuestionSet
from scrollback.feedback import Feedback, find_trace, latest_by_trace, load_feedback

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CANDIDATES_PATH = REPO_ROOT / "eval" / "candidates" / "feedback.yaml"
TODO = "TODO"

HEADER = """\
# Candidate eval questions from bad bot feedback (`scrollback eval import-feedback`).
# GIT-IGNORED: real questions and answers on the owner's data. Never commit this file.
#
# Review each entry (set review: accepted | rejected). For an accepted one, copy `proposed`
# into eval/questions.yaml by hand: pick a category, difficulty, split and a synthetic
# `today` anchor, and write gold_sql from knowledge/metrics.yml and the glossary, not from
# the answer below. Then run `scrollback eval check-gold --update` and review the diff.
# Re-running the import adds new traces only; edits to existing entries are kept.
"""


@dataclass
class ImportSummary:
    feedback: int  # feedback lines read
    bad: int  # traces whose latest rating is bad
    added: int
    already_in_file: int
    missing_traces: int  # bad feedback whose trace was not found (not added; retried next run)
    now_good: int  # entries in the file whose latest rating has since become good
    matches_eval_set: int  # added candidates whose question is already in questions.yaml
    path: Path


def normalise(question: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", question.lower()))


def candidate(fb: Feedback, trace: dict[str, Any], qset: QuestionSet | None) -> dict[str, Any]:
    final = trace.get("final_answer") or {}
    question = str(trace.get("question", ""))
    same = []
    if qset is not None:
        key = normalise(question)
        same = [q.id for q in qset.questions if normalise(q.question) == key]
    errors = sorted(
        {str(t.get("error_code")) for t in trace.get("tool_calls", []) if t.get("is_error")}
    )
    return {
        "trace_id": fb.trace_id,
        "review": "pending",
        "feedback": {"rating": fb.rating, "note": fb.note, "at": fb.created_at},
        "asked": {
            "question": question,
            "today": trace.get("today"),
            "status": trace.get("status"),
            "model": trace.get("model"),
            "prompt_version": trace.get("prompt_version"),
            "system_prompt_sha256": trace.get("system_prompt_sha256"),
        },
        "answer": {
            "text": final.get("text"),
            "assumptions": final.get("assumptions", []),
            "metrics_used": final.get("metrics_used", []),
            "sql_used": final.get("sql_used", []),
        },
        "tool_errors": errors,
        "same_question_in_eval_set": same,
        "proposed": {
            "id": TODO,
            "question": question,
            "category": TODO,
            "difficulty": TODO,
            "split": TODO,
            "today": TODO,
            "gold_sql": TODO,
            "answer_type": TODO,
            "tests": fb.note or TODO,
        },
    }


def load_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path.name} must be a list of candidates")
    return data


def import_feedback(
    traces_dir: Path, out_path: Path = DEFAULT_CANDIDATES_PATH, qset: QuestionSet | None = None
) -> ImportSummary:
    feedback = load_feedback(traces_dir)
    latest = latest_by_trace(feedback)
    existing = load_candidates(out_path)
    in_file = {str(c.get("trace_id")) for c in existing}
    bad = [fb for fb in latest.values() if fb.rating == "bad"]

    added: list[dict[str, Any]] = []
    missing = 0
    for fb in sorted(bad, key=lambda f: f.created_at):
        if fb.trace_id in in_file:
            continue
        trace = find_trace(traces_dir, fb.trace_id)
        if trace is None:
            missing += 1
            continue
        added.append(candidate(fb, trace, qset))

    now_good = sum(1 for tid in in_file if tid in latest and latest[tid].rating == "good")
    if added or not out_path.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(existing + added, sort_keys=False, allow_unicode=True, width=100)
        out_path.write_text(HEADER + "\n" + (body if existing + added else "[]\n"), "utf-8")
    return ImportSummary(
        feedback=len(feedback),
        bad=len(bad),
        added=len(added),
        already_in_file=sum(1 for fb in bad if fb.trace_id in in_file),
        missing_traces=missing,
        now_good=now_good,
        matches_eval_set=sum(1 for c in added if c["same_question_in_eval_set"]),
        path=out_path,
    )
