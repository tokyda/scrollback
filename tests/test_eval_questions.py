"""Eval question set: schema rules, anchor filtering, gold checks and snapshot drift."""

import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from scrollback.evals.gold import (
    GoldAnswer,
    QuestionGold,
    check_gold,
    run_gold_sql,
    snapshot_drift,
    write_snapshot,
)
from scrollback.evals.questions import (
    CATEGORIES,
    DEFAULT_QUESTIONS_PATH,
    QuestionSet,
    load_questions,
)
from scrollback.evals.warehouses import filter_export
from scrollback.synth.generator import synthesize

QSET = load_questions()


def _raw() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_QUESTIONS_PATH.read_text())


# --------------------------------------------------------------------------- the committed set


def test_set_size_and_categories() -> None:
    assert 60 <= len(QSET.questions) <= 80
    per_cat = Counter(q.category for q in QSET.questions)
    assert set(per_cat) == set(CATEGORIES)
    assert min(per_cat.values()) >= 6


def test_split_is_stratified_about_70_30() -> None:
    dev = len(QSET.by_split("dev"))
    assert 0.6 <= dev / len(QSET.questions) <= 0.75
    for cat in CATEGORIES:
        qs = [q for q in QSET.questions if q.category == cat]
        test_share = sum(q.split == "test" for q in qs) / len(qs)
        assert 0.15 <= test_share <= 0.45, cat


def test_phase3_q5_included_as_is() -> None:
    q = QSET.get("q042")
    assert q.question == "How many sessions did I have last week, and how long was the longest one?"
    assert q.today == date(2026, 11, 20)


def test_params_match_current_defaults() -> None:
    # Not required forever, but a change here must be deliberate (re-snapshot golds).
    assert QSET.params.metrics.session_gap_min == 30
    assert QSET.params.timezone.local_tz == "Asia/Singapore"


# --------------------------------------------------------------------------- schema rules


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["questions"].append(dict(d["questions"][0])), "duplicate question ids"),
        (lambda d: d["questions"][0].update(gold_sql=None), "gold_sql is required"),
        (lambda d: d["questions"][62].update(gold_sql="SELECT 1"), "no gold_sql"),
        (lambda d: d["questions"][71].update(gold_sql="SELECT 1"), "no_data questions have no"),
        (
            lambda d: d["questions"][0].update(answer_type="no_data", gold_sql=None),
            "only used for trap",
        ),
        (lambda d: d["questions"][0].update(alternatives=["SELECT 1"]), "only for ambiguous"),
        (lambda d: d["questions"][69].update(requires_caveat=False), "require a caveat"),
        (lambda d: d["questions"][0].update(category="unanswerable"), "unanswerable <=>"),
        (lambda d: d["questions"][0].update(units=["count", "count"]), "exactly one unit"),
        (
            lambda d: d.update(
                questions=[q for q in d["questions"] if q["category"] != "trap"][:-1]
                + [q for q in d["questions"] if q["category"] == "trap"][:5]
            ),
            "categories with < 6",
        ),
        (lambda d: d["questions"][0].update(bogus=1), "Extra inputs"),
    ],
)
def test_schema_rejects(mutate: Any, message: str) -> None:
    data = _raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        QuestionSet.model_validate(data)


def test_split_must_cover_each_category() -> None:
    data = _raw()
    for q in data["questions"]:
        if q["category"] == "ranking":
            q["split"] = "dev"
    with pytest.raises(ValidationError, match="ranking must have dev and test"):
        QuestionSet.model_validate(data)


# --------------------------------------------------------------------------- anchor filtering


def test_filter_export_drops_records_after_today() -> None:
    export = synthesize(days=10).export  # 2026-11-10 .. 2026-11-19 local
    kept = filter_export(export, date(2026, 11, 14), QSET.params)
    views = kept["Your Activity"]["Watch History"]["VideoList"]
    all_views = export["Your Activity"]["Watch History"]["VideoList"]
    assert 0 < len(views) < len(all_views)
    # Every kept, parseable timestamp is on or before 14 Nov local (UTC+8 -> <= 15:59:59 UTC).
    parseable = [v["Date"] for v in views if v["Date"][:2] == "20" and v["Date"][4] == "-"]
    assert max(parseable) <= "2026-11-14 15:59:59"
    # The original document is not modified.
    assert len(export["Your Activity"]["Watch History"]["VideoList"]) == len(all_views)


def test_filter_keeps_unparseable_dates() -> None:
    export = synthesize(days=60).export
    kept = filter_export(export, date(2026, 9, 1), QSET.params)
    bad = [
        v for v in kept["Your Activity"]["Watch History"]["VideoList"]
        if not (v.get("Date", "")[:2] == "20" and v.get("Date", "")[4:5] == "-")
    ]  # fmt: skip
    all_bad = [
        v for v in export["Your Activity"]["Watch History"]["VideoList"]
        if not (v.get("Date", "")[:2] == "20" and v.get("Date", "")[4:5] == "-")
    ]  # fmt: skip
    assert len(bad) == len(all_bad)


# --------------------------------------------------------------------------- gold checks


def test_check_gold_flags_problems() -> None:
    con = duckdb.connect(":memory:")
    empty = run_gold_sql(con, "SELECT 1 AS x WHERE false")
    golds = {q.id: QuestionGold(None) for q in QSET.questions}
    golds["q001"] = QuestionGold(empty)
    golds["q030"] = QuestionGold(GoldAnswer(["a"], [[1.0]]))  # 1 column, 2 units
    golds["q003"] = QuestionGold(GoldAnswer(["n"], [[11885]]))  # equals a wrong_value
    bad = QSET.model_copy(
        update={"questions": [
            q.model_copy(update={"gold_sql": "SELECT local_date FROM fct_views"})
            if q.id == "q002" else q for q in QSET.questions
        ]}
    )  # fmt: skip
    problems = check_gold(bad, golds)
    assert any(p.startswith("q002: gold SQL rejected by the agent's guard") for p in problems)
    assert any(p.startswith("q001: gold returned no rows") for p in problems)
    assert any(p.startswith("q030: gold has 1 columns but 2 units") for p in problems)
    assert any(p.startswith("q003: wrong_value 11885") for p in problems)


def test_snapshot_drift(tmp_path: Path) -> None:
    golds = {"q001": QuestionGold(GoldAnswer(["n"], [[1]]))}
    path = tmp_path / "snap.json"
    write_snapshot(golds, path)
    assert snapshot_drift(golds, path) == []
    changed = {"q001": QuestionGold(GoldAnswer(["n"], [[2]]))}
    assert snapshot_drift(changed, path) == ["q001: gold differs from snapshot"]
    assert "no gold snapshot" in snapshot_drift(golds, tmp_path / "missing.json")[0]
    assert json.loads(path.read_text())["q001"]["gold"]["rows"] == [[1]]
