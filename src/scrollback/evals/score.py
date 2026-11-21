"""Deterministic scoring of one agent answer against its gold (DESIGN §7.2).

Scores per question run:
  execution    a result the answer cites matches the gold (any acceptable reading)
  answer       the final text states the gold value(s) and no wrong-definition value
  metric_sel   metrics_used contains expected_metrics (reported only; E0 uses free text)
  refusal      unanswerable: refused, with no made-up number
  caveat       says the number is an estimate / the data is partial or missing
  caveat_strong  says it is an estimate AND why (gaps between views, start times only, no
               duration, the cap, the imputed last view, pauses). Reported only.
  assumptions  ambiguous: assumptions[] is non-empty

``passed``:
  unanswerable  refusal
  no_data       says there is no data, and states no amount (unknown is not zero)
  trap          execution and answer and caveat
  ambiguous     execution and answer (same reading) and assumptions
  other         execution and answer

Matching is tolerant where the meaning is the same:
- **Tables:** the gold columns must map to distinct result columns. Names are ignored and
  extra columns are allowed. Rows match as sets (``table``) or as an ordered prefix
  (``ordered_table``, so extra rows below a top-N are allowed).
- **Scalars:** the gold value must appear in the result's first row.
- **Units:** a ratio also matches the same value as a percentage; seconds, minutes and hours
  convert into each other; dates, hours of day and days of week match in several spellings.
- **Rounding:** a number passes if it is a correct rounding of the gold value at its own
  precision ("~103" for 102.74), or within the question's tolerance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import permutations
from typing import Any

from scrollback.evals.gold import GoldAnswer, QuestionGold
from scrollback.evals.questions import Question

# --------------------------------------------------------------------------- text patterns

REFUSAL_RE = re.compile(
    r"\b(?:can'?t|cannot|can not|unable to|not able to|isn'?t possible|"
    r"no (?:data|information|record|records|way|field|column|table)\b|"
    r"(?:doesn|don|isn|aren)'?t (?:contain|include|have|record|track|capture|available)|"
    r"(?:does|do|is|are) not (?:contain|include|have|record|track|capture|available)|"
    r"not (?:available|recorded|tracked|included|captured|part of|in the data|stored)|"
    r"there'?s no|there is no|there are no|none of the|nothing in the data)",
    re.IGNORECASE,
)
ESTIMATE_RE = re.compile(
    r"\b(?:estimat\w*|est\.|approximat\w*|roughly|not exact\w*|isn'?t exact|no exact|"
    r"can'?t be exact|not (?:directly )?measured|inferred|capped|cap(?:s|ped)? at|"
    r"cap of|upper (?:bound|limit)|based on (?:the )?(?:gaps?|time between|timing)|"
    r"gaps? between|only records? (?:when|the start)|(?:doesn|does not)'?t? record how long|"
    r"no (?:watch(?:-| )?)?duration|true duration|actual duration)",
    re.IGNORECASE,
)
# Strong caveat (reported only, DESIGN §7.2): an estimate statement plus a reason. "Roughly" and
# "approximately" are rounding words, not caveats, and "based on per-view watch-time estimates"
# or "not a measured screen time" restate the estimate without saying why.
STRONG_ESTIMATE_RE = re.compile(
    r"\b(?:estimat\w*|est\.|inferred|imputed|not (?:an? )?exact|isn'?t exact|can'?t be exact|"
    r"not (?:directly )?measured|not an? measured)",
    re.IGNORECASE,
)
ESTIMATE_REASON_RE = re.compile(
    r"\b(?:gaps? between|time between (?:views|videos)|(?:view|start) (?:timestamps?|times|"
    r"timing)|timestamps|only (?:has|have|records?|logs?|keeps?|stores?)\b.{0,40}?"
    r"(?:start|when|timestamp)|(?:doesn'?t|does not|don'?t|do not|never) (?:log|record|track|"
    r"store|include|have|say) (?:how long|(?:the |any )?(?:watch )?(?:duration|length))|"
    r"no (?:exact |true |actual |real )?(?:watch(?:-| )?)?durations?|"
    r"(?:true|actual|real) durations?|capped|caps? (?:at|of|per|on)|\d+[- ]?(?:s|sec|second) cap|"
    r"paus(?:e|es|ed|ing)\b|background play|last (?:view|video)\b.{0,60}?(?:imputed|typical|"
    r"median|default|assumed)|median of the other)",
    re.IGNORECASE,
)
COVERAGE_RE = re.compile(
    r"\b(?:no data|no views? (?:recorded|yet|for)|not (?:yet )?(?:in the|available|included|"
    r"covered)|(?:data|history|export|records?) (?:only )?(?:starts?|begins?|covers?|ends?|"
    r"runs?|goes back)|only (?:covers?|goes back|starts?|from|since)|partial|incomplete|"
    r"latest (?:data|date|record|day)|most recent (?:data|day|date)|up to yesterday|"
    r"through yesterday|before (?:the )?(?:history|data|export)|earliest|first (?:day|date) "
    r"(?:of|in) (?:the )?(?:data|history)|hasn'?t been (?:exported|loaded|recorded)|"
    r"not been (?:exported|loaded|recorded)|unknown)",
    re.IGNORECASE,
)
NO_DATA_RE = re.compile(
    r"\b(?:no data|no (?:[a-z]+ )?(?:watch(?:ing)? )?(?:time|minutes|activity|views?|data) "
    r"(?:is |are |was |were |has been |have been )?(?:recorded|logged)|"
    r"no (?:views?|records?|activity|watch(?:ing)? history) (?:recorded |yet |"
    r"for|logged|on)|no (?:watch )?(?:events?|views?|data|records?|activity) (?:for|on|today|"
    r"recorded|yet|logged)|(?:data|history|export) (?:ends?|stops?|finishes) (?:on|at)|"
    r"not (?:yet )?(?:in the (?:data|export|history)|available|included|"
    r"covered|been (?:exported|loaded|recorded))|(?:data|history|export) (?:only )?(?:ends?|"
    r"runs?|goes|covers?) (?:up )?(?:to|through|until)|latest (?:data|date|record|day)|"
    r"most recent (?:data|day|date)|up to yesterday|through yesterday|can'?t (?:tell|see|"
    r"know)|cannot (?:tell|see|know)|unknown|doesn'?t (?:include|cover|have)|"
    r"does not (?:include|cover|have)|isn'?t (?:in|available|included))",
    re.IGNORECASE,
)

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]  # fmt: skip
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _month_re(month: str) -> str:
    """A month name or its abbreviation ("Sep", "Sept", "Sep."), no capture group."""
    rest = "t|tember" if month == "september" else month[3:]
    return month[:3] + r"(?:" + rest + r")?\.?"


_MONTH_ALT = "|".join(_month_re(m) for m in MONTHS)
DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b"),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_ALT})(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b(?:{_MONTH_ALT})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
]
TIME_RE = re.compile(
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?|\b\d{1,2}\s*[ap]\.?m\.?\b", re.I
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUMBER_RE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.])-?\d+(?:\.\d+)?%?")
NOT_AN_ANSWER_RE = re.compile(
    r"\b\d+\s+(?:tables?|columns?|tools?|queries|query|steps?|results?|marts?)\b", re.I
)


UNIT_HINT_RE = re.compile(
    r"\s*(?P<u>hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class TextNumber:
    value: float
    decimals: int
    percent: bool
    unit_hint: str | None = None  # "hours" | "minutes" | "seconds" when a unit word follows
    pos: int = 0  # character offset in the normalised text


def _hint(word: str) -> str:
    w = word.lower()
    if w.startswith("h"):
        return "hours"
    if w.startswith("m"):
        return "minutes"
    return "seconds"


def normalise_text(text: str) -> str:
    """Typographic apostrophes and dashes to ASCII, so the patterns stay ASCII-only."""
    return (
        text.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .replace("\u00a0", " ")
    )


def strip_dates_and_times(text: str) -> str:
    """Blank out date ranges, dates, times, years and "N tables"-style counts (same length:
    offsets hold). A range is blanked whole, so the 9 in "Mon 9 - Sun 15 Nov" is not a number;
    2000 is a leap year, so a missing year never rejects 29 Feb."""
    out = text
    for m, _, _ in _date_ranges(text, 2000):
        out = out[: m.start()] + " " * (m.end() - m.start()) + out[m.end() :]
    for pattern in [*DATE_RES, TIME_RE, YEAR_RE, NOT_AN_ANSWER_RE]:
        out = pattern.sub(lambda m: " " * len(m.group(0)), out)
    return out


def numbers_in(text: str) -> list[TextNumber]:
    found = []
    clean = strip_dates_and_times(text)
    for m in NUMBER_RE.finditer(clean):
        raw = m.group(0)
        percent = raw.endswith("%")
        digits = raw.rstrip("%").replace(",", "")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        follow = UNIT_HINT_RE.match(clean, m.end())
        hint = _hint(follow.group("u")) if follow and not percent else None
        found.append(TextNumber(float(digits), decimals, percent, hint, m.start()))
    return found


# --------------------------------------------------------------------------- value matching


def _scales(unit: str) -> list[float]:
    """Factors f such that candidate * f is in the gold's unit."""
    return {
        "ratio": [1.0, 0.01],
        "minutes": [1.0, 1 / 60, 60.0],  # minutes, seconds, hours
        "seconds": [1.0, 60.0, 3600.0],  # seconds, minutes, hours
        "hours": [1.0, 1 / 60, 1 / 3600],
    }.get(unit, [1.0])


def _decimals_of(value: float) -> int:
    text = repr(float(value))
    if "e" in text or "." not in text:
        return 0
    frac = text.split(".")[1].rstrip("0")
    return len(frac)


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").rstrip("%"))
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


DURATION_TO_SECONDS = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}


def _within(
    gold: float, candidate: float, f: float, d: int, tolerance: float, explicit: bool = False
) -> bool:
    # Rounding slack only when the candidate is unscaled, shows decimals, or carries an explicit
    # unit word: a bare integer scaled by 60 or 3600 must not match a large gold by rounding.
    rounding = 0.5 * 10 ** (-d) * abs(f) if (f == 1.0 or d >= 1 or explicit) else 0.0
    tol = max(tolerance, rounding, 1e-9 * max(1.0, abs(gold)))
    return abs(candidate * f - gold) <= tol + 1e-12


def numeric_match(
    gold: float,
    candidate: float,
    unit: str,
    tolerance: float,
    decimals: int | None = None,
    scaled: bool = True,
) -> bool:
    """A query-result value equals ``gold`` within tolerance/rounding, at any supported scale
    (only unscaled when ``scaled`` is False).

    A whole-number cell gets no rounding slack against a ratio: 0 or 1 is not a rounded share,
    and with the usual half-unit slack it would match every share below or above one half.
    """
    d = _decimals_of(candidate) if decimals is None else decimals
    scales = _scales(unit) if scaled else [1.0]
    if unit == "ratio" and d == 0:
        tol = max(tolerance, 1e-9)
        return any(abs(candidate * f - gold) <= tol + 1e-12 for f in scales)
    return any(_within(gold, candidate, f, d, tolerance) for f in scales)


def text_number_match(gold: float, n: TextNumber, unit: str, tolerance: float) -> bool:
    """A number written in the answer equals ``gold``.

    Percentages only match ratios. For durations the following unit word decides the scale
    ("2.5 hours" vs a minutes gold); without one the number must already be in the gold unit.
    """
    if n.percent:
        return unit == "ratio" and _within(gold, n.value, 0.01, n.decimals, tolerance)
    if unit in DURATION_TO_SECONDS and n.unit_hint is not None:
        f = DURATION_TO_SECONDS[n.unit_hint] / DURATION_TO_SECONDS[unit]
        return _within(gold, n.value, f, n.decimals, tolerance, explicit=True)
    return _within(gold, n.value, 1.0, n.decimals, tolerance)


def _to_date_str(value: Any) -> str | None:
    if isinstance(value, datetime | date):
        return value.isoformat()[:10]
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def _dow_name(value: Any) -> str | None:
    if isinstance(value, str):
        v = value.strip().lower()
        for d in DAYS:
            if v == d or v == d[:3]:
                return d
        num = _as_float(v)
        value = num if num is not None else value
    if isinstance(value, int | float) and not isinstance(value, bool):
        n = int(value)
        if 1 <= n <= 7:
            return DAYS[n - 1]  # ISO: 1 = Monday
        if n == 0:
            return "sunday"  # DuckDB dayofweek: 0 = Sunday
    return None


def value_match(
    gold: Any, candidate: Any, unit: str, tolerance: float, scaled: bool = True
) -> bool:
    if unit == "date":
        return _to_date_str(gold) is not None and _to_date_str(gold) == _to_date_str(candidate)
    if unit == "dow":
        return _dow_name(gold) is not None and _dow_name(gold) == _dow_name(candidate)
    if unit == "text":
        return str(gold).strip().casefold() == str(candidate).strip().casefold()
    if unit == "hour_of_day":
        c = _as_float(candidate)
        if c is None and isinstance(candidate, str):
            m = re.match(r"^\s*(\d{1,2})", candidate)
            c = float(m.group(1)) if m else None
        return c is not None and int(c) == int(gold) and c == int(c)
    g, c = _as_float(gold), _as_float(candidate)
    if g is None or c is None:
        return False
    return numeric_match(g, c, unit, tolerance, scaled=scaled)


# --------------------------------------------------------------------------- result matching


@dataclass(frozen=True)
class ResultTable:
    columns: list[str]
    rows: list[list[Any]]


def label_columns(result: ResultTable) -> set[int]:
    """Columns that label rows rather than measure them: whole numbers, at least 3 rows, all
    distinct and together a run of consecutive integers (hour 0-23, day of week 1-7, rank).
    Their cells still match unscaled (hour 21 for "which hour"), but are never rescaled, so
    hour 17 is not read as 17% of a share."""
    labels: set[int] = set()
    rows = result.rows[:DERIVE_MAX_ROWS]
    if len(rows) < 3:
        return labels
    for j in range(len(result.columns)):
        values = [row[j] if j < len(row) else None for row in rows]
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            continue
        ints = [int(v) for v in values if v is not None]
        if len(set(ints)) == len(ints) and max(ints) - min(ints) + 1 == len(ints):
            labels.add(j)
    return labels


def _rows_match(gold_row: list[Any], cand_row: list[Any], mapping: tuple[int, ...],
                units: list[str], tol: float) -> bool:  # fmt: skip
    return all(
        value_match(g, cand_row[j], units[i], tol)
        for i, (g, j) in enumerate(zip(gold_row, mapping, strict=True))
    )


def table_match(q: Question, gold: GoldAnswer, result: ResultTable) -> bool:
    units: list[str] = list(q.units)
    k = len(gold.columns)
    if not result.rows or not result.columns or len(result.columns) < k:
        return False
    if q.answer_type == "scalar":
        first = result.rows[0]
        labels = label_columns(result)
        return any(
            value_match(gold.scalar, v, units[0], q.tolerance, scaled=j not in labels)
            for j, v in enumerate(first)
        )
    if q.answer_type == "table" and len(gold.rows) == 1 and _long_format_match(q, gold, result):
        return True
    for mapping in permutations(range(len(result.columns)), k):
        if q.answer_type == "ordered_table":
            if len(result.rows) < len(gold.rows):
                continue
            if all(_rows_match(g, c, mapping, units, q.tolerance)
                   for g, c in zip(gold.rows, result.rows, strict=False)):  # fmt: skip
                return True
        else:  # table: same number of rows, matched as a set
            if len(result.rows) != len(gold.rows):
                continue
            unused = list(range(len(result.rows)))
            ok = True
            for g in gold.rows:
                hit = next(
                    (i for i in unused if _rows_match(g, result.rows[i], mapping, units,
                                                      q.tolerance)), None)  # fmt: skip
                if hit is None:
                    ok = False
                    break
                unused.remove(hit)
            if ok:
                return True
    return False


def _long_format_match(q: Question, gold: GoldAnswer, result: ResultTable) -> bool:
    """A one-row gold (a, b) also matches a result that lists a and b in different rows,
    e.g. GROUP BY is_weekend giving one row per group. Each gold value needs its own cell."""
    used: set[tuple[int, int]] = set()
    for value, unit in zip(gold.rows[0], q.units, strict=True):
        hit = next(
            ((i, j) for i, row in enumerate(result.rows) for j, cell in enumerate(row)
             if (i, j) not in used and value_match(value, cell, unit, q.tolerance)),
            None,
        )  # fmt: skip
        if hit is None:
            return False
        used.add(hit)
    return True


DERIVE_MAX_ROWS = 100


def _numeric_grid(result: ResultTable) -> list[list[float | None]]:
    grid: list[list[float | None]] = []
    for row in result.rows[:DERIVE_MAX_ROWS]:
        grid.append([None if isinstance(v, bool) else _as_float(v) for v in row])
    return grid


Derivation = tuple[float, tuple[float, ...]]  # (value, the cells it was computed from)


def derivations(result: ResultTable) -> list[Derivation]:
    """Simple arithmetic an answer may do on top of a query result, with its operand cells.

    Contiguous range sums of a column (e.g. summing per-day rows into a weekend), and +, -,
    x, / of two cells in the same row or (difference, ratio) in the same column.
    """
    grid = _numeric_grid(result)
    if not grid:
        return []
    ncols = max(len(r) for r in grid)
    out: list[Derivation] = []

    def pair(a: float, b: float, ops: str) -> None:
        if "+" in ops:
            out.append((a + b, (a, b)))
        out.extend([(a - b, (a, b)), (b - a, (a, b))])
        if "*" in ops:
            out.append((a * b, (a, b)))
        if b:
            out.append((a / b, (a, b)))
        if a:
            out.append((b / a, (a, b)))

    for j in range(ncols):
        col = [r[j] if j < len(r) else None for r in grid]
        for i in range(len(col)):
            total, used = 0.0, []
            for k in range(i, len(col)):
                v = col[k]
                if v is None:
                    break
                total += v
                used.append(v)
                if k > i:
                    out.append((total, tuple(used)))
        vals = [v for v in col if v is not None]
        for a_i, a in enumerate(vals):
            for b in vals[a_i + 1 :]:
                pair(a, b, "")
    for row in grid:
        vals = [v for v in row if v is not None]
        for a_i, a in enumerate(vals):
            for b in vals[a_i + 1 :]:
                pair(a, b, "+*")
    return out


def derived_values(result: ResultTable) -> list[float]:
    return [v for v, _ in derivations(result)]


_MON = r"(?P<{name}>" + _MONTH_ALT + ")"
_WD = r"(?:(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday)?,?\s+)?"
_SEP = r"\s*(?:-|to|through|until|thru)\s*"
RANGE_RES = [
    # 2026-11-09 to (Sun) 2026-11-15
    re.compile(rf"(?P<a>\d{{4}}-\d{{2}}-\d{{2}}){_SEP}{_WD}(?P<b>\d{{4}}-\d{{2}}-\d{{2}})", re.I),
    # (Mon) 9 Nov (2026) to (Sun) 15 Nov (2026)
    re.compile(
        rf"{_WD}(?P<d1>\d{{1,2}})\s+{_MON.format(name='m1')}(?:\s+(?P<y1>\d{{4}}))?{_SEP}"
        rf"{_WD}(?P<d2>\d{{1,2}})\s+{_MON.format(name='m2')}(?:\s+(?P<y2>\d{{4}}))?",
        re.I,
    ),
    # 10-16 Nov (2026), (Mon) 9 - (Sun) 15 Nov (2026)
    re.compile(
        rf"\b{_WD}(?P<d1>\d{{1,2}}){_SEP}{_WD}(?P<d2>\d{{1,2}})\s+{_MON.format(name='m2')}"
        rf"(?:\s+(?P<y2>\d{{4}}))?",
        re.I,
    ),
    # Nov 10-16 (2026)
    re.compile(
        rf"{_MON.format(name='m2')}\s+(?P<d1>\d{{1,2}})\s*-\s*(?P<d2>\d{{1,2}})"
        rf"(?:,?\s+(?P<y2>\d{{4}}))?",
        re.I,
    ),
]


def _month_number(token: str) -> int:
    return [m[:3] for m in MONTHS].index(token.lower()[:3]) + 1


def _date_ranges(text: str, year: int) -> list[tuple[re.Match[str], date, date]]:
    """Every valid range match in ``text``; a missing year defaults to ``year``."""
    found = []
    for pattern in RANGE_RES:
        for m in pattern.finditer(text):
            g = m.groupdict()
            try:
                if g.get("a"):
                    start, end = date.fromisoformat(g["a"]), date.fromisoformat(g["b"])
                else:
                    m2 = _month_number(g["m2"])
                    m1 = _month_number(g["m1"]) if g.get("m1") else m2
                    y2 = int(g["y2"]) if g.get("y2") else year
                    y1 = int(g["y1"]) if g.get("y1") else y2
                    start, end = date(y1, m1, int(g["d1"])), date(y2, m2, int(g["d2"]))
            except (ValueError, KeyError):
                continue
            if start <= end:
                found.append((m, start, end))
    return found


def stated_date_ranges(text: str, today: date) -> list[tuple[date, date]]:
    """Explicit date ranges an answer states ("2026-11-09 to 2026-11-15", "10-16 Nov 2026",
    "Mon 9 Nov to Sun 15 Nov", "Mon 9 - Sun 15 Nov", "Nov 10-16"). Missing years default to
    ``today``'s year."""
    found: list[tuple[date, date]] = []
    for _, start, end in _date_ranges(text, today.year):
        if (start, end) not in found:
            found.append((start, end))
    return found


def range_sum_match(
    g: float,
    unit: str,
    tolerance: float,
    result: ResultTable,
    ranges: list[tuple[date, date]],
    text_nums: list[TextNumber],
) -> bool:
    """A gold total equals the sum of the cited rows inside a date range the answer states.

    Safeguard: only rows whose date (a date column of the result) falls inside the stated
    range are summed, and that sum must match both a total the text states and the gold.
    """
    if not ranges or not result.rows:
        return False
    date_cols = [
        j for j in range(len(result.columns))
        if all(_to_date_str(row[j]) for row in result.rows if j < len(row))
    ]  # fmt: skip
    if not date_cols:
        return False
    dj = date_cols[0]
    grid = _numeric_grid(result)
    for start, end in ranges:
        inside = [
            i for i, row in enumerate(result.rows[:DERIVE_MAX_ROWS])
            if start <= date.fromisoformat(str(_to_date_str(row[dj]))) <= end
        ]  # fmt: skip
        if not inside:
            continue
        for j in range(len(result.columns)):
            if j == dj:
                continue
            values = [grid[i][j] if j < len(grid[i]) else None for i in inside]
            if any(v is None for v in values):
                continue
            total = float(sum(v for v in values if v is not None))
            if numeric_match(g, total, unit, tolerance, decimals=6) and _stated(
                total, text_nums, unit, tolerance
            ):
                return True
    return False


def _flag_columns(result: ResultTable) -> list[int]:
    """Boolean group columns (is_late_night), or whole-number columns holding only 0 and 1."""
    rows = result.rows[:DERIVE_MAX_ROWS]
    flags = []
    for j in range(len(result.columns)):
        values = [row[j] if j < len(row) else None for row in rows]
        booleans = all(isinstance(v, bool) for v in values) and len(set(values)) == 2
        zero_one = all(type(v) is int for v in values) and set(values) == {0, 1}
        if booleans or zero_one:
            flags.append(j)
    return flags


def group_shares(result: ResultTable) -> list[float]:
    """Shares of a column total held by one side of a flag column, as in a GROUP BY
    is_late_night result: sum(col where flag) / sum(col). Few candidates per result (two per
    flag and numeric column), and ``derived_match`` also needs the text to state the share."""
    grid = _numeric_grid(result)
    rows = result.rows[:DERIVE_MAX_ROWS]
    labels = label_columns(result)
    shares: list[float] = []
    flags = _flag_columns(result)
    for fj in flags:
        for j in range(len(result.columns)):
            if j == fj or j in labels or j in flags:
                continue
            column = [grid[i][j] if j < len(grid[i]) else None for i in range(len(grid))]
            if any(v is None for v in column):
                continue
            total = float(sum(v for v in column if v is not None))
            if total <= 0:
                continue
            for side in {bool(row[fj]) for row in rows}:
                part = sum(v for v, row in zip(column, rows, strict=True)
                           if v is not None and bool(row[fj]) == side)  # fmt: skip
                shares.append(part / total)
    return shares


def _stated(value: float, text_nums: list[TextNumber], unit: str, tolerance: float) -> bool:
    units = (unit, "number") if unit != "number" else ("number",)
    return any(text_number_match(value, n, u, tolerance) for n in text_nums for u in units)


def derived_match(
    q: Question,
    gold: GoldAnswer,
    result: ResultTable,
    text_nums: list[TextNumber],
    ranges: list[tuple[date, date]] | None = None,
) -> bool:
    """Every gold value (scalar or one-row table) is a cell of, or derived from, ``result``.

    A derived value counts only when the answer shows the arithmetic: the text states the
    derived value *and* every cell it was computed from. So "Saturday 293 + Sunday 186 =
    about 479 min" counts, while two unrelated cells whose difference happens to equal the
    gold (8.1 - 3.9 = 4.2 vs 4.18) do not. A sum over many dated rows also counts when the
    answer states the total and its date range (``range_sum_match``). A share of a group
    (``group_shares``) counts when the text states it.
    """
    if q.answer_type not in ("scalar", "table") or len(gold.rows) != 1:
        return False
    labels = label_columns(result)
    cells = [
        (v, j not in labels) for row in _numeric_grid(result) for j, v in enumerate(row)
        if v is not None
    ]  # fmt: skip
    derived = derivations(result)
    for value, unit in zip(gold.rows[0], q.units, strict=True):
        g = _as_float(value)
        if g is None or unit in NON_NUMERIC_UNITS:
            if not any(value_match(value, c, unit, q.tolerance) for r in result.rows for c in r):
                return False
            continue
        if any(numeric_match(g, c, unit, q.tolerance, scaled=s) for c, s in cells):
            continue
        if any(
            numeric_match(g, d, unit, q.tolerance, decimals=6)
            and _stated(d, text_nums, unit, q.tolerance)
            and all(_stated(o, text_nums, "number", 0.0) for o in operands)
            for d, operands in derived
        ):
            continue
        if range_sum_match(g, unit, q.tolerance, result, ranges or [], text_nums):
            continue
        if unit == "ratio" and any(
            numeric_match(g, sh, unit, q.tolerance, decimals=6)
            and _stated(sh, text_nums, unit, q.tolerance)
            for sh in group_shares(result)
        ):
            continue
        return False
    return True


# --------------------------------------------------------------------------- text matching


def _date_patterns(iso: str) -> list[re.Pattern[str]]:
    d = date.fromisoformat(iso[:10])
    mon = _month_re(MONTHS[d.month - 1])
    day = rf"0?{d.day}(?:st|nd|rd|th)?"
    return [
        re.compile(rf"\b{d.isoformat()}\b"),
        re.compile(rf"\b{day}\s+{mon}\b", re.I),
        re.compile(rf"\b{mon}\s+{day}\b", re.I),
    ]


def _hour_patterns(hour: int) -> list[re.Pattern[str]]:
    h12 = hour % 12 or 12
    ampm = "a" if hour < 12 else "p"
    return [
        re.compile(rf"\b0?{hour}:00\b"),
        re.compile(rf"\b0?{hour}(?:h|:\d\d)?\s*(?:-|to)\s*0?{(hour + 1) % 24}"),
        re.compile(rf"\b{h12}(?::00)?\s*{ampm}\.?m\b", re.I),
        re.compile(rf"\bhour\s+(?:of\s+)?0?{hour}\b", re.I),
        re.compile(rf"\b0?{hour}h\b", re.I),
    ]


def text_states(text: str, value: Any, unit: str, tolerance: float) -> bool:
    if unit == "date":
        iso = _to_date_str(value)
        return iso is not None and any(p.search(text) for p in _date_patterns(iso))
    if unit == "dow":
        name = _dow_name(value)
        return (
            name is not None
            and re.search(rf"\b{name[:3]}(?:{name[3:]})?s?\b", text, re.I) is not None
        )
    if unit == "text":
        return str(value).strip().casefold() in text.casefold()
    if unit == "hour_of_day":
        return any(p.search(text) for p in _hour_patterns(int(value)))
    g = _as_float(value)
    if g is None:
        return False
    return any(text_number_match(g, n, unit, tolerance) for n in numbers_in(text))


NON_NUMERIC_UNITS = ("date", "dow", "text", "hour_of_day")


def states_wrong_value(text: str, q: Question, readings: list[GoldAnswer]) -> float | None:
    """A wrong-definition value the answer leads with, or None.

    A wrong value counts only if it is stated *before* the first unambiguous mention of the
    gold value it competes with (same column unit), or the gold value is not stated at all.
    So "11,521 different videos, from 11,885 total views" is fine (context), while
    "the longest ran 62 minutes ... estimated 35 minutes" fails (it leads with the wrong one).
    A number that matches both a gold and a wrong value is ambiguous and ignored.
    """
    nums = numbers_in(text)
    all_golds = [
        (float(v), q.units[i % len(q.units)])
        for g in readings for row in g.rows for i, v in enumerate(row)
        if _as_float(v) is not None and q.units[i % len(q.units)] not in NON_NUMERIC_UNITS
    ]  # fmt: skip
    for wrong in q.wrong_values:
        for u in [u for u in q.units if u not in NON_NUMERIC_UNITS] or ["number"]:
            competing = [g for g, gu in all_golds if gu == u]

            def is_gold(n: TextNumber, golds: list[float] = competing, unit: str = u) -> bool:
                return any(text_number_match(g, n, unit, q.tolerance) for g in golds)

            def is_wrong(n: TextNumber, unit: str = u, w: float = wrong) -> bool:
                return text_number_match(w, n, unit, q.tolerance)

            wrong_pos = [n.pos for n in nums if is_wrong(n) and not is_gold(n)]
            if not wrong_pos:
                continue
            gold_pos = [n.pos for n in nums if is_gold(n) and not is_wrong(n)]
            if not gold_pos or min(wrong_pos) < min(gold_pos):
                return wrong
    return None


def stated_amounts(text: str, question: str) -> list[float]:
    """Numbers in ``text`` that look like answers (not dates/years/times or question numbers)."""
    in_question = {n.value for n in numbers_in(question)}
    return [n.value for n in numbers_in(text) if n.value not in in_question]


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
GROUNDING_UNITS = ("number", "minutes", "seconds", "ratio")


def grounded(
    n: TextNumber, results: list[ResultTable], extra_values: list[float] | None = None
) -> bool:
    """``n`` is a cell of, simple arithmetic on, or the row count of a query result, or a
    value a describe_table call returned (row count, column statistics, sample cells)."""
    for v in extra_values or []:
        if any(text_number_match(v, n, unit, 0.0) for unit in GROUNDING_UNITS):
            return True
    for table in results:
        values = [v for row in _numeric_grid(table) for v in row if v is not None]
        values += derived_values(table)
        values.append(float(len(table.rows)))
        for v in values:
            if any(text_number_match(v, n, unit, 0.0) for unit in GROUNDING_UNITS):
                return True
    return False


def refusal_ungrounded_numbers(
    text: str, question: str, results: list[ResultTable], extra_values: list[float] | None = None
) -> list[float] | None:
    """For an unanswerable question: the numbers that make the refusal fail.

    - No numbers at all: [] (a refusal phrase anywhere is enough).
    - Numbers stated: the answer must *lead* with a refusal (a refusal phrase in the first
      sentence, which itself states no number); every later number must be grounded in a
      query result of the run (a cell, simple arithmetic as in execution scoring, or a row
      count). Returns the offending numbers, or None when the refusal does not lead.
    """
    in_question = {n.value for n in numbers_in(question)}
    nums = [n for n in numbers_in(text) if n.value not in in_question]
    if not nums:
        return []
    sentences = [x for x in SENTENCE_SPLIT_RE.split(text) if x.strip()]
    first = sentences[0] if sentences else ""
    first_amounts = stated_amounts(first, question)
    if not REFUSAL_RE.search(first) or first_amounts:
        return first_amounts or [n.value for n in nums]
    rest = text[len(first) :]
    return [n.value for n in numbers_in(rest) if n.value not in in_question
            and not grounded(n, results, extra_values)]  # fmt: skip


TODAY_RE = re.compile(
    r"\b(?:today|so far|this morning|this afternoon|this evening|tonight)\b", re.I
)
OTHER_DAY_RE = re.compile(
    r"\b(?:yesterday|the day before|day before|that day|previous day|last day|latest day|"
    r"most recent day|last full day|last week|earlier)\b",
    re.I,
)


def amounts_about_day(text: str, question: str, day: date) -> list[float]:
    """Amounts stated as the answer for ``day`` (no_data questions).

    Numbers count unless their sentence is clearly about another day ("yesterday", "that
    day", "the day before", or a date other than ``day``) and not about today. So "no data
    for today; yesterday you watched 103 minutes" states no amount for today and passes,
    while "you watched 0 minutes (today)" and a bare "You watched 12 minutes." fail.
    """
    today_patterns = [TODAY_RE, *_date_patterns(day.isoformat())]
    out: list[float] = []
    for sentence in SENTENCE_SPLIT_RE.split(text):
        about_today = any(p.search(sentence) for p in today_patterns)
        other_date = any(p.search(_strip_day(sentence, day)) for p in DATE_RES[:3])
        about_other = bool(OTHER_DAY_RE.search(sentence)) or other_date
        if about_other and not about_today:
            continue
        out += stated_amounts(sentence, question)
    return out


def _strip_day(sentence: str, day: date) -> str:
    for p in _date_patterns(day.isoformat()):
        sentence = p.sub(" ", sentence)
    return sentence


# --------------------------------------------------------------------------- scoring


@dataclass
class AgentOutput:
    status: str
    text: str = ""
    result_ids: list[str] = field(default_factory=list)
    metrics_used: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    results: dict[str, ResultTable] = field(default_factory=dict)  # cited results only
    # Every successful run_sql result of the run (cited or not); used to ground numbers in
    # refusals. Falls back to ``results`` when empty.
    all_results: dict[str, ResultTable] = field(default_factory=dict)
    # Numbers returned by describe_table (row counts, column stats, sample cells).
    tool_values: list[float] = field(default_factory=list)


def describe_table_values(tool_calls: list[dict[str, Any]]) -> list[float]:
    """Numeric values from the run's describe_table outputs (as stored in its trace)."""
    import json

    values: list[float] = []
    for call in tool_calls:
        if call.get("name") != "describe_table" or call.get("is_error"):
            continue
        try:
            payload = json.loads(call.get("output", ""))
        except json.JSONDecodeError:
            continue  # truncated in the trace
        if isinstance(payload.get("row_count"), int | float):
            values.append(float(payload["row_count"]))
        for stat in payload.get("column_stats", []):
            for key in ("min", "max", "distinct"):
                v = _as_float(stat.get(key)) if not isinstance(stat.get(key), bool) else None
                if v is not None:
                    values.append(v)
        for row in payload.get("sample_rows", []):
            values += [v for v in (_as_float(c) for c in row if not isinstance(c, bool)) if v]
    return values


@dataclass
class Score:
    passed: bool
    execution: bool | None = None  # strict or derived
    execution_strict: bool | None = None  # the gold is literally in a cited result
    answer: bool | None = None
    gold_stated: bool = False  # the text states the gold value(s) of some reading
    metric_selection: bool | None = None
    refusal: bool | None = None
    caveat: bool | None = None
    caveat_strong: bool | None = None  # estimate + reason; reported only
    assumptions: bool | None = None
    reading: int | None = None  # 0 = gold, 1.. = alternative index
    wrong_value: float | None = None
    refused: bool = False
    fabricated: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _norm_metric(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def score(q: Question, gold: QuestionGold, out: AgentOutput) -> Score:
    text = normalise_text(out.text or "")
    caveat = bool(ESTIMATE_RE.search(text) or COVERAGE_RE.search(text))
    strong = bool(STRONG_ESTIMATE_RE.search(text) and ESTIMATE_REASON_RE.search(text))
    refused = bool(REFUSAL_RE.search(text))
    metric_sel = None
    if q.expected_metrics:
        used = {_norm_metric(m) for m in out.metrics_used}
        metric_sel = all(_norm_metric(m) in used for m in q.expected_metrics)

    if out.status != "answered":
        return Score(passed=False, execution=False, answer=False, metric_selection=metric_sel,
                     caveat=False, caveat_strong=False,
                     notes=[f"status {out.status}"])  # fmt: skip

    if q.answer_type == "refusal":
        ungrounded = refusal_ungrounded_numbers(
            text, q.question, list((out.all_results or out.results).values()), out.tool_values
        )
        ok = refused and ungrounded == []
        return Score(passed=ok, refusal=ok, caveat=caveat, caveat_strong=strong,
                     refused=refused, fabricated=ungrounded or [],
                     metric_selection=None)  # fmt: skip

    if q.answer_type == "no_data":
        says_no_data = bool(NO_DATA_RE.search(text))
        amounts = amounts_about_day(text, q.question, q.today)
        ok = says_no_data and not amounts
        notes = [] if says_no_data else ["does not say there is no data"]
        if amounts:
            notes.append(f"states an amount {amounts}")
        return Score(passed=ok, refusal=ok, caveat=says_no_data, caveat_strong=strong,
                     refused=refused, fabricated=amounts, notes=notes)  # fmt: skip

    readings = gold.all()
    text_nums = numbers_in(text)
    # Date ranges may be stated in the text or the assumptions (both are part of the answer).
    ranges = stated_date_ranges(normalise_text(" ".join([text, *out.assumptions])), q.today)
    cited = [out.results[r] for r in out.result_ids if r in out.results]
    execution = False
    execution_strict = False
    answer = False
    same_reading = False
    reading_idx: int | None = None
    exec_reading: int | None = None
    for idx, g in enumerate(readings):
        strict_ok = any(table_match(q, g, r) for r in cited)
        execution_strict = execution_strict or strict_ok
        exec_ok = strict_ok or any(derived_match(q, g, r, text_nums, ranges) for r in cited)
        values = [g.scalar] if q.answer_type == "scalar" else [v for row in g.rows for v in row]
        units = (
            [q.units[0]]
            if q.answer_type == "scalar"
            else [q.units[i % len(q.units)] for row in g.rows for i in range(len(row))]
        )
        text_ok = all(text_states(text, v, u, q.tolerance)
                      for v, u in zip(values, units, strict=True))  # fmt: skip
        if exec_ok and exec_reading is None:
            exec_reading = idx
        if exec_ok and text_ok:
            execution, answer, reading_idx, same_reading = True, True, idx, True
            break
        execution = execution or exec_ok
        answer = answer or text_ok
        if reading_idx is None and text_ok:
            reading_idx = idx
    if reading_idx is None:
        reading_idx = exec_reading

    gold_stated = answer
    wrong = states_wrong_value(text, q, readings)
    notes = []
    if wrong is not None:
        answer = False
        notes.append(f"leads with wrong-definition value {wrong}")

    has_assumptions = bool([a for a in out.assumptions if a.strip()])
    # Execution and text must agree on one reading (gold or one alternative).
    passed = same_reading and wrong is None
    if q.category == "ambiguous":
        passed = passed and has_assumptions
    if q.requires_caveat:
        passed = passed and caveat
    return Score(
        passed=passed,
        execution=execution,
        execution_strict=execution_strict,
        answer=answer,
        gold_stated=gold_stated,
        metric_selection=metric_sel,
        caveat=caveat,
        caveat_strong=strong,
        assumptions=has_assumptions if q.category == "ambiguous" else None,
        reading=reading_idx,
        wrong_value=wrong,
        refused=refused,
        notes=notes,
    )
