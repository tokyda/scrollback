"""dbt build on the synthetic sample (ROADMAP Phase 2 "Done when", part 1)."""

import json
import os
import subprocess
from pathlib import Path

import duckdb
import pytest

from scrollback.ingest.load import ingest_file
from scrollback.warehouse.db import connect
from scrollback.warehouse.dbt_runner import DUCKDB_PATH_ENV, METRIC_VARS, dbt_vars, run_dbt
from tests.conftest import REPO_ROOT, SAMPLE_EXPORT, BuiltWarehouse, repo_settings

MARTS = ["fct_views", "fct_sessions", "fct_likes", "fct_searches", "dim_date", "agg_daily"]


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> tuple:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row


def test_dbt_build_passes_every_test(synthetic_warehouse: BuiltWarehouse) -> None:
    result = synthetic_warehouse.result
    assert result.success, result.failures
    assert result.failures == []
    assert result.statuses["pass"] >= 45  # data tests
    assert result.statuses["success"] == 13  # models
    assert set(result.statuses) <= {"pass", "success", "warn"}  # no error / fail / skip
    # The synthetic data ends 2026-11-19, so the freshness check starts warning (by design)
    # 14 days later. That is the only test allowed to warn.
    assert all(w.endswith(".assert_data_freshness") for w in result.warnings), result.warnings


def test_freshness_warns_but_does_not_fail(tmp_path: Path) -> None:
    from datetime import date

    from scrollback.synth.generator import synthesize, write

    export = tmp_path / "old_export.json"
    write(synthesize(days=14, end=date(2026, 10, 1)), export)
    db_path = tmp_path / "wh.duckdb"
    con = connect(db_path)
    ingest_file(con, export)
    con.close()
    # Data ends 2026-10-01, more than 7 days before any date this test runs on.
    settings = repo_settings(freshness_warn_days=7)
    result = run_dbt(settings, db_path, quiet=True, target_path=tmp_path / "target")
    assert result.success
    assert result.warnings == ["test.scrollback.assert_data_freshness"]


def test_vars_come_from_config_only() -> None:
    settings = repo_settings()
    values = dbt_vars(settings)
    assert set(values) == {*METRIC_VARS, "source_tz", "local_tz"}
    assert values["session_gap_min"] == settings.metrics.session_gap_min
    assert values["view_cap_sec"] == settings.metrics.view_cap_sec
    project = (REPO_ROOT / "dbt" / "dbt_project.yml").read_text()
    assert "vars:" not in project  # no fallback defaults in dbt


def test_dbt_without_vars_fails_loudly(tmp_path: Path) -> None:
    db_path = tmp_path / "wh.duckdb"
    con = connect(db_path)
    ingest_file(con, SAMPLE_EXPORT)
    con.close()
    dbt = Path(os.environ.get("VIRTUAL_ENV", REPO_ROOT / ".venv")) / "bin" / "dbt"
    project = str(REPO_ROOT / "dbt")
    completed = subprocess.run(
        [
            str(dbt),
            "--quiet",
            "build",
            "--project-dir",
            project,
            "--profiles-dir",
            project,
            "--target-path",
            str(tmp_path / "target"),
        ],
        env={**os.environ, DUCKDB_PATH_ENV: str(db_path), "DO_NOT_TRACK": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "Required var" in completed.stdout + completed.stderr


def test_marts_exist_and_are_tables(wh: duckdb.DuckDBPyConnection) -> None:
    rows = wh.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_name IN (SELECT unnest($1))",
        [MARTS],
    ).fetchall()
    assert dict(rows) == dict.fromkeys(MARTS, "BASE TABLE")


def test_timezone_conversion(wh: duckdb.DuckDBPyConnection) -> None:
    # Synthetic export is UTC; local_tz is Asia/Singapore (UTC+8, no DST).
    offsets = wh.execute(
        "SELECT DISTINCT date_diff('second', event_ts_utc, event_ts_local) FROM stg_watch"
    ).fetchall()
    assert offsets == [(8 * 3600,)]
    raw_vs_utc = _one(
        wh,
        "SELECT count(*) FROM raw_watch r JOIN stg_watch s ON s.view_id = r.record_hash "
        "WHERE strftime(s.event_ts_utc, '%Y-%m-%d %H:%M:%S') != r.event_ts_raw",
    )
    assert raw_vs_utc == (0,)


def test_row_counts_line_up(wh: duckdb.DuckDBPyConnection) -> None:
    manifest = json.loads(SAMPLE_EXPORT.with_name("user_data_tiktok.manifest.json").read_text())
    expected = manifest["expected"]["datasets"]
    assert _one(wh, "SELECT count(*) FROM fct_views") == (expected["watch"]["valid_unique"],)
    assert _one(wh, "SELECT count(*) FROM fct_likes") == (expected["like"]["valid_unique"],)
    assert _one(wh, "SELECT count(*) FROM fct_searches") == (expected["search"]["valid_unique"],)
    assert _one(wh, "SELECT sum(views), sum(likes), sum(searches) FROM agg_daily") == (
        expected["watch"]["valid_unique"],
        expected["like"]["valid_unique"],
        expected["search"]["valid_unique"],
    )
    assert _one(wh, "SELECT sum(n_views) FROM fct_sessions") == (expected["watch"]["valid_unique"],)


def test_date_spine_is_contiguous_and_zero_filled(wh: duckdb.DuckDBPyConnection) -> None:
    n, first, last = _one(wh, "SELECT count(*), min(local_date), max(local_date) FROM dim_date")
    assert str(first) == manifest_start() and str(last) == manifest_end()
    assert n == (last - first).days + 1
    zero_days = _one(wh, "SELECT count(*) FROM agg_daily WHERE views = 0 AND NOT is_active")[0]
    assert zero_days > 0  # the detox week guarantees some
    nulls = _one(
        wh,
        "SELECT count(*) FROM agg_daily WHERE views IS NULL OR est_watch_min IS NULL "
        "OR sessions IS NULL OR likes IS NULL OR searches IS NULL",
    )
    assert nulls == (0,)


def manifest_start() -> str:
    return json.loads(SAMPLE_EXPORT.with_name("user_data_tiktok.manifest.json").read_text())[
        "start"
    ]


def manifest_end() -> str:
    return json.loads(SAMPLE_EXPORT.with_name("user_data_tiktok.manifest.json").read_text())["end"]


def test_estimates_respect_parameters(synthetic_warehouse: BuiltWarehouse) -> None:
    con = synthetic_warehouse.connect()
    cap = synthetic_warehouse.settings.metrics.view_cap_sec
    gap = synthetic_warehouse.settings.metrics.session_gap_min * 60
    low, high = _one(con, "SELECT min(est_watch_sec), max(est_watch_sec) FROM fct_views")
    assert low >= 0 and high == cap  # long pauses are capped at exactly view_cap_sec
    # Within a session, no gap exceeds the session threshold; between sessions every gap does.
    assert _one(
        con,
        f"SELECT count(*) FROM fct_views WHERE NOT is_last_in_session AND gap_to_next_sec > {gap}",
    ) == (0,)
    assert _one(
        con,
        f"SELECT count(*) FROM fct_views WHERE is_last_in_session "
        f"AND gap_to_next_sec IS NOT NULL AND gap_to_next_sec <= {gap}",
    ) == (0,)
    con.close()


def test_injected_events_visible_in_agg_daily(wh: duckdb.DuckDBPyConnection) -> None:
    median = _one(wh, "SELECT median(est_watch_min) FROM agg_daily")[0]
    detox = _one(
        wh,
        "SELECT sum(est_watch_min) FROM agg_daily "
        "WHERE local_date BETWEEN '2026-09-07' AND '2026-09-13'",
    )[0]
    binge = _one(
        wh,
        "SELECT min(est_watch_min) FROM agg_daily WHERE local_date IN ('2026-10-24', '2026-10-25')",
    )[0]
    assert detox < median  # a whole week below one median day
    assert binge > 3 * median
    assert _one(wh, "SELECT count(*) FROM fct_sessions WHERE is_binge")[0] > 0


@pytest.mark.parametrize("gap_min", [5])
def test_changing_session_gap_changes_sessions(tmp_path: Path, gap_min: int) -> None:
    db_path = tmp_path / "wh.duckdb"
    con = connect(db_path)
    ingest_file(con, SAMPLE_EXPORT)
    con.close()
    default = run_dbt(repo_settings(), db_path, quiet=True, target_path=tmp_path / "t1")
    con = duckdb.connect(str(db_path), read_only=True)
    n_default = _one(con, "SELECT count(*) FROM fct_sessions")[0]
    con.close()
    tighter = run_dbt(
        repo_settings(session_gap_min=gap_min), db_path, quiet=True, target_path=tmp_path / "t2"
    )
    con = duckdb.connect(str(db_path), read_only=True)
    n_tighter = _one(con, "SELECT count(*) FROM fct_sessions")[0]
    con.close()
    assert default.success and tighter.success
    assert n_tighter > n_default
