"""SQL guard (DESIGN §6.3): one test per rejection rule, plus the shapes that must pass."""

import pytest

from scrollback.agent.sql_guard import ResultMode, SqlGuardError, guard_sql


def ok(sql: str, mode: ResultMode = "aggregate_only", max_rows: int = 200) -> str:
    return guard_sql(sql, result_mode=mode, max_rows=max_rows).sql


def rejected(sql: str, mode: ResultMode = "aggregate_only") -> str:
    with pytest.raises(SqlGuardError) as err:
        guard_sql(sql, result_mode=mode, max_rows=200)
    return err.value.rule


# --------------------------------------------------------------------------- one test per rule


@pytest.mark.parametrize("sql", ["", "   ;  ", "SELEC 1", "SELECT FROM WHERE ("])
def test_parse_error(sql: str) -> None:
    assert rejected(sql, "full") == "parse_error"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT count(*) FROM fct_views; DROP TABLE fct_views",
    ],
)
def test_multiple_statements(sql: str) -> None:
    assert rejected(sql, "full") == "multiple_statements"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO fct_views VALUES (1)",
        "UPDATE fct_views SET video_id = 'x'",
        "DELETE FROM fct_views",
        "DROP TABLE fct_views",
        "CREATE TABLE fct_x AS SELECT 1",
        "ALTER TABLE fct_views ADD COLUMN x INT",
        "PRAGMA show_tables",
        "SET threads = 1",
        "ATTACH 'other.duckdb'",
        "COPY fct_views TO 'out.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "DESCRIBE fct_views",
        "SUMMARIZE fct_views",
        "BEGIN TRANSACTION",
    ],
)
def test_write_or_ddl(sql: str) -> None:
    assert rejected(sql, "full") == "write_or_ddl"


def test_not_select() -> None:
    assert rejected("VALUES (1), (2)", "full") == "not_select"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/hosts')",
        "SELECT * FROM range(10)",
        "SELECT * FROM generate_series(1, 3)",
        "SELECT * FROM glob('*')",
        "SELECT * FROM duckdb_settings()",
    ],
)
def test_table_function(sql: str) -> None:
    assert rejected(sql, "full") == "table_function"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM information_schema.tables",
        "SELECT count(*) FROM other.fct_views",
        "SELECT count(*) FROM memory.main.fct_views".replace("memory.main.", "temp."),
    ],
)
def test_schema_not_allowed(sql: str) -> None:
    assert rejected(sql, "full") == "schema_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM raw_watch",
        "SELECT count(*) FROM stg_watch",
        "SELECT count(*) FROM int_sessions",
        "SELECT count(*) FROM ingest_log",
        "SELECT count(*) FROM raw_rejects",
        "SELECT count(*) FROM agent_traces",
        "SELECT * FROM 'data/raw/export.json'",
        "SELECT count(*) FROM fct_views JOIN raw_like USING (video_id)",
        "SELECT count(*) FROM fct_views WHERE video_id IN (SELECT video_id FROM raw_watch)",
    ],
)
def test_table_not_allowed(sql: str) -> None:
    assert rejected(sql, "full") == "table_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT getenv('ANTHROPIC_API_KEY')",
        "SELECT current_setting('threads')",
        "SELECT read_text('/etc/hosts')",
        "SELECT count(*) FROM fct_views WHERE video_id = getenv('X')",
    ],
)
def test_function_not_allowed(sql: str) -> None:
    assert rejected(sql, "full") == "function_not_allowed"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM fct_views",
        "SELECT video_id, event_ts_local FROM fct_views WHERE local_date = '2026-11-19'",
        "SELECT local_date, sum(est_watch_sec) OVER (ORDER BY local_date) FROM fct_views",
        "WITH v AS (SELECT * FROM fct_views) SELECT * FROM v",
        "SELECT * FROM (SELECT view_id FROM fct_views) AS t",
        "SELECT count(*) FROM fct_views UNION ALL SELECT video_id FROM fct_views",
        "SELECT d.local_date, v.video_id FROM dim_date d JOIN fct_views v USING (local_date)",
    ],
)
def test_not_aggregate(sql: str) -> None:
    assert rejected(sql) == "not_aggregate"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT view_id, count(*) FROM fct_views GROUP BY view_id",
        "SELECT session_id, sum(est_watch_sec) FROM fct_views GROUP BY 1",
        "SELECT event_ts_local AS t, count(*) FROM fct_views GROUP BY t",
        "SELECT session_id, max(est_watch_sec) FROM fct_sessions GROUP BY session_id",
        "SELECT search_id, count(*) FROM fct_searches GROUP BY search_id",
    ],
)
def test_row_level_grouping(sql: str) -> None:
    assert rejected(sql) == "row_level_grouping"


# --------------------------------------------------------------------------- must pass


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(est_watch_sec) / 60.0 FROM fct_views WHERE local_date = DATE '2026-11-19'",
        "SELECT count(*) FROM fct_views",
        "SELECT local_hour, count(*) FROM fct_views GROUP BY local_hour ORDER BY 1",
        "SELECT * FROM agg_daily WHERE local_date >= '2026-11-01'",
        "SELECT local_date, est_watch_min FROM agg_daily ORDER BY local_date",
        "SELECT d.iso_week, sum(a.views) FROM agg_daily a JOIN dim_date d USING (local_date) "
        "GROUP BY 1",
        "WITH daily AS (SELECT local_date, count(*) AS n FROM fct_views GROUP BY 1) "
        "SELECT * FROM daily ORDER BY n DESC",
        "WITH s AS (SELECT local_date, max(est_watch_sec) AS m FROM fct_sessions GROUP BY 1) "
        "SELECT a.local_date, a.views, s.m FROM agg_daily a LEFT JOIN s USING (local_date)",
        "SELECT video_id, count(*) AS n FROM fct_views GROUP BY video_id ORDER BY n DESC",
        "SELECT search_term_norm, count(*) FROM fct_searches GROUP BY 1 ORDER BY 2 DESC",
        "SELECT date_trunc('hour', event_ts_local) AS h, count(*) FROM fct_views GROUP BY 1",
        "SELECT local_date, avg(views) OVER (ORDER BY local_date ROWS 6 PRECEDING) FROM agg_daily",
        "SELECT count(*) FROM fct_views UNION ALL SELECT count(*) FROM fct_likes",
        "SELECT 1",
        "FROM agg_daily SELECT local_date, views",
        "SELECT count(*) FROM main.fct_views",
        "select count(*) from fct_views;",
    ],
)
def test_allowed_queries(sql: str) -> None:
    assert ok(sql)


def test_full_mode_allows_row_level_marts() -> None:
    assert ok("SELECT view_id, event_ts_local FROM fct_views", mode="full")
    assert rejected("SELECT count(*) FROM raw_watch", mode="full") == "table_not_allowed"


# --------------------------------------------------------------------------- limit rewriting


def test_limit_added_when_missing() -> None:
    result = guard_sql("SELECT * FROM agg_daily", result_mode="aggregate_only", max_rows=200)
    assert result.sql.endswith("LIMIT 200")
    assert result.limit_applied and result.notes == ["LIMIT 200 added"]


def test_limit_reduced_when_too_large() -> None:
    result = guard_sql("SELECT * FROM agg_daily LIMIT 5000", result_mode="full", max_rows=200)
    assert result.sql.endswith("LIMIT 200")
    assert result.notes == ["LIMIT reduced to 200"]


def test_small_limit_kept() -> None:
    result = guard_sql("SELECT * FROM agg_daily LIMIT 7", result_mode="full", max_rows=200)
    assert result.sql.endswith("LIMIT 7") and not result.limit_applied


def test_non_literal_limit_replaced() -> None:
    sql = "SELECT * FROM agg_daily LIMIT (SELECT 1000)"
    assert ok(sql).endswith("LIMIT 200")


def test_limit_on_set_operation() -> None:
    sql = "SELECT count(*) FROM fct_views UNION ALL SELECT count(*) FROM fct_likes"
    assert ok(sql).endswith("LIMIT 200")


def test_tables_reported() -> None:
    result = guard_sql(
        "WITH x AS (SELECT local_date, count(*) n FROM fct_views GROUP BY 1) "
        "SELECT * FROM x JOIN dim_date USING (local_date)",
        result_mode="aggregate_only",
        max_rows=200,
    )
    assert result.tables == ("dim_date", "fct_views")


# --------------------------------------------------------------------------- E-1 staging scope


def test_staging_scope_allows_only_stg_views() -> None:
    sql = "SELECT count(*) FROM stg_watch"
    assert guard_sql(sql, result_mode="aggregate_only", max_rows=200, tables="staging").tables == (
        "stg_watch",
    )
    with pytest.raises(SqlGuardError) as err:
        guard_sql("SELECT count(*) FROM fct_views", result_mode="aggregate_only", max_rows=200,
                  tables="staging")  # fmt: skip
    assert err.value.rule == "table_not_allowed" and "stg_*" in err.value.message
    with pytest.raises(SqlGuardError) as err:
        guard_sql("SELECT count(*) FROM stg_watch", result_mode="aggregate_only", max_rows=200)
    assert err.value.rule == "table_not_allowed"  # marts scope rejects staging


def test_staging_scope_has_no_preaggregated_tables() -> None:
    with pytest.raises(SqlGuardError) as err:
        guard_sql("SELECT local_date, video_id FROM stg_watch", result_mode="aggregate_only",
                  max_rows=200, tables="staging")  # fmt: skip
    assert err.value.rule == "not_aggregate"
    sessionised = (
        "WITH g AS (SELECT local_date, event_ts_utc - lag(event_ts_utc) OVER (ORDER BY "
        "event_ts_utc) AS gap FROM stg_watch) SELECT local_date, count(*) FROM g GROUP BY 1"
    )
    assert guard_sql(sessionised, result_mode="aggregate_only", max_rows=200, tables="staging")
