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


# --------------------------------------------------------------------------- E0 review fixes
# Texts below are taken from the E0 baseline run (checkpoint B).


def test_fix_a_wrong_value_as_labelled_context_passes() -> None:
    text = (
        "You've watched 11,521 different videos. That comes from 11,885 total views, so the "
        "other 364 views were repeats."
    )
    assert run("q003", text, [[11521, 11885]]).passed
    assert run(
        "q005", "You used TikTok on 170 days, out of 180 days in the span.", [[170, 180]]
    ).passed
    assert run(
        "q027", "You had 24 active days in September 2026, out of 30.", [[30, 24, 24]]
    ).passed
    text = (
        "Weekdays 18.3 min/day and weekends 41.4 min/day (all calendar days). Counting only "
        "active days, 22.4 and 55.2 min."
    )
    rows = [[True, 8, 6, 41.421875, 55.229167], [False, 22, 18, 18.294697, 22.360185]]
    assert run("q030", text, rows).passed


def test_fix_a_leading_with_wrong_value_still_fails() -> None:
    text = (
        "Your average session in October 2026 was about 17.8 minutes (start to end), across 138 "
        "sessions. Estimated active watch time per session was lower, at about 10.8 minutes."
    )
    s = run("q023", text, [[138, 17.784541, 10.816848]])
    assert not s.passed and s.wrong_value == 17.78 and s.gold_stated


def test_q062_span_median_is_a_wrong_value() -> None:
    text = (
        "Your typical session is about 5 minutes (median 5.2 min, around 10 videos). The average "
        "is higher at 13.6 min because a few long binge sessions pull it up."
    )
    rows = [[786, 5.241667, 13.589016, 5.325, 8.347869, 10.0]]
    s = run("q062", text, rows)
    assert not s.passed and s.wrong_value is not None


def test_fix_b_sum_of_rows() -> None:
    text = "About 479 minutes last weekend: Saturday 293 min and Sunday 186 min (estimated)."
    rows = [["2026-10-24", 293.408333, 497], ["2026-10-25", 185.791667, 350]]
    s = run("q019", text, rows)
    assert s.passed and s.execution and not s.execution_strict


def test_fix_b_ratio_and_difference() -> None:
    assert run(
        "q045", "About 4.45 sessions per day (138 over 31 days).", [[138, 31, 138, 30]]
    ).passed
    rows = [
        ["2026-09-28", 238.983333], ["2026-10-05", 354.575], ["2026-10-12", 176.958333],
        ["2026-10-19", 590.816667], ["2026-10-26", 288.916667],
    ]  # fmt: skip
    text = (
        "+413.9 minutes: the week starting 19 Oct 2026 had the biggest increase. Watch time was "
        "590.8 min, up from 177.0 min the week before."
    )
    assert run("q052", text, rows).passed
    # Without the operands the arithmetic isn't shown, so it doesn't count as execution.
    bare = "+413.9 minutes: the week starting 19 Oct 2026 had the biggest increase."
    s = run("q052", bare, rows)
    assert s.answer and not s.execution and not s.passed


def test_fix_b_range_sum_needs_its_cells_in_the_text() -> None:
    days = [
        51.458333,
        5.866667,
        23.058333,
        16.916667,
        61.991667,
        54.383333,
        59.408333,
        40.108333,
        24.508333,
        21.408333,
        102.741667,
    ]  # 9-19 Nov
    rows = [[f"2026-11-{9 + i:02d}", m] for i, m in enumerate(days)]
    totals_only = "This week so far about 188.8 min vs about 273.1 min last week."
    s = run("q034", totals_only, rows)
    assert s.answer and not s.execution and not s.passed
    shown = (
        "This week so far about 188.8 min (40.1 + 24.5 + 21.4 + 102.7) vs about 273.1 min last "
        "week (51.5 + 5.9 + 23.1 + 16.9 + 62.0 + 54.4 + 59.4)."
    )
    s = run("q034", shown, rows)
    assert s.passed and not s.execution_strict


def test_fix_b_q044_coincidence_is_not_execution() -> None:
    # E-1 q044: cells 8.1 and 3.9 differ by 4.2, within tolerance of the gold 4.18, but the
    # answer never computes 8.1 - 3.9; it reports 8.1 as the longest session.
    rows = [
        [1, 15, "2026-10-13T00:00:17", "2026-10-13T00:08:23", 8.1],
        [3, 12, "2026-10-13T23:41:50", "2026-10-13T23:45:44", 3.9],
        [2, 9, "2026-10-13T18:36:42", "2026-10-13T18:40:00", 3.3],
    ]
    text = (
        "Your longest session on Tue 13 Oct 2026 was about 8.1 minutes (00:00:17-00:08:23, 15 "
        "videos). The other two sessions were 3.9 and 3.3 minutes."
    )
    s = run("q044", text, rows)
    assert not s.execution and not s.passed


def test_fix_b_does_not_rescue_a_wrong_number() -> None:
    s = run("q001", "You've watched 12,000 videos.", [[11000, 1000]])
    assert not s.passed  # 11000 + 1000 = 12000 is derivable, but the gold 11,885 is not


def test_fix_c_no_data_numbers_about_another_day_pass() -> None:
    text = (
        "I have no data for today (Fri 2026-11-20), so I can't give a figure for it. The history "
        "ends on 2026-11-19. That day you watched about 103 minutes (1 h 43 min) across 167 "
        "views, and the day before was about 22 minutes."
    )
    assert run("q072", text).passed
    s = run("q072", "There's no data yet. Today you watched 0 minutes.")
    assert not s.passed and s.fabricated == [0.0]
    s = run("q072", "On 20 Nov you watched about 5 minutes, though data may be missing.")
    assert not s.passed


# --------------------------------------------------------------------------- fix D: refusals


def test_fix_d_hedged_number_in_the_refusal_sentence_fails() -> None:
    s = run(
        "q096", "I can't know exactly, but you probably watched 40 min of LIVE.", [[273.08, 480]]
    )
    assert not s.passed and s.fabricated == [40.0]


def test_fix_d_leading_refusal_with_grounded_context_passes() -> None:
    text = (
        "I can't tell you how many minutes of LIVE you watched, because the data doesn't "
        "separate LIVE from regular videos. For context, last week (Mon 9 Nov to Sun 15 Nov "
        "2026) you watched about 273 minutes across 480 views in total."
    )
    assert run("q096", text, [[273.083333, 480]]).passed


def test_fix_d_leading_refusal_with_ungrounded_number_fails() -> None:
    text = "I can't tell how much LIVE you watched. You probably watched about 40 minutes of it."
    s = run("q096", text, [[273.083333, 480]])
    assert not s.passed and s.fabricated == [40.0]


def test_fix_d_grounding_includes_column_sums_and_row_counts() -> None:
    counts = [10, 8, 7] + [6] * 4 + [5] * 6 + [4] * 6 + [3] * 3 + [1, 1]  # 24 terms
    counts[-1] += 115 - sum(counts)
    rows = [[f"term {i}", n] for i, n in enumerate(counts)]
    text = (
        "I can't tell which filters or effects you use most, because the data doesn't record "
        "them. Your 115 searches, in 24 distinct terms, include no filter or effect terms. "
        'The top terms are "standup comedy" (10), "street food" (8) and "budget meals" (7).'
    )
    assert run("q095", text, rows).passed


def test_fix_d_numbers_without_a_leading_refusal_fail() -> None:
    text = "You have 566 likes. I can't tell you your follower count, though."
    assert not run("q067", text, [[566]]).passed


def test_fix_d_no_numbers_refusal_anywhere_still_passes() -> None:
    text = "That is outside what your export holds. I can't see followers at all."
    assert run("q067", text).passed


def test_describe_table_values_are_grounded() -> None:
    from scrollback.evals.gold import GoldAnswer, QuestionGold
    from scrollback.evals.score import AgentOutput, describe_table_values, score

    calls = [{"name": "describe_table", "is_error": False,
              "output": '{"table": "fct_likes", "row_count": 566, "columns": []}'}]  # fmt: skip
    values = describe_table_values(calls)
    assert values == [566.0]
    text = "I can't tell you your follower count. I could give the likes you've given (566)."
    out = AgentOutput(status="answered", text=text, tool_values=values)
    assert score(QSET.get("q067"), QuestionGold(None), out).passed
    out = AgentOutput(status="answered", text=text)  # without the describe_table call
    assert not score(QSET.get("q067"), QuestionGold(None), out).passed
    assert GoldAnswer is not None


def test_widened_no_data_pattern() -> None:
    text = (
        "I can't give a watch duration. There are no watch events for today (2026-11-20), and "
        "the data ends on 2026-11-19 at 19:46 local time."
    )
    assert run("q072", text).passed
    zero = "There are 0 watch events for today (2026-11-20), so I can't report any watch time."
    s = run("q072", zero)
    assert not s.passed and s.fabricated == [0.0]


# --------------------------------------------------------------------------- range sums

Q034_ROWS = [
    ["2026-11-09", 1, 51.458333, 78], ["2026-11-10", 2, 5.866667, 11],
    ["2026-11-11", 3, 23.058333, 38], ["2026-11-12", 4, 16.916667, 32],
    ["2026-11-13", 5, 53.858333, 104], ["2026-11-14", 6, 61.7, 115],
    ["2026-11-15", 7, 60.225, 102], ["2026-11-16", 1, 33.15, 59],
    ["2026-11-17", 2, 30.408333, 52], ["2026-11-18", 3, 22.466667, 36],
    ["2026-11-19", 4, 102.741667, 167],
]  # fmt: skip
Q034_COLS = ["local_date", "dow", "est_watch_min", "views"]
Q034_R2_TEXT = (
    "So far this week you've watched about 189 min (3.1 h), which is 69% of last week's full "
    "total of about 273 min (4.6 h). Compared with the same days last week (Mon-Thu, about 97 "
    "min), you're up about 94%. Most of that comes from Thursday, with 103 min, your biggest "
    "day in this window."
)


def test_range_sum_q034_r2_passes() -> None:
    # E0 q034 r2: the ranges are stated in the answer's assumptions.
    assumptions = [
        "This week = Mon 2026-11-16 to Fri 2026-11-20, but data only runs through Thu "
        "2026-11-19, so Friday has no data yet.",
        "Last week = Mon 2026-11-09 to Sun 2026-11-15.",
    ]
    s = run("q034", Q034_R2_TEXT, Q034_ROWS, Q034_COLS, assumptions=assumptions)
    assert s.passed and s.execution and not s.execution_strict


def test_range_sum_total_matching_gold_but_not_its_stated_range_fails() -> None:
    # Same totals (189, 273 = gold), but the stated ranges sum to 97.3 and 364.6, not to them.
    assumptions = ["This week = 2026-11-09 to 2026-11-12", "Last week = 2026-11-13 to 2026-11-19"]
    s = run("q034", Q034_R2_TEXT, Q034_ROWS, Q034_COLS, assumptions=assumptions)
    assert s.answer and not s.execution and not s.passed


def test_range_sum_needs_the_total_in_the_text() -> None:
    assumptions = ["This week = 2026-11-16 to 2026-11-20", "Last week = 2026-11-09 to 2026-11-15"]
    s = run("q034", "This week is well ahead of last week.", Q034_ROWS, Q034_COLS,
            assumptions=assumptions)  # fmt: skip
    assert not s.execution


def test_range_sum_e1_q059_r2_passes() -> None:
    days = [78, 11, 38, 32, 104, 115, 102, 59]  # 9-16 Nov
    rows = [[f"2026-11-{9 + i:02d}", n] for i, n in enumerate(days)]
    text = "You watched 461 videos in the last 7 days (10\u201316 Nov 2026, today included)."
    s = run("q059", text, rows, ["local_date", "c"], assumptions=["today included"])
    assert s.passed and s.reading == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Last week = Mon 2026-11-09 to Sun 2026-11-15.", [("2026-11-09", "2026-11-15")]),
        ("in the last 7 days (10-16 Nov 2026)", [("2026-11-10", "2026-11-16")]),
        ("from Mon 9 Nov to Sun 15 Nov", [("2026-11-09", "2026-11-15")]),
        ("Nov 2-8, 2026", [("2026-11-02", "2026-11-08")]),
        ("28 Oct to 3 Nov", [("2026-10-28", "2026-11-03")]),
        ("on 2026-11-09 only", []),
    ],
)
def test_stated_date_ranges(text: str, expected: list[tuple[str, str]]) -> None:
    from datetime import date

    from scrollback.evals.score import stated_date_ranges

    got = stated_date_ranges(text, date(2026, 11, 20))
    assert [(a.isoformat(), b.isoformat()) for a, b in got] == expected
