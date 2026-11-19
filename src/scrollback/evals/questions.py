"""The eval question set, ``eval/questions.yaml`` (DESIGN §7.1).

The file pins its own parameters (``params:``) so gold answers never drift when
``config.yaml`` is tuned on real data, and records the dev/test split per question.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scrollback.config import MetricsConfig, TimezoneConfig

DEFAULT_QUESTIONS_PATH = Path(__file__).resolve().parents[3] / "eval" / "questions.yaml"

Category = Literal[
    "simple_agg", "time_window", "relative_date", "metric_definition", "comparison", "ranking",
    "session_derived", "multi_step", "ambiguous", "unanswerable", "trap",
]  # fmt: skip
CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]
# no_data: the right answer is "there is no data for that"; any stated amount fails.
AnswerType = Literal["scalar", "table", "ordered_table", "refusal", "no_data"]
# Units drive tolerant matching: ratio also matches a percentage, durations match in
# seconds/minutes/hours, dates/hours/days of week match in several spellings.
Unit = Literal[
    "count", "ratio", "seconds", "minutes", "hours", "days", "date", "hour_of_day", "dow",
    "text", "number",
]  # fmt: skip
Label = Literal[
    "wrong_metric_definition", "wrong_time_window", "timezone_or_day_boundary",
    "hallucinated_table_or_column", "join_or_grain_error", "aggregation_error",
    "sql_error_unrecovered", "answer_misreport", "missing_caveat", "failed_refusal",
    "over_refusal", "tool_loop_or_step_limit",
]  # fmt: skip
LABELS: tuple[str, ...] = Label.__args__  # type: ignore[attr-defined]
MIN_PER_CATEGORY = 6


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvalParams(_Strict):
    """Warehouse parameters the gold answers were computed with."""

    timezone: TimezoneConfig
    metrics: MetricsConfig


class Question(_Strict):
    id: str = Field(pattern=r"^q\d{3}$")
    question: str
    category: Category
    difficulty: Literal["easy", "medium", "hard"]
    split: Literal["dev", "test"]
    today: date
    tests: str  # one line: what this question checks
    answer_type: AnswerType
    gold_sql: str | None = None
    alternatives: list[str] = Field(default_factory=list)  # other acceptable readings
    units: list[Unit] = Field(default_factory=lambda: list[Unit](["number"]))  # per column
    tolerance: float = Field(0.0, ge=0)  # absolute, in the gold's own units
    expected_metrics: list[str] = Field(default_factory=list)
    requires_caveat: bool = False
    wrong_values: list[float] = Field(default_factory=list)  # wrong-definition numbers
    likely_failure: Label | None = None  # hint for label suggestion
    expect_empty: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Question:
        if self.answer_type in ("refusal", "no_data"):
            if self.gold_sql or self.alternatives:
                raise ValueError(f"{self.id}: {self.answer_type} questions have no gold_sql")
        elif not self.gold_sql:
            raise ValueError(f"{self.id}: gold_sql is required")
        if (self.category == "unanswerable") != (self.answer_type == "refusal"):
            raise ValueError(f"{self.id}: unanswerable <=> answer_type refusal")
        if self.alternatives and self.category != "ambiguous":
            raise ValueError(f"{self.id}: alternatives are only for ambiguous questions")
        if self.answer_type == "no_data" and self.category != "trap":
            raise ValueError(f"{self.id}: no_data is only used for trap questions")
        if self.category == "trap" and not self.requires_caveat:
            raise ValueError(f"{self.id}: trap questions require a caveat")
        if self.answer_type == "scalar" and len(self.units) != 1:
            raise ValueError(f"{self.id}: a scalar has exactly one unit")
        return self


class QuestionSet(_Strict):
    version: int
    params: EvalParams
    questions: list[Question]

    @model_validator(mode="after")
    def _set_checks(self) -> QuestionSet:
        ids = [q.id for q in self.questions]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate question ids {dupes}")
        texts = [" ".join(q.question.lower().split()) for q in self.questions]
        dupe_texts = sorted({t for t in texts if texts.count(t) > 1})
        if dupe_texts:
            raise ValueError(f"duplicate question texts {dupe_texts}")
        per_cat: Counter[str] = Counter(q.category for q in self.questions)
        thin = sorted(c for c in CATEGORIES if per_cat[c] < MIN_PER_CATEGORY)
        if thin:
            raise ValueError(f"categories with < {MIN_PER_CATEGORY} questions: {thin}")
        for cat in CATEGORIES:
            splits = {q.split for q in self.questions if q.category == cat}
            if splits != {"dev", "test"}:
                raise ValueError(f"category {cat} must have dev and test questions")
        return self

    def by_split(self, split: str) -> list[Question]:
        if split == "all":
            return list(self.questions)
        return [q for q in self.questions if q.split == split]

    def get(self, qid: str) -> Question:
        for q in self.questions:
            if q.id == qid:
                return q
        raise KeyError(qid)

    @property
    def anchors(self) -> list[date]:
        return sorted({q.today for q in self.questions})


def load_questions(path: Path = DEFAULT_QUESTIONS_PATH) -> QuestionSet:
    with path.open(encoding="utf-8") as fh:
        return QuestionSet.model_validate(yaml.safe_load(fh))
