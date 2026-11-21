"""`eval import-feedback`: bad feedback -> git-ignored candidate file (synthetic traces only)."""

import json
import subprocess
from pathlib import Path

import yaml
from typer.testing import CliRunner

from scrollback.cli import app
from scrollback.evals.feedback_import import import_feedback, load_candidates
from scrollback.evals.questions import load_questions
from scrollback.feedback import record_feedback

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


def _trace(traces: Path, trace_id: str, question: str, *, error: bool = False) -> None:
    traces.mkdir(parents=True, exist_ok=True)
    day = f"{trace_id[:4]}-{trace_id[4:6]}-{trace_id[6:8]}"
    record = {
        "trace_id": trace_id, "question": question, "today": "2026-11-22",
        "status": "answered", "model": "claude-haiku-5-5", "prompt_version": "3",
        "system_prompt_sha256": "abc",
        "final_answer": {"text": "About 40 min.", "assumptions": ["day = 2026-11-20"],
                         "metrics_used": ["est_watch_minutes"], "sql_used": ["SELECT 1"]},
        "tool_calls": [{"is_error": error, "error_code": "not_aggregate" if error else None}],
    }  # fmt: skip
    with (traces / f"{day}.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")


T1, T2, T3, T4 = (f"20261122T0{i}0000-0000000{i}" for i in range(1, 5))


def test_import_adds_bad_only_and_is_idempotent(tmp_path: Path) -> None:
    traces, out = tmp_path / "traces", tmp_path / "cand" / "feedback.yaml"
    qset = load_questions()
    known = qset.questions[0].question
    _trace(traces, T1, "how much last night?", error=True)
    _trace(traces, T2, "good one")
    _trace(traces, T3, known.upper() + "!!")  # same question as the eval set's first
    record_feedback(traces, None, trace_id=T1, rating="bad", note="wrong day")
    record_feedback(traces, None, trace_id=T2, rating="good")
    record_feedback(traces, None, trace_id=T3, rating="bad")
    record_feedback(traces, None, trace_id=T4, rating="bad")  # no trace yet

    s = import_feedback(traces, out, qset)
    assert (s.feedback, s.bad, s.added, s.missing_traces, s.matches_eval_set) == (4, 3, 2, 1, 1)
    text = out.read_text()
    assert text.startswith("# Candidate eval questions") and "GIT-IGNORED" in text
    cands = load_candidates(out)
    assert [c["trace_id"] for c in cands] == [T1, T3]
    c1 = cands[0]
    assert c1["review"] == "pending" and c1["feedback"]["note"] == "wrong day"
    assert c1["asked"]["question"] == "how much last night?"
    assert c1["answer"]["sql_used"] == ["SELECT 1"] and c1["tool_errors"] == ["not_aggregate"]
    assert c1["proposed"]["gold_sql"] == "TODO" and c1["proposed"]["tests"] == "wrong day"
    assert cands[1]["same_question_in_eval_set"] == [qset.questions[0].id]

    # owner edits survive a re-run; nothing is duplicated; T4 is added once its trace exists
    cands[0]["review"] = "accepted"
    out.write_text(yaml.safe_dump(cands, sort_keys=False))
    _trace(traces, T4, "late trace")
    s = import_feedback(traces, out, qset)
    assert (s.added, s.already_in_file) == (1, 2)
    cands = load_candidates(out)
    assert [c["trace_id"] for c in cands] == [T1, T3, T4] and cands[0]["review"] == "accepted"

    # a later good rating doesn't delete the entry, it is flagged
    record_feedback(traces, None, trace_id=T1, rating="good")
    s = import_feedback(traces, out, qset)
    assert (s.added, s.now_good) == (0, 1)


def test_questions_yaml_untouched_and_candidates_ignored(tmp_path: Path) -> None:
    before = (REPO / "eval/questions.yaml").read_bytes()
    traces = tmp_path / "traces"
    _trace(traces, T1, "q")
    record_feedback(traces, None, trace_id=T1, rating="bad")
    out = tmp_path / "feedback.yaml"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"paths:\n  traces_dir: {traces}\n")
    result = runner.invoke(
        app, ["eval", "import-feedback", "--out", str(out), "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert "Added 1 candidates" in result.output and "About 40 min" not in result.output
    assert (REPO / "eval/questions.yaml").read_bytes() == before
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "eval/candidates/feedback.yaml"], cwd=REPO, check=False
    )
    assert ignored.returncode == 0


def test_empty_feedback_writes_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "f.yaml"
    s = import_feedback(tmp_path / "traces", out, None)
    assert s.added == 0 and load_candidates(out) == []
