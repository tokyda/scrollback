"""The date spine covers the watch-history window only (DESIGN §4.4, owner decision 2026-11-22).

The first real export had likes reaching back over a year before its watch history; with a
spine over every dataset those days looked like zero-view days instead of unknown ones.
"""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from scrollback.config import TimezoneConfig
from scrollback.warehouse.db import connect, ensure_raw_schema
from scrollback.warehouse.dbt_runner import run_dbt

from .conftest import repo_settings
from .test_watch_time_fixture import _insert


@pytest.fixture(scope="module")
def spine_wh(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("spine_wh")
    db_path = root / "spine.duckdb"
    con = connect(db_path)
    ensure_raw_schema(con)
    views = [("w1", "2026-03-06 10:00:00", "1"), ("w2", "2026-03-08 10:00:00", "2")]
    _insert(con, "raw_watch", [{"record_hash": h, "event_ts_raw": ts, "link": f"/video/{v}",
                                "video_id": v} for h, ts, v in views])  # fmt: skip
    likes = [("l_old", "2025-03-01 12:00:00", "9"), ("l_in", "2026-03-06 10:00:05", "1")]
    _insert(con, "raw_like", [{"record_hash": h, "event_ts_raw": ts, "link": f"/video/{v}",
                               "video_id": v} for h, ts, v in likes])  # fmt: skip
    _insert(con, "raw_search", [{"record_hash": "s1", "event_ts_raw": "2025-06-01 09:00:00",
                                 "search_term": "old search"}])  # fmt: skip
    con.close()
    settings = repo_settings(session_gap_min=30, view_cap_sec=180, default_last_view_sec=20,
                             binge_threshold_min=60)  # fmt: skip
    settings.timezone = TimezoneConfig(source_tz="UTC", local_tz="UTC")
    result = run_dbt(settings, db_path, quiet=True, target_path=root / "target")
    assert result.success, result.failures
    return db_path


def test_spine_is_the_watch_window(spine_wh: Path) -> None:
    con = duckdb.connect(str(spine_wh), read_only=True)
    first, last, n = con.execute(
        "SELECT min(local_date), max(local_date), count(*) FROM dim_date"
    ).fetchone()  # type: ignore[misc]
    assert (first, last, n) == (date(2026, 3, 6), date(2026, 3, 8), 3)
    daily = con.execute("SELECT local_date, views, likes FROM agg_daily ORDER BY 1").fetchall()
    assert daily == [(date(2026, 3, 6), 1, 1), (date(2026, 3, 7), 0, 0), (date(2026, 3, 8), 1, 0)]


def test_events_outside_the_window_stay_in_their_fact_tables(spine_wh: Path) -> None:
    con = duckdb.connect(str(spine_wh), read_only=True)
    assert con.execute("SELECT count(*) FROM fct_likes").fetchone() == (2,)
    assert con.execute("SELECT count(*) FROM fct_searches").fetchone() == (1,)
