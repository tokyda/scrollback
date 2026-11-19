"""Deterministic scoring of one agent answer against its gold (DESIGN §7.2).

Scores per question run:
  execution    a result the answer cites matches the gold (any acceptable reading)
  answer       the final text states the gold value(s) and no wrong-definition value
  metric_sel   metrics_used contains expected_metrics (reported only; E0 uses free text)
  refusal      unanswerable: refused, with no made-up number
  caveat       says the number is an estimate / the data is partial or missing
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
    r"\b(?:estimat\w*|approximat\w*|roughly|not exact\w*|isn'?t exact|no exact|"
    r"can'?t be exact|not (?:directly )?measured|inferred|capped|cap(?:s|ped)? at|"
    r"cap of|upper (?:bound|limit)|based on (?:the )?(?:gaps?|time between|timing)|"
    r"gaps? between|only records? (?:when|the start)|(?:doesn|does not)'?t? record how long|"
    r"no (?:watch(?:-| )?)?duration|true duration|actual duration)",
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
    r"\b(?:no data|no (?:views?|records?|activity|watch(?:ing)? history) (?:recorded |yet |"
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
_MONTH_ALT = "|".join(m[:3] + r"(?:" + m[3:] + r")?\.?" for m in MONTHS)
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
    """Blank out dates, times, years and "N tables"-style counts (same length: offsets hold)."""
    out = text
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
    gold: float, candidate: float, unit: str, tolerance: float, decimals: int | None = None
) -> bool:
    """A query-result value (any supported scale) equals ``gold`` within tolerance/rounding."""
    d = _decimals_of(candidate) if decimals is None else decimals
    return any(_within(gold, candidate, f, d, tolerance) for f in _scales(unit))


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


def value_match(gold: Any, candidate: Any, unit: str, tolerance: float) -> bool:
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
    return numeric_match(g, c, unit, tolerance)


# --------------------------------------------------------------------------- result matching


@dataclass(frozen=True)
class ResultTable:
    columns: list[str]
    rows: list[list[Any]]


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
        return any(value_match(gold.scalar, v, units[0], q.tolerance) for v in first)
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


def _stated(value: float, text_nums: list[TextNumber], unit: str, tolerance: float) -> bool:
    units = (unit, "number") if unit != "number" else ("number",)
    return any(text_number_match(value, n, u, tolerance) for n in text_nums for u in units)


def derived_match(
    q: Question, gold: GoldAnswer, result: ResultTable, text_nums: list[TextNumber]
) -> bool:
    """Every gold value (scalar or one-row table) is a cell of, or derived from, ``result``.

    A derived value counts only when the answer shows the arithmetic: the text states the
    derived value *and* every cell it was computed from. So "Saturday 293 + Sunday 186 =
    about 479 min" counts, while two unrelated cells whose difference happens to equal the
    gold (8.1 - 3.9 = 4.2 vs 4.18) do not.
    """
    if q.answer_type not in ("scalar", "table") or len(gold.rows) != 1:
        return False
    cells = [v for row in _numeric_grid(result) for v in row if v is not None]
    derived = derivations(result)
    for value, unit in zip(gold.rows[0], q.units, strict=True):
        g = _as_float(value)
        if g is None or unit in NON_NUMERIC_UNITS:
            if not any(value_match(value, c, unit, q.tolerance) for r in result.rows for c in r):
                return False
            continue
        if any(numeric_match(g, c, unit, q.tolerance) for c in cells):
            continue
        if not any(
            numeric_match(g, d, unit, q.tolerance, decimals=6)
            and _stated(d, text_nums, unit, q.tolerance)
            and all(_stated(o, text_nums, "number", 0.0) for o in operands)
            for d, operands in derived
        ):
            return False
    return True


# --------------------------------------------------------------------------- text matching


def _date_patterns(iso: str) -> list[re.Pattern[str]]:
    d = date.fromisoformat(iso[:10])
    month = MONTHS[d.month - 1]
    mon = rf"{month[:3]}(?:{month[3:]})?\.?"
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
    refused = bool(REFUSAL_RE.search(text))
    metric_sel = None
    if q.expected_metrics:
        used = {_norm_metric(m) for m in out.metrics_used}
        metric_sel = all(_norm_metric(m) in used for m in q.expected_metrics)

    if out.status != "answered":
        return Score(passed=False, execution=False, answer=False, metric_selection=metric_sel,
                     caveat=False, notes=[f"status {out.status}"])  # fmt: skip

    if q.answer_type == "refusal":
        ungrounded = refusal_ungrounded_numbers(
            text, q.question, list((out.all_results or out.results).values()), out.tool_values
        )
        ok = refused and ungrounded == []
        return Score(passed=ok, refusal=ok, caveat=caveat, refused=refused,
                     fabricated=ungrounded or [], metric_selection=None)  # fmt: skip

    if q.answer_type == "no_data":
        says_no_data = bool(NO_DATA_RE.search(text))
        amounts = amounts_about_day(text, q.question, q.today)
        ok = says_no_data and not amounts
        notes = [] if says_no_data else ["does not say there is no data"]
        if amounts:
            notes.append(f"states an amount {amounts}")
        return Score(passed=ok, refusal=ok, caveat=says_no_data, refused=refused,
                     fabricated=amounts, notes=notes)  # fmt: skip

    readings = gold.all()
    text_nums = numbers_in(text)
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
        exec_ok = strict_ok or any(derived_match(q, g, r, text_nums) for r in cited)
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
        assumptions=has_assumptions if q.category == "ambiguous" else None,
        reading=reading_idx,
        wrong_value=wrong,
        refused=refused,
        notes=notes,
    )
