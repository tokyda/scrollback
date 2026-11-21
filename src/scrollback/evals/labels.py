"""Bad-case labels (DESIGN §7.3): a suggested primary label per failure, confirmed by hand.

``suggest_label`` reads the score and the trace and proposes one label from the taxonomy with a
one-line reason. ``suspect_scoring`` separately flags failures that may be scoring false
negatives (a regex or matcher misfire rather than an agent mistake) so they are reviewed apart
from genuine agent failures.

Hand-confirmed labels live in ``eval/labels/<run_id>.yaml``: set ``label`` on each failure.
An ``overrides`` list in the same file records an owner decision that reverses the scorer for
one (question, repeat): ``passed``, a ``label`` when it fails, and a ``reason``. ``eval
rescore`` applies it and keeps the scorer's own verdict as ``passed_scored``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from scrollback.evals.questions import LABELS, Question
from scrollback.evals.score import Score

HALLUCINATION_RE = re.compile(
    r"Referenced column|does not have a column|not found in FROM clause|"
    r"Table with name .* does not exist|table_not_allowed|unknown table",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ENDED_STATUSES = {"step_limit", "no_final_answer", "max_tokens", "error", "harness_error"}


def _dates(*sqls: str | None) -> set[str]:
    return {d for sql in sqls if sql for d in DATE_RE.findall(sql)}


def suggest_label(
    q: Question,
    s: Score,
    *,
    status: str,
    tool_calls: list[dict[str, Any]],
    sql_used: list[str],
) -> tuple[str, str]:
    """(label, one-line reason) for a failed run."""
    if status in ENDED_STATUSES:
        return "tool_loop_or_step_limit", f"run ended with status {status} and no answer"
    if status == "refused":
        return "over_refusal", "the model declined the request (stop_reason refusal)"

    if q.answer_type == "refusal":
        if s.fabricated:
            return "failed_refusal", f"answered with numbers {s.fabricated} instead of refusing"
        return "failed_refusal", "did not say the data cannot answer this"
    if q.answer_type == "no_data":
        if s.fabricated:
            return "failed_refusal", f"stated an amount {s.fabricated} for a day with no data"
        return "missing_caveat", "did not say there is no data for today"

    sql_errors = [t for t in tool_calls if t.get("name") == "run_sql" and t.get("is_error")]
    hallucinated = [t for t in sql_errors if HALLUCINATION_RE.search(str(t.get("output", "")))]

    if not sql_used:
        if s.refused:
            return "over_refusal", "refused an answerable question"
        if hallucinated:
            return "hallucinated_table_or_column", "queried a table or column that does not exist"
        if sql_errors:
            codes = sorted({str(t.get("error_code")) for t in sql_errors})
            return "sql_error_unrecovered", f"no successful query cited; errors {codes}"
        return "answer_misreport", "answer cites no query result"

    if isinstance(s.wrong_value, str):
        return "answer_misreport", f"labels {s.wrong_value[:10]} with the wrong weekday"
    if s.wrong_value is not None:
        return "wrong_metric_definition", f"leads with {s.wrong_value}, a wrong-definition value"
    if s.execution and not s.answer:
        return "answer_misreport", "the cited result matches gold but the text does not state it"
    if s.execution and s.answer:
        if q.requires_caveat and not s.caveat:
            return "missing_caveat", "right number but no estimate/coverage caveat"
        if q.category == "ambiguous" and not s.assumptions:
            return "missing_caveat", "ambiguous question answered without stating assumptions"
        return "answer_misreport", "result and text match different readings"

    gold_dates = _dates(q.gold_sql, *q.alternatives)
    agent_dates = _dates(*sql_used)
    if gold_dates and agent_dates and agent_dates != gold_dates:
        label = (
            q.likely_failure
            if q.likely_failure in ("wrong_time_window", "timezone_or_day_boundary")
            else "wrong_time_window"
        )
        return label, f"date literals {sorted(agent_dates)} vs gold {sorted(gold_dates)}"
    if hallucinated:
        return "hallucinated_table_or_column", "hit unknown columns, then answered off-target"
    if q.likely_failure:
        return q.likely_failure, f"result does not match gold (question hint: {q.likely_failure})"
    return "aggregation_error", "result does not match gold"


def suspect_scoring(q: Question, s: Score) -> str | None:
    """Why this failure might be a scoring false negative, or None."""
    if s.passed:
        return None
    if q.answer_type == "refusal":
        if s.refused and s.fabricated:
            return f"refusal phrase found but numbers {s.fabricated} counted as made up"
        if not s.refused and not s.fabricated:
            return "no numbers stated but no refusal phrase matched"
        return None
    if q.answer_type == "no_data":
        if not s.fabricated and not s.caveat:
            return "no amount stated but no 'no data' phrase matched"
        if s.fabricated and s.caveat:
            return f"says there is no data but numbers {s.fabricated} counted as today's amount"
        return None
    if s.wrong_value is not None and s.gold_stated:
        return (
            f"leads with wrong-definition value {s.wrong_value} but also states the gold "
            "value: check that the wrong value is the headline, not labelled context"
        )
    if s.execution and not s.answer and s.wrong_value is None:
        return "result matches gold but the text check failed"
    if s.answer and not s.execution:
        return "text states the gold value but no cited result matched"
    if s.execution and s.answer and q.requires_caveat and not s.caveat:
        return "only the caveat check failed"
    return None


def write_label_file(run_id: str, failures: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "run_id": run_id,
        "instructions": (
            "Confirm each failure: set `label` to one of the taxonomy labels (the suggestion is "
            "a starting point). Optionally set `scoring_false_negative: true` and a `note`."
        ),
        "taxonomy": list(LABELS),
        "failures": failures,
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100))


def load_confirmed(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """(question, repeat) -> failure entry, for entries with a confirmed label."""
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text()) or {}
    out = {}
    for f in doc.get("failures", []):
        if f.get("label"):
            if f["label"] not in LABELS:
                raise ValueError(f"{f['question']}: unknown label {f['label']!r}")
            out[(f["question"], int(f["repeat"]))] = f
    return out


def load_overrides(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """(question, repeat) -> owner override of the scorer's pass/fail verdict."""
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text()) or {}
    out = {}
    for o in doc.get("overrides") or []:
        key = f"{o.get('question')} r{o.get('repeat')}"
        if not isinstance(o.get("passed"), bool) or not o.get("reason"):
            raise ValueError(f"override {key}: needs `passed` (true/false) and a `reason`")
        if not o["passed"] and o.get("label") not in LABELS:
            raise ValueError(f"override {key}: a fail needs a taxonomy `label`")
        out[(o["question"], int(o["repeat"]))] = o
    return out
