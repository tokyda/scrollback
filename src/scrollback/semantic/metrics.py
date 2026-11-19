"""Load and validate ``knowledge/metrics.yml`` (DESIGN §5).

This module owns the schema of the metrics layer and turns a metric into runnable SQL for the
simple cases (whole range or per day). The fuller compiler for the agent's ``query_metric``
tool (grains, filters, group-by) builds on it in Phase 5 (``semantic/compile.py``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scrollback.config import MetricsConfig, Settings

DEFAULT_METRICS_PATH = Path(__file__).resolve().parents[3] / "knowledge" / "metrics.yml"
MART_TABLES = frozenset(
    {"fct_views", "fct_sessions", "fct_likes", "fct_searches", "dim_date", "agg_daily"}
)
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
# Names usable as {placeholders} and in a metric's `params`.
PARAM_NAMES = frozenset({*MetricsConfig.model_fields, "local_tz", "source_tz"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WatchTimeMethod(_Strict):
    name: str
    version: int
    doc: str
    summary: str


class Metric(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    description: str
    type: Literal["simple", "query"]
    sql: str
    table: str
    time_column: str = "local_date"
    default_grain: Literal["day", "week", "month"] = "day"
    unit: Literal["count", "minutes", "ratio", "days"]
    params: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)

    @field_validator("table")
    @classmethod
    def _mart_only(cls, value: str) -> str:
        if value not in MART_TABLES:
            raise ValueError(f"table must be a mart ({sorted(MART_TABLES)}), got {value!r}")
        return value

    @field_validator("params")
    @classmethod
    def _known_params(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - PARAM_NAMES)
        if unknown:
            raise ValueError(f"unknown params {unknown}")
        return value

    @model_validator(mode="after")
    def _sql_shape(self) -> Metric:
        sql = self.sql.strip()
        if ";" in sql:
            raise ValueError(f"{self.name}: sql must be a single statement without ';'")
        if self.type == "simple" and re.search(r"\b(select|from)\b", sql, re.IGNORECASE):
            raise ValueError(f"{self.name}: a simple metric's sql is an aggregate expression only")
        if self.type == "query":
            missing = [p for p in ("$start", "$end") if p not in sql]
            if missing:
                raise ValueError(f"{self.name}: query metric must use {missing}")
        return self


class MetricsFile(_Strict):
    version: int
    watch_time_method: WatchTimeMethod
    caveats: dict[str, str]
    metrics: list[Metric]

    @model_validator(mode="after")
    def _cross_checks(self) -> MetricsFile:
        names = [m.name for m in self.metrics]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate metric names {duplicates}")
        for metric in self.metrics:
            unknown = sorted(set(metric.caveats) - set(self.caveats))
            if unknown:
                raise ValueError(f"{metric.name}: unknown caveats {unknown}")
        return self

    def get(self, name: str) -> Metric:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        raise KeyError(name)

    def texts(self) -> list[str]:
        """Every free-text field, for placeholder checks."""
        out = [self.watch_time_method.summary, *self.caveats.values()]
        for m in self.metrics:
            out += [m.label, m.description]
        return out


def load_metrics(path: Path = DEFAULT_METRICS_PATH) -> MetricsFile:
    with path.open(encoding="utf-8") as fh:
        return MetricsFile.model_validate(yaml.safe_load(fh))


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER_RE.findall(text))


def param_values(settings: Settings) -> dict[str, object]:
    values: dict[str, object] = dict(settings.metrics.model_dump())
    values["local_tz"] = settings.timezone.local_tz
    values["source_tz"] = settings.timezone.source_tz
    return values


def render(text: str, settings: Settings) -> str:
    """Fill ``{param}`` placeholders with the configured values."""
    values = param_values(settings)
    return PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), text)


def metric_query(metric: Metric, by_day: bool = False) -> str:
    """SQL for ``metric`` over ``$start``..``$end``, as one row or one row per day."""
    if metric.type == "query":
        if by_day:
            raise ValueError(f"{metric.name} is a query metric; per-day series not supported")
        return metric.sql
    where = f"WHERE {metric.time_column} BETWEEN $start AND $end"
    if by_day:
        return (
            f"SELECT {metric.time_column} AS local_date, {metric.sql} AS value "
            f"FROM {metric.table} {where} GROUP BY 1 ORDER BY 1"
        )
    return f"SELECT {metric.sql} AS value FROM {metric.table} {where}"
