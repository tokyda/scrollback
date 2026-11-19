"""SQL guard for the agent's ``run_sql`` tool (DESIGN §6.3, §4.6).

Every query the model writes is parsed with sqlglot (DuckDB dialect) and must pass these rules,
each with a stable code so traces and tests can name it:

  parse_error            the SQL does not parse
  multiple_statements    more than one statement
  not_select             the statement is not a SELECT / WITH ... SELECT / set operation
  write_or_ddl           a write, DDL, PRAGMA, SET, ATTACH, COPY, INSTALL ... anywhere in the tree
  table_function         FROM <function>(...) (read_csv, range, glob, duckdb_settings, ...)
  schema_not_allowed     a table qualified with a schema other than main
  table_not_allowed      a table outside the marts allowlist (fct_*, dim_*, agg_*)
  function_not_allowed   a function that reads files, the environment or settings
  not_aggregate          (aggregate_only) the result is row-level, not aggregated
  row_level_grouping     (aggregate_only) GROUP BY a row identifier or a raw timestamp

Passing queries get ``LIMIT max_rows`` added, or a larger literal LIMIT reduced to it.

The guard stops accidental row dumps and keeps the agent on the marts; it is not the only
layer. Queries also run on a read-only DuckDB connection with external access disabled, a
locked configuration and a statement timeout (agent/tools.py).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

logging.getLogger("sqlglot").setLevel(logging.ERROR)

ResultMode = Literal["aggregate_only", "full"]

ALLOWED_TABLE_RE = re.compile(r"^(fct|dim|agg)_[a-z0-9_]+$")
AGGREGATED_TABLE_RE = re.compile(r"^(agg|dim)_[a-z0-9_]+$")
ALLOWED_SCHEMAS = frozenset({"", "main"})

FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.Pragma, exp.Set, exp.Copy, exp.Attach, exp.Detach, exp.Install,
    exp.Describe, exp.Summarize, exp.Use, exp.TruncateTable, exp.Transaction,
)  # fmt: skip

FORBIDDEN_FUNCTION_PREFIXES = (
    "read_", "duckdb_", "pragma_", "parquet_", "sqlite_", "postgres_", "mysql_", "iceberg_",
    "delta_", "http_", "glob",
)  # fmt: skip
FORBIDDEN_FUNCTIONS = frozenset(
    {"getenv", "current_setting", "query", "query_table", "sniff_csv", "load", "install",
     "checkpoint", "force_checkpoint", "which_secret", "readcsv", "readparquet"}
)  # fmt: skip

# GROUP BY on these bare columns returns one row per record (aggregate_only).
ROW_LEVEL_COLUMNS = frozenset(
    {"view_id", "like_id", "search_id", "share_id", "favorite_id", "session_id",
     "event_ts_utc", "event_ts_local", "start_ts_local", "end_ts_local"}
)  # fmt: skip


class SqlGuardError(ValueError):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"[{rule}] {message}")
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class GuardedSql:
    sql: str  # the rewritten SQL that will run
    tables: tuple[str, ...]
    limit_applied: bool
    notes: list[str] = field(default_factory=list)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _cte_names(root: exp.Expr) -> set[str]:
    return {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}


def _is_set_operation(node: exp.Expr) -> bool:
    return isinstance(node, exp.Union | exp.Intersect | exp.Except)


def _check_tables(root: exp.Expr) -> list[str]:
    ctes = _cte_names(root)
    tables: list[str] = []
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            name = _function_name(table.this) if isinstance(table.this, exp.Func) else "?"
            raise SqlGuardError("table_function", f"table functions are not allowed ({name})")
        name = table.name.lower()
        schema = (table.db or "").lower()
        if schema not in ALLOWED_SCHEMAS:
            raise SqlGuardError("schema_not_allowed", f"schema {table.db!r} is not allowed")
        if not schema and name in ctes:
            continue
        if not ALLOWED_TABLE_RE.match(name):
            raise SqlGuardError(
                "table_not_allowed",
                f"table {table.name!r} is not allowed; query the marts (fct_*, dim_*, agg_*)",
            )
        tables.append(name)
    return sorted(set(tables))


def _check_functions(root: exp.Expr) -> None:
    for func in root.find_all(exp.Func):
        name = _function_name(func)
        if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
            raise SqlGuardError("function_not_allowed", f"function {name}() is not allowed")


# --------------------------------------------------------------------------- aggregate_only


def _has_aggregate_projection(select: exp.Select) -> bool:
    for projection in select.expressions:
        for agg in projection.find_all(exp.AggFunc):
            if agg.find_ancestor(exp.Window) is None:
                return True
    return False


def _source_is_aggregated(source: exp.Expr, ctes: dict[str, exp.Expr]) -> bool:
    if isinstance(source, exp.Table):
        name = source.name.lower()
        if not source.db and name in ctes:
            return _query_is_aggregated(ctes[name], ctes)
        return bool(AGGREGATED_TABLE_RE.match(name))
    if isinstance(source, exp.Subquery):
        return _query_is_aggregated(source.this, ctes)
    return False


def _query_is_aggregated(node: exp.Expr, ctes: dict[str, exp.Expr]) -> bool:
    if _is_set_operation(node):
        return _query_is_aggregated(node.this, ctes) and _query_is_aggregated(node.expression, ctes)
    if isinstance(node, exp.Subquery):
        return _query_is_aggregated(node.this, ctes)
    if not isinstance(node, exp.Select):
        return False
    if node.args.get("group") is not None or _has_aggregate_projection(node):
        return True
    sources: list[exp.Expr] = []
    from_ = node.args.get("from") or node.args.get("from_")
    if from_ is not None:
        sources.append(from_.this)
    sources += [join.this for join in node.args.get("joins") or []]
    # A SELECT with no FROM (e.g. SELECT 1) is a constant, not row-level data.
    return all(_source_is_aggregated(src, ctes) for src in sources)


def _group_key_expressions(select: exp.Select) -> list[exp.Expr]:
    group = select.args.get("group")
    if group is None:
        return []
    keys: list[exp.Expr] = []
    projections = select.expressions
    for key in group.expressions:
        if isinstance(key, exp.Literal) and key.is_int:
            index = int(key.this) - 1  # GROUP BY 1 refers to the first projection
            if 0 <= index < len(projections):
                keys.append(projections[index].unalias())
                continue
        if isinstance(key, exp.Column) and not key.table:
            alias_target = next(
                (p.unalias() for p in projections if p.alias and p.alias == key.name), None
            )
            if alias_target is not None:
                keys.append(alias_target)
                continue
        keys.append(key)
    return keys


def _check_row_level_grouping(root: exp.Expr) -> None:
    for select in root.find_all(exp.Select):
        for key in _group_key_expressions(select):
            if isinstance(key, exp.Column) and key.name.lower() in ROW_LEVEL_COLUMNS:
                raise SqlGuardError(
                    "row_level_grouping",
                    f"GROUP BY {key.name} returns one row per record; group by a date, hour, "
                    "day of week or other attribute instead",
                )


def _check_aggregate_only(root: exp.Expr) -> None:
    ctes = {cte.alias_or_name.lower(): cte.this for cte in root.find_all(exp.CTE)}
    if not _query_is_aggregated(root, ctes):
        raise SqlGuardError(
            "not_aggregate",
            "result_mode is aggregate_only: the query must aggregate (COUNT/SUM/AVG/... or "
            "GROUP BY) or read only agg_* / dim_* tables",
        )
    _check_row_level_grouping(root)


# --------------------------------------------------------------------------- limit


def _apply_limit(root: exp.Expr, max_rows: int) -> tuple[bool, list[str]]:
    limit = root.args.get("limit")
    if limit is None:
        root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        return True, [f"LIMIT {max_rows} added"]
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= max_rows:
        return False, []
    root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return True, [f"LIMIT reduced to {max_rows}"]


# --------------------------------------------------------------------------- entry point


def guard_sql(sql: str, *, result_mode: ResultMode, max_rows: int) -> GuardedSql:
    """Validate ``sql`` and return the rewritten query, or raise :class:`SqlGuardError`."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise SqlGuardError("parse_error", "empty query")
    try:
        statements = [s for s in sqlglot.parse(text, dialect="duckdb") if s is not None]
    except SqlglotError as exc:
        raise SqlGuardError("parse_error", f"could not parse SQL: {exc}") from None
    if len(statements) != 1:
        raise SqlGuardError("multiple_statements", "send exactly one statement")
    root = statements[0]

    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise SqlGuardError(
                "write_or_ddl", f"{type(node).__name__.upper()} statements are not allowed"
            )
    if not (isinstance(root, exp.Select) or _is_set_operation(root)):
        raise SqlGuardError("not_select", "only SELECT / WITH queries are allowed")

    tables = _check_tables(root)
    _check_functions(root)
    if result_mode == "aggregate_only":
        _check_aggregate_only(root)

    limited, notes = _apply_limit(root, max_rows)
    return GuardedSql(
        sql=root.sql(dialect="duckdb"),
        tables=tuple(tables),
        limit_applied=limited,
        notes=notes,
    )
