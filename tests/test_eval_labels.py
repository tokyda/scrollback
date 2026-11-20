"""Suggested bad-case labels and scoring false-negative flags (DESIGN §7.3)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from scrollback.evals.labels import load_confirmed, suggest_label, suspect_scoring, write_label_file
from scrollback.evals.questions import load_questions
from scrollback.evals.score import Score

QSET = load_questions()


def label(
    qid: str, s: Score, status: str = "answered", tool_calls: Any = None, sql_used: Any = None
) -> tuple[str, str]:
    return suggest_label(
        QSET.get(qid),
        s,
        status=status,
        tool_calls=tool_calls or [],
        sql_used=sql_used if sql_used is not None else ["SELECT 1"],
    )


def test_status_failures() -> None:
    assert label("q001", Score(False), status="step_limit")[0] == "tool_loop_or_step_limit"
    assert label("q001", Score(False), status="refused")[0] == "over_refusal"


def test_refusal_questions() -> None:
    assert label("q063", Score(False, fabricated=[42.0]))[0] == "failed_refusal"
    assert label("q072", Score(False, fabricated=[0.0]))[0] == "failed_refusal"
    assert label("q072", Score(False))[0] == "missing_caveat"


def test_no_cited_result() -> None:
    errors = [
        {
            "name": "run_sql",
            "is_error": True,
            "error_code": "execution_error",
            "output": '[execution_error] Binder Error: Referenced column "x" not found',
        }
    ]
    assert label("q001", Score(False), tool_calls=errors, sql_used=[])[0] == (
        "hallucinated_table_or_column"
    )
    timeout = [{"name": "run_sql", "is_error": True, "error_code": "timeout", "output": "slow"}]
    assert (
        label("q001", Score(False), tool_calls=timeout, sql_used=[])[0] == "sql_error_unrecovered"
    )
    assert label("q001", Score(False, refused=True), sql_used=[])[0] == "over_refusal"


def test_definition_misreport_caveat() -> None:
    assert label("q042", Score(False, execution=True, wrong_value=62.03))[0] == (
        "wrong_metric_definition"
    )
    assert label("q001", Score(False, execution=True, answer=False))[0] == "answer_misreport"
    assert label("q071", Score(False, execution=True, answer=True, caveat=False))[0] == (
        "missing_caveat"
    )


def test_date_mismatch_and_hints() -> None:
    lab, reason = label(
        "q013",
        Score(False, execution=False, answer=False),
        sql_used=["SELECT ... WHERE local_date BETWEEN '2026-11-10' AND '2026-11-16'"],
    )
    assert lab == "wrong_time_window" and "2026-11-10" in reason
    lab, _ = label(
        "q043",
        Score(False, execution=False, answer=False),
        sql_used=["... WHERE local_date = '2026-11-13'"],
    )
    assert lab == "timezone_or_day_boundary"
    lab, _ = label("q022", Score(False, execution=False, answer=False), sql_used=["SELECT 1"])
    assert lab == "wrong_metric_definition"  # from the question's likely_failure hint


@pytest.mark.parametrize(
    ("qid", "s", "flag"),
    [
        ("q063", Score(False, refused=True, fabricated=[3.0]), "numbers [3.0] counted as made up"),
        ("q063", Score(False, refused=False), "no refusal phrase matched"),
        ("q072", Score(False, caveat=False), "no 'no data' phrase matched"),
        ("q001", Score(False, execution=True, answer=False), "text check failed"),
        ("q001", Score(False, execution=False, answer=True), "no cited result matched"),
        ("q071", Score(False, execution=True, answer=True, caveat=False), "only the caveat"),
        ("q001", Score(False, execution=False, answer=False), None),
        ("q001", Score(True), None),
    ],
)
def test_suspect_scoring(qid: str, s: Score, flag: str | None) -> None:
    result = suspect_scoring(QSET.get(qid), s)
    assert (result is None) if flag is None else (result is not None and flag in result)


def test_label_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "run.yaml"
    failures = [
        {"question": "q001", "repeat": 1, "suggested": "answer_misreport", "label": None},
        {"question": "q002", "repeat": 2, "suggested": "aggregation_error", "label": None},
    ]
    write_label_file("run", failures, path)
    doc = yaml.safe_load(path.read_text())
    doc["failures"][0]["label"] = "answer_misreport"
    path.write_text(yaml.safe_dump(doc))
    confirmed = load_confirmed(path)
    assert list(confirmed) == [("q001", 1)]
    doc["failures"][1]["label"] = "nonsense"
    path.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="unknown label"):
        load_confirmed(path)


def test_suspect_flags_cover_groups_a_and_c() -> None:
    # Group A (old scoring): a wrong value stated next to the gold value.
    a = Score(False, execution=True, answer=False, wrong_value=11885.0, gold_stated=True)
    flag = suspect_scoring(QSET.get("q003"), a)
    assert flag is not None and "not labelled context" in flag
    # Group C (old scoring): says there is no data, but numbers were counted as amounts.
    c = Score(False, caveat=True, fabricated=[103.0, 167.0])
    flag = suspect_scoring(QSET.get("q072"), c)
    assert flag is not None and "says there is no data" in flag
