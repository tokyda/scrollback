"""Agent tools v0: describe_table, run_sql, make_chart, final_answer (DESIGN §6.2).

Deliberately minimal for the E0 baseline: no search_knowledge and no query_metric, and
describe_table returns no column descriptions unless ``agent.describe_include_descriptions``
is on (E1).

Every tool returns a :class:`ToolOutcome`; errors are returned to the model as text with a
stable code (``[code] message``) so it can correct itself, and are recorded in the trace.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from scrollback.agent.sql_guard import AGGREGATED_TABLE_RE, SqlGuardError, guard_sql
from scrollback.config import Settings

MART_TABLES: tuple[str, ...] = (
    "fct_views",
    "fct_sessions",
    "fct_likes",
    "fct_searches",
    "dim_date",
    "agg_daily",
)
MAX_TOOL_OUTPUT_CHARS = 30_000


# --------------------------------------------------------------------------- schemas


def _strict(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _strict(
        "describe_table",
        "Show the columns and types of one table, with sample rows or column statistics.",
        {"table": {"type": "string", "enum": list(MART_TABLES)}},
    ),
    _strict(
        "run_sql",
        "Run one read-only DuckDB SELECT against the tables and return the rows (at most 200) "
        "with a result_id. Errors come back as text; fix the query and try again.",
        {"sql": {"type": "string", "description": "A single SELECT or WITH query."}},
    ),
    _strict(
        "make_chart",
        "Draw a line or bar chart from a previous run_sql result. Returns a chart_id.",
        {
            "result_id": {"type": "string", "description": "result_id from run_sql, e.g. r1"},
            "kind": {"type": "string", "enum": ["line", "bar"]},
            "x": {"type": "string", "description": "Column for the x axis."},
            "y": {"type": "string", "description": "Numeric column for the y axis."},
            "title": {"type": "string"},
        },
    ),
    _strict(
        "final_answer",
        "Give the final answer. Call this exactly once, at the end.",
        {
            "text": {"type": "string", "description": "The answer, leading with the number."},
            "result_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "result_ids of the run_sql results the answer is based on.",
            },
            "metrics_used": {"type": "array", "items": {"type": "string"}},
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Resolved date range, timezone and any interpretation made.",
            },
            "chart_id": {"type": "string", "description": "chart_id, or empty string."},
        },
    ),
]
TOOL_NAMES = frozenset(t["name"] for t in TOOL_SCHEMAS)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DescribeTableInput(_Input):
    table: str


class RunSqlInput(_Input):
    sql: str


class MakeChartInput(_Input):
    result_id: str
    kind: Literal["line", "bar"]
    x: str
    y: str
    title: str


class FinalAnswerInput(_Input):
    text: str
    result_ids: list[str]
    metrics_used: list[str]
    assumptions: list[str]
    chart_id: str = ""


# --------------------------------------------------------------------------- state


@dataclass
class QueryResult:
    id: str
    sql: str  # the SQL that actually ran (after guard rewriting)
    columns: list[str]
    rows: list[tuple[Any, ...]]
    duration_ms: float


@dataclass
class FinalAnswer:
    text: str
    result_ids: list[str]
    metrics_used: list[str]
    assumptions: list[str]
    chart_id: str
    sql_used: list[str]
    chart_path: str | None


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    error_code: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    final: FinalAnswer | None = None


@dataclass
class ToolContext:
    con: duckdb.DuckDBPyConnection
    settings: Settings
    trace_id: str
    results: dict[str, QueryResult] = field(default_factory=dict)
    charts: dict[str, Path] = field(default_factory=dict)
    consecutive_sql_failures: int = 0
    sql_disabled: bool = False


def open_readonly(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Read-only connection with external access disabled and configuration locked."""
    return duckdb.connect(
        str(db_path),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )


# --------------------------------------------------------------------------- helpers


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def _error(code: str, message: str, **meta: Any) -> ToolOutcome:
    return ToolOutcome(content=f"[{code}] {message}", is_error=True, error_code=code, meta=meta)


def _dumps(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        return text[:MAX_TOOL_OUTPUT_CHARS] + " …[truncated]"
    return text


def _mart_descriptions(settings: Settings) -> dict[str, dict[str, Any]]:
    path = settings.paths.dbt_project_dir / "models" / "marts" / "_marts.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for model in data.get("models", []):
        cols = {
            c["name"]: " ".join(str(c.get("description", "")).split())
            for c in model.get("columns", [])
        }
        out[model["name"]] = {
            "description": " ".join(str(model.get("description", "")).split()),
            "columns": cols,
        }
    return out


def _execute(
    con: duckdb.DuckDBPyConnection, sql: str, timeout_sec: float
) -> tuple[list[str], list[tuple[Any, ...]]]:
    timer = threading.Timer(timeout_sec, con.interrupt)
    timer.start()
    try:
        cursor = con.execute(sql)
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description or []]
    finally:
        timer.cancel()
    return columns, rows


# --------------------------------------------------------------------------- tools


def describe_table(ctx: ToolContext, args: DescribeTableInput) -> ToolOutcome:
    table = args.table
    if table not in MART_TABLES:
        return _error("table_not_allowed", f"unknown table {table!r}; tables: {list(MART_TABLES)}")
    cols = ctx.con.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position",
        [table],
    ).fetchall()
    count_row = ctx.con.execute(f"SELECT count(*) FROM {table}").fetchone()
    payload: dict[str, Any] = {
        "table": table,
        "row_count": int(count_row[0]) if count_row else 0,
        "columns": [{"name": c, "type": t} for c, t in cols],
    }
    agent = ctx.settings.agent
    if agent.describe_include_descriptions:
        docs = _mart_descriptions(ctx.settings).get(table, {})
        payload["description"] = docs.get("description", "")
        for col in payload["columns"]:
            col["description"] = docs.get("columns", {}).get(col["name"], "")
    n = agent.describe_sample_rows
    row_level = agent.result_mode == "aggregate_only" and not AGGREGATED_TABLE_RE.match(table)
    if row_level:
        # aggregate_only: no individual records; column statistics instead (DESIGN §4.6).
        stats = []
        for name, _type in cols:
            row = ctx.con.execute(
                f'SELECT min("{name}"), max("{name}"), count(DISTINCT "{name}") FROM {table}'
            ).fetchone()
            if row is not None:
                lo, hi, distinct = row
                stats.append(
                    {
                        "column": name,
                        "min": _jsonable(lo),
                        "max": _jsonable(hi),
                        "distinct": int(distinct),
                    }
                )
        payload["column_stats"] = stats
    elif n:
        sample = ctx.con.execute(f"SELECT * FROM {table} LIMIT {int(n)}").fetchall()
        payload["sample_rows"] = [[_jsonable(v) for v in r] for r in sample]
    return ToolOutcome(content=_dumps(payload), meta={"table": table})


def run_sql(ctx: ToolContext, args: RunSqlInput) -> ToolOutcome:
    agent = ctx.settings.agent
    if ctx.sql_disabled:
        return _error(
            "sql_disabled",
            "run_sql is disabled after repeated errors; call final_answer with what you have.",
        )

    def failed(code: str, message: str, **meta: Any) -> ToolOutcome:
        ctx.consecutive_sql_failures += 1
        if ctx.consecutive_sql_failures > agent.sql_max_retries:
            ctx.sql_disabled = True
            message += (
                f" No retries left ({agent.sql_max_retries} used); call final_answer now and "
                "say what could not be computed."
            )
        return _error(code, message, sql=args.sql, failures=ctx.consecutive_sql_failures, **meta)

    try:
        guarded = guard_sql(args.sql, result_mode=agent.result_mode, max_rows=agent.max_result_rows)
    except SqlGuardError as exc:
        return failed(exc.rule, exc.message)

    started = time.perf_counter()
    try:
        columns, rows = _execute(ctx.con, guarded.sql, agent.statement_timeout_sec)
    except duckdb.InterruptException:
        return failed(
            "timeout", f"query exceeded {agent.statement_timeout_sec:g}s", executed=guarded.sql
        )
    except duckdb.Error as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return failed("execution_error", first_line, executed=guarded.sql)
    duration_ms = (time.perf_counter() - started) * 1000

    ctx.consecutive_sql_failures = 0
    result_id = f"r{len(ctx.results) + 1}"
    ctx.results[result_id] = QueryResult(result_id, guarded.sql, columns, rows, duration_ms)
    payload = {
        "result_id": result_id,
        "columns": columns,
        "rows": [[_jsonable(v) for v in r] for r in rows],
        "row_count": len(rows),
    }
    if guarded.notes:
        payload["note"] = "; ".join(guarded.notes)
    return ToolOutcome(
        content=_dumps(payload),
        meta={
            "result_id": result_id,
            "executed": guarded.sql,
            "row_count": len(rows),
            "duration_ms": round(duration_ms, 1),
        },
    )


def make_chart(ctx: ToolContext, args: MakeChartInput) -> ToolOutcome:
    result = ctx.results.get(args.result_id)
    if result is None:
        return _error("unknown_result_id", f"no result {args.result_id!r}; use a run_sql result_id")
    for col in (args.x, args.y):
        if col not in result.columns:
            return _error(
                "unknown_column", f"{col!r} is not a column of {args.result_id}: {result.columns}"
            )
    if not result.rows:
        return _error("empty_result", f"{args.result_id} has no rows to chart")
    xi, yi = result.columns.index(args.x), result.columns.index(args.y)
    xs = [_jsonable(r[xi]) for r in result.rows]
    try:
        ys = [float(r[yi]) if r[yi] is not None else float("nan") for r in result.rows]
    except (TypeError, ValueError):
        return _error("not_numeric", f"y column {args.y!r} must be numeric")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    chart_id = f"c{len(ctx.charts) + 1}"
    out_dir = ctx.settings.paths.charts_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ctx.trace_id}_{chart_id}.png"
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    labels = [str(x) for x in xs]
    if args.kind == "bar":
        ax.bar(labels, ys)
    else:
        ax.plot(labels, ys, marker="o" if len(xs) <= 31 else None)
    ax.set_title(args.title)
    ax.set_xlabel(args.x)
    ax.set_ylabel(args.y)
    if len(labels) > 12:
        step = max(1, len(labels) // 12)
        ax.set_xticks(range(0, len(labels), step))
        ax.set_xticklabels(labels[::step], rotation=45, ha="right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    ctx.charts[chart_id] = path
    return ToolOutcome(
        content=_dumps({"chart_id": chart_id, "points": len(xs)}),
        meta={"chart_id": chart_id, "path": str(path)},
    )


def final_answer(ctx: ToolContext, args: FinalAnswerInput) -> ToolOutcome:
    unknown = [r for r in args.result_ids if r not in ctx.results]
    if unknown:
        return _error(
            "unknown_result_id", f"unknown result_ids {unknown}; available: {sorted(ctx.results)}"
        )
    chart_path = None
    if args.chart_id:
        if args.chart_id not in ctx.charts:
            return _error("unknown_chart_id", f"unknown chart_id {args.chart_id!r}")
        chart_path = str(ctx.charts[args.chart_id])
    final = FinalAnswer(
        text=args.text.strip(),
        result_ids=list(args.result_ids),
        metrics_used=list(args.metrics_used),
        assumptions=list(args.assumptions),
        chart_id=args.chart_id,
        sql_used=[ctx.results[r].sql for r in args.result_ids],
        chart_path=chart_path,
    )
    return ToolOutcome(content="Answer recorded.", final=final)


_HANDLERS: dict[str, tuple[type[_Input], Any]] = {
    "describe_table": (DescribeTableInput, describe_table),
    "run_sql": (RunSqlInput, run_sql),
    "make_chart": (MakeChartInput, make_chart),
    "final_answer": (FinalAnswerInput, final_answer),
}


def execute_tool(ctx: ToolContext, name: str, raw_input: dict[str, Any]) -> ToolOutcome:
    """Validate the input against the tool's schema, then run it."""
    if name not in _HANDLERS:
        return _error("unknown_tool", f"unknown tool {name!r}; tools: {sorted(_HANDLERS)}")
    model, handler = _HANDLERS[name]
    try:
        args = model.model_validate(raw_input)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return _error("invalid_input", f"invalid input for {name}: fields {fields}")
    outcome: ToolOutcome = handler(ctx, args)
    return outcome
