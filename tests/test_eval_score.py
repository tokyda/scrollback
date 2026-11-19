"""Deterministic scoring (DESIGN §7.2) on real eval questions with hand-built agent outputs."""

import json
from typing import Any

import pytest

from scrollback.evals.gold import DEFAULT_SNAPSHOT, GoldAnswer, QuestionGold
from scrollback.evals.questions import load_questions
from scrollback.evals.score import (
    AgentOutput,
    ResultTable,
    Score,
    numbers_in,
    score,
    text_states,
)

QSET = load_questions()
SNAP = json.loads(DEFAULT_SNAPSHOT.read_text())


def gold(qid: str) -> QuestionGold:
    entry = SNAP[qid]
    g = entry["gold"]
    return QuestionGold(
        None if g is None else GoldAnswer(g["columns"], g["rows"]),
        [GoldAnswer(a["columns"], a["rows"]) for a in entry["alternatives"]],
    )


def run(
    qid: str,
    text: str,
    rows: list[list[Any]] | None = None,
    columns: list[str] | None = None,
    assumptions: list[str] | None = None,
    metrics: list[str] | None = None,
    status: str = "answered",
) -> Score:
    results = {}
    ids = []
    if rows is not None:
        cols = columns or [f"c{i}" for i in range(len(rows[0]) if rows else 1)]
        results["r1"] = ResultTable(cols, rows)
        ids = ["r1"]
    out = AgentOutput(
        status=status,
        text=text,
        result_ids=ids,
        metrics_used=metrics or [],
        assumptions=assumptions if assumptions is not None else ["range stated"],
        results=results,
    )
    return score(QSET.get(qid), gold(qid), out)


# --------------------------------------------------------------------------- scalars


def test_exact_count() -> None:
    s = run("q001", "You've watched 11,885 videos.", [[11885]])
    assert s.passed and s.execution and s.answer


def test_wrong_count_fails_both() -> None:
    s = run("q001", "You've watched 11,800 videos.", [[11800]])
    assert not s.passed and not s.execution and not s.answer


def test_rounded_text_and_rounded_sql() -> None:
    assert run("q007", "About 1,493 minutes in October.", [[1492.725]]).passed
    assert run("q007", "About 1,492.7 minutes.", [[1492.7]]).passed  # ROUND(x, 1)
    assert run("q007", "About 1,493 minutes.", [[1493.0]]).passed  # ROUND(x, 0)
    assert not run("q007", "About 1,500 minutes.", [[1492.725]]).answer


def test_seconds_result_for_minutes_gold() -> None:
    s = run("q007", "1,492.7 minutes", [[89563.5, 2687]], ["seconds", "views"])
    assert s.passed


def test_hours_text_for_minutes_gold_with_caveat() -> None:
    s = run("q071", "About 24.9 hours (estimated from gaps between views).", [[1492.725]])
    assert s.passed and s.caveat


def test_trap_without_caveat_fails() -> None:
    s = run("q071", "You spent 24.9 hours watching in October.", [[1492.725]])
    assert s.execution and s.answer and not s.caveat and not s.passed


def test_explicit_unit_rounding_but_no_bare_integer_scaling() -> None:
    q = QSET.get("q075")  # 60.225 minutes
    assert text_states("about 1 hour of screen time", 60.225, "minutes", q.tolerance)
    assert not text_states("you had 1 session", 60.225, "minutes", q.tolerance)
    s = run("q075", "Estimated 60 minutes across 1 session.", [[1, 60.225]])
    assert s.passed


def test_bare_small_integer_in_result_does_not_match_scaled() -> None:
    # A count of 1 must not match 60.225 minutes via an hours scaling.
    s = run("q075", "Estimated screen time: 60.2 minutes.", [[1]])
    assert not s.execution


def test_ratio_as_percentage() -> None:
    s = run("q022", "16.5% of your October views were late at night.", [[2687, 443, 16.49]])
    assert s.passed
    assert run("q022", "0.165 of views", [[0.1649]]).passed


def test_wrong_definition_value_fails_answer() -> None:
    s = run(
        "q042",
        "33 sessions last week; the longest ran about 62 minutes (34.7 min of estimated watch).",
        [[33, 3722, 2082.5]],
        ["n", "max_span_sec", "max_watch_sec"],
    )
    assert s.execution  # 2082.5 s = 34.71 min is in the result
    assert s.wrong_value == 62.03 and not s.answer and not s.passed


def test_definition_trap_correct() -> None:
    s = run("q042", "33 sessions; the longest was about 34.7 minutes.", [[33, 2082.5]])
    assert s.passed and s.wrong_value is None


def test_dates_are_not_numbers() -> None:
    assert [n.value for n in numbers_in("On Saturday 14 November 2026 (2026-11-14) at 21:00")] == []
    s = run("q043", "You had 8 sessions on Saturday 14 November.", [[8]])
    assert s.passed
    s = run("q043", "You had 9 sessions on Saturday 14 November.", [[9]])
    assert not s.passed and s.wrong_value == 9


# --------------------------------------------------------------------------- tables


def test_table_any_column_order_and_extra_columns() -> None:
    rows = [[41.421875, 18.294697, 30]]
    s = run("q030", "Weekdays averaged 18.3 min/day, weekends 41.4 min/day.", rows)
    assert s.passed


def test_table_long_format() -> None:
    rows = [[False, 18.29, 22], [True, 41.42, 8]]
    s = run("q030", "Weekdays 18.3 min/day vs weekends 41.4 min/day.", rows)
    assert s.passed


def test_table_wrong_values_active_days_only() -> None:
    s = run("q030", "Weekdays 22.4 min/day, weekends 55.2 min/day.", [[22.36, 55.23]])
    assert not s.passed and not s.execution


def test_ordered_table_prefix_and_order() -> None:
    rows = [["standup comedy", 10], ["street food", 8], ["budget meals", 7], ["thrift haul", 6]]
    text = "Top 3: standup comedy, street food and budget meals."
    assert run("q036", text, rows).passed
    swapped = [rows[1], rows[0], rows[2]]
    assert not run("q036", text, swapped).execution


def test_dates_and_days_of_week() -> None:
    assert run("q037", "Saturday 24 October was your biggest day.", [["2026-10-24", 293.4]]).passed
    assert run("q037", "Oct 24 was the top day.", [["2026-10-24"]]).passed
    assert run("q038", "Sundays are your heaviest day.", [[7, 52.38]]).passed
    assert run("q038", "Sunday, by a little.", [[0, 52.38]]).passed  # DuckDB dayofweek
    assert run("q038", "Sunday.", [["Sunday", 52.38]]).passed
    assert not run("q038", "Saturday.", [["Saturday", 51.67]]).passed


def test_hour_of_day() -> None:
    assert run("q058", "Most active 9pm-10pm.", [[21, 1010]]).passed
    assert run("q058", "The 21:00 hour.", [[21, 1010]]).passed
    alt = run("q058", "By watch time, 22:00 is your top hour.", [[22, 32031.5]])
    assert alt.passed and alt.reading == 1


# --------------------------------------------------------------------------- ambiguous


def test_ambiguous_any_reading_with_assumptions() -> None:
    s = run("q059", "480 videos in the last 7 days (9-15 Nov).", [[480]], assumptions=["9-15 Nov"])
    assert s.passed and s.reading == 0
    s = run("q059", "461 videos in the last 7 days.", [[461]], assumptions=["10-16 Nov"])
    assert s.passed and s.reading == 1


def test_ambiguous_requires_assumptions() -> None:
    s = run("q059", "480 videos.", [[480]], assumptions=[])
    assert s.execution and s.answer and not s.passed and s.assumptions is False


def test_ambiguous_reading_must_be_consistent() -> None:
    s = run("q059", "You watched 480 videos.", [[461]], assumptions=["x"])
    assert not s.passed


# --------------------------------------------------------------------------- refusals


def test_refusal_pass() -> None:
    text = "I can't tell: the data has no creator information (I checked 5 tables)."
    assert run("q063", text).passed


def test_refusal_with_dates_is_fine() -> None:
    text = "There is no creator field in the data, which covers 2026-05-24 to 19 Nov 2026."
    assert run("q063", text).passed


def test_refusal_fabricated_number_fails() -> None:
    s = run("q063", "I can't be sure, but creator_0042 appears in 42 of your views.")
    assert not s.passed and s.fabricated == [42.0]


def test_answer_without_refusal_fails() -> None:
    assert not run("q064", "You spent about 15 dollars.").passed


def test_no_data_today() -> None:
    assert run("q072", "There's no data for today yet: your export only goes up to 19 Nov.").passed
    s = run("q072", "You've watched 0 minutes today.", [[0.0]])
    assert not s.passed and s.fabricated == [0.0]
    s = run("q072", "0 minutes so far - though there is no data for today in the export.")
    assert not s.passed


def test_coverage_caveat() -> None:
    s = run("q074", "About 314.7 minutes, but your history only starts on 24 May.", [[314.658]])
    assert s.passed and s.caveat


# --------------------------------------------------------------------------- other


def test_not_answered() -> None:
    s = run("q001", "", status="step_limit")
    assert not s.passed and s.notes == ["status step_limit"]


def test_metric_selection_reported_not_required() -> None:
    s = run("q001", "11,885 videos.", [[11885]], metrics=["Views"])
    assert s.passed and s.metric_selection is True
    s = run("q001", "11,885 videos.", [[11885]], metrics=["view count"])
    assert s.passed and s.metric_selection is False


def test_typographic_apostrophes() -> None:
    assert run("q067", "I can\u2019t see follower data \u2013 it isn\u2019t in the export.").passed


@pytest.mark.parametrize(
    ("text", "values"),
    [
        ("16.5% of views", [(16.5, True)]),
        ("1,492.7 minutes and ~103", [(1492.7, False), (103.0, False)]),
        ("checked 6 tables and 2 queries", []),
        ("week of 2026-10-19: +413.9", [(413.9, False)]),
    ],
)
def test_numbers_in(text: str, values: list[tuple[float, bool]]) -> None:
    assert [(n.value, n.percent) for n in numbers_in(text)] == values
