"""`scrollback inspect gaps`: aggregates only, consistent with the marts."""

import re
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from scrollback.cli import app
from scrollback.warehouse.db import connect, ensure_raw_schema
from scrollback.warehouse.inspect import (
    WarehouseNotBuiltError,
    fmt_duration,
    format_gap_report,
    gap_stats,
)
from tests.conftest import REPO_ROOT, BuiltWarehouse

runner = CliRunner()


def test_stats_are_consistent_with_marts(wh: duckdb.DuckDBPyConnection) -> None:
    stats = gap_stats(wh, view_cap_sec=180, session_gap_min=30)
    n_views = wh.execute("SELECT count(*) FROM fct_views").fetchone()
    n_sessions = wh.execute("SELECT count(*) FROM fct_sessions").fetchone()
    assert n_views is not None and n_sessions is not None
    assert stats.n_views == n_views[0]
    assert stats.n_gaps == n_views[0] - 1
    assert sum(b.count for b in stats.bins) == stats.n_gaps
    assert stats.sessions_by_gap_min[30] == n_sessions[0]
    # Bins are contiguous and the parameter values are bin edges.
    edges = [b.low_sec for b in stats.bins]
    assert edges == sorted(edges) and 180 in edges and 1800 in edges
    pairs = zip(stats.bins[:-1], stats.bins[1:], strict=True)
    assert all(b.high_sec == nxt.low_sec for b, nxt in pairs)
    # Monotone what-ifs: a longer threshold never yields more sessions / capped share.
    sessions = list(stats.sessions_by_gap_min.values())
    assert sessions == sorted(sessions, reverse=True)
    capped = list(stats.capped_share_by_cap.values())
    assert capped == sorted(capped, reverse=True)
    assert list(stats.percentiles.values()) == sorted(stats.percentiles.values())


def test_capped_share_matches_fct_views(wh: duckdb.DuckDBPyConnection) -> None:
    stats = gap_stats(wh, view_cap_sec=180, session_gap_min=30)
    row = wh.execute(
        "SELECT avg(CASE WHEN gap_to_next_sec > 180 THEN 1.0 ELSE 0 END) FROM fct_views "
        "WHERE NOT is_last_in_session"
    ).fetchone()
    assert row is not None
    assert stats.capped_share_by_cap[180] == pytest.approx(row[0])


def test_cli_output_is_aggregate_only(synthetic_warehouse: BuiltWarehouse) -> None:
    result = runner.invoke(app, ["inspect", "gaps", "--db", str(synthetic_warehouse.path)])
    assert result.exit_code == 0, result.output
    out = result.output
    for needle in ("p50", "p99", "Histogram", "<- current", "<- up to view_cap_sec"):
        assert needle in out
    assert not re.search(r"\d{4}-\d{2}-\d{2}", out)  # no dates or timestamps
    assert not re.search(r"\d{15,}", out)  # no video IDs or hashes
    assert not re.search(r"[0-9a-f]{32,}", out)


def test_not_built_warehouse(tmp_path: Path) -> None:
    con = connect(tmp_path / "wh.duckdb")
    ensure_raw_schema(con)
    with pytest.raises(WarehouseNotBuiltError):
        gap_stats(con, 180, 30)
    con.close()
    result = runner.invoke(app, ["inspect", "gaps", "--db", str(tmp_path / "wh.duckdb")])
    assert result.exit_code == 1
    assert "run `scrollback build` first" in result.output


def test_missing_warehouse(tmp_path: Path) -> None:
    result = runner.invoke(app, ["inspect", "gaps", "--db", str(tmp_path / "nope.duckdb")])
    assert result.exit_code == 1
    assert "run `scrollback ingest` first" in result.output


def test_report_uses_configured_parameters(synthetic_warehouse: BuiltWarehouse) -> None:
    con = synthetic_warehouse.connect()
    report = format_gap_report(gap_stats(con, view_cap_sec=120, session_gap_min=45))
    con.close()
    assert "view_cap_sec = 120 (2m)" in report
    assert "session_gap_min = 45 (45m)" in report
    assert "45 min:" in report and "120 s:" in report


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0s"), (59.4, "59s"), (60, "1m"), (125, "2m05s"), (3600, "1h"), (37500, "10h25m")],
)
def test_fmt_duration(seconds: float, text: str) -> None:
    assert fmt_duration(seconds) == text


def test_cli_help_lists_inspect() -> None:
    result = runner.invoke(app, ["inspect", "--help"])
    assert result.exit_code == 0 and "gaps" in result.output
    assert (REPO_ROOT / "dbt").exists()
