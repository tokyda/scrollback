"""Hand-built fixture: 16 views with sessions and watch time worked out by hand.

ROADMAP Phase 2 "Done when", part 2: the dbt models must reproduce these values EXACTLY.

Parameters used here (set explicitly below, not read from config.yaml, so the expected
values never drift when the real config is tuned):

    session_gap_min       = 30   -> a gap > 1800 s starts a new session; == 1800 s does not
    view_cap_sec          = 180  -> a non-last view counts at most 180 s
    default_last_view_sec = 20   -> used for the last view of a single-view session
    binge_threshold_min   = 5    -> a session with >= 300 s estimated is a binge (lowered from
                                    60 so this small fixture has both binge and non-binge)
    late night            = local hours [0, 5)
    source_tz = local_tz  = UTC  -> raw timestamps ARE local times, so the arithmetic below can
                                    be read straight off the table. (Timezone conversion is
                                    tested separately in test_warehouse_build.py.)

The rule (DESIGN §4.3), for each view in time order:
    gap       = seconds until the next view (in the whole history)
    new session starts when the gap FROM the previous view is > 1800 s
    estimate  = min(gap, 180)                          if the view is not last in its session
              = median(other estimates in session)    if it is last and the session has others
              = 20                                     if it is the only view in its session

------------------------------------------------------------------------------------------
view  time (local)          video  gap to next   session  estimate (seconds)
------------------------------------------------------------------------------------------
Session A  (starts at v01)
v01   2026-03-06 10:00:00   A        40           A        min(40, 180)            =  40
v02   2026-03-06 10:00:40   B        30           A        min(30, 180)            =  30
v03   2026-03-06 10:01:10   C      1800           A        min(1800, 180)          = 180
      ^ a 30-minute pause: gap == 1800 s is NOT > 1800 s, so v04 stays in session A,
        and v03 is credited the full cap (the "pauses counted up to the cap" caveat).
v04   2026-03-06 10:31:10   D        15           A        min(15, 180)            =  15
v05   2026-03-06 10:31:25   E      1801           A        LAST -> median(40, 30, 180, 15)
                                                           sorted 15, 30, 40, 180 -> even
                                                           count, mean of middle two
                                                           (30 + 40) / 2           =  35.0
      Session A total = 40 + 30 + 180 + 15 + 35 = 300 s = 5.0 min -> binge (>= 300 s)

Session B  (starts at v06; gap from v05 is 1801 s > 1800 s -> new session)
v06   2026-03-06 11:01:26   A     14314           B        LAST and ONLY view -> default 20
      ^ video A again: a rewatch.
      Session B total = 20 s -> not a binge

Session C  (11:01:26 -> 15:00:00 is 14314 s > 1800 s -> new session)
v07   2026-03-06 15:00:00   F       100           C        min(100, 180)           = 100
v08   2026-03-06 15:01:40   G     17900           C        LAST -> median(100)     = 100
      Session C total = 200 s -> not a binge

Session D  (15:01:40 -> 20:00:00 is 17900 s -> new session)
v09   2026-03-06 20:00:00   H         0           D        min(0, 180)             =   0
v10   2026-03-06 20:00:00   I       300           D        min(300, 180)           = 180
      ^ v09 and v10 share a second; ties are ordered by view_id, so v09 comes first
        and gets a 0-second gap. v10's 5-minute gap is capped at 180.
v11   2026-03-06 20:05:00   J        12           D        min(12, 180)            =  12
v12   2026-03-06 20:05:12   H     13368           D        LAST -> median(0, 180, 12)
                                                           sorted 0, 12, 180       =  12
      ^ video H again: a rewatch.
      Session D total = 0 + 180 + 12 + 12 = 204 s -> not a binge

Session E  (20:05:12 -> 23:58:00 is 13368 s -> new session; crosses local midnight)
v13   2026-03-06 23:58:00   K        90           E        min(90, 180)            =  90
v14   2026-03-06 23:59:30   L        60           E        min(60, 180)            =  60
v15   2026-03-07 00:00:30   M        30           E        min(30, 180)            =  30
v16   2026-03-07 00:01:00   N      (none)         E        LAST -> median(90, 60, 30)
                                                           sorted 30, 60, 90       =  60
      Session E total = 90 + 60 + 30 + 60 = 240 s -> not a binge
------------------------------------------------------------------------------------------

Daily roll-up (agg_daily). Views credit their OWN date; sessions credit their START date.
    2026-03-06 (Friday):   views = 14 (v01-v14)
                           est   = A 300 + B 20 + C 200 + D 204 + v13 90 + v14 60 = 874 s
                                 = 874 / 60 = 14.5666... min
                           sessions started = 5 (A, B, C, D, E)
                           longest session  = A, 300 s = 5.0 min
                           late-night views = 0
    2026-03-07 (Saturday): views = 2 (v15, v16), est = 30 + 60 = 90 s = 1.5 min
                           sessions started = 0, longest session = 0
                           late-night views = 2 (00:00:30 and 00:01:00 are in [0, 5))
    Check: 874 + 90 = 964 s = sum of the five session totals (300+20+200+204+240).

Likes (liked_after_watch = a view of the same video at or before the like, <= 30 min earlier):
    l1 2026-03-06 10:00:50 video B -> v02 at 10:00:40, 10 s earlier            -> True
    l2 2026-03-06 12:00:00 video Z -> never viewed                             -> False
    l3 2026-03-06 11:40:00 video A -> latest earlier view v06 at 11:01:26 is
                                      38 min 34 s earlier (> 30 min)           -> False
Search: "  Pasta   RECIPES " on 2026-03-07 09:00:00 -> search_term_norm "pasta recipes".
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from scrollback.config import TimezoneConfig
from scrollback.warehouse.db import connect, ensure_raw_schema
from scrollback.warehouse.dbt_runner import run_dbt
from tests.conftest import repo_settings

VIEWS: list[tuple[str, str, str]] = [  # (view_id, raw timestamp, video)
    ("v01", "2026-03-06 10:00:00", "A"),
    ("v02", "2026-03-06 10:00:40", "B"),
    ("v03", "2026-03-06 10:01:10", "C"),
    ("v04", "2026-03-06 10:31:10", "D"),
    ("v05", "2026-03-06 10:31:25", "E"),
    ("v06", "2026-03-06 11:01:26", "A"),
    ("v07", "2026-03-06 15:00:00", "F"),
    ("v08", "2026-03-06 15:01:40", "G"),
    ("v09", "2026-03-06 20:00:00", "H"),
    ("v10", "2026-03-06 20:00:00", "I"),
    ("v11", "2026-03-06 20:05:00", "J"),
    ("v12", "2026-03-06 20:05:12", "H"),
    ("v13", "2026-03-06 23:58:00", "K"),
    ("v14", "2026-03-06 23:59:30", "L"),
    ("v15", "2026-03-07 00:00:30", "M"),
    ("v16", "2026-03-07 00:01:00", "N"),
]
LIKES = [
    ("l1", "2026-03-06 10:00:50", "B"),
    ("l2", "2026-03-06 12:00:00", "Z"),
    ("l3", "2026-03-06 11:40:00", "A"),
]
SEARCHES = [("s1", "2026-03-07 09:00:00", "  Pasta   RECIPES ")]

# view_id -> (session_id, est_watch_sec, is_last_in_session, is_rewatch), from the table above.
EXPECTED_VIEWS: dict[str, tuple[str, float, bool, bool]] = {
    "v01": ("v01", 40.0, False, False),
    "v02": ("v01", 30.0, False, False),
    "v03": ("v01", 180.0, False, False),
    "v04": ("v01", 15.0, False, False),
    "v05": ("v01", 35.0, True, False),
    "v06": ("v06", 20.0, True, True),
    "v07": ("v07", 100.0, False, False),
    "v08": ("v07", 100.0, True, False),
    "v09": ("v09", 0.0, False, False),
    "v10": ("v09", 180.0, False, False),
    "v11": ("v09", 12.0, False, False),
    "v12": ("v09", 12.0, True, True),
    "v13": ("v13", 90.0, False, False),
    "v14": ("v13", 60.0, False, False),
    "v15": ("v13", 30.0, False, False),
    "v16": ("v13", 60.0, True, False),
}

# session_id -> (local_date, n_views, est_watch_sec, is_binge, start, end)
EXPECTED_SESSIONS: dict[str, tuple[date, int, float, bool, str, str]] = {
    "v01": (date(2026, 3, 6), 5, 300.0, True, "2026-03-06 10:00:00", "2026-03-06 10:31:25"),
    "v06": (date(2026, 3, 6), 1, 20.0, False, "2026-03-06 11:01:26", "2026-03-06 11:01:26"),
    "v07": (date(2026, 3, 6), 2, 200.0, False, "2026-03-06 15:00:00", "2026-03-06 15:01:40"),
    "v09": (date(2026, 3, 6), 4, 204.0, False, "2026-03-06 20:00:00", "2026-03-06 20:05:12"),
    "v13": (date(2026, 3, 6), 4, 240.0, False, "2026-03-06 23:58:00", "2026-03-07 00:01:00"),
}

# local_date -> (views, est_watch_min, sessions, longest_session_min, late_night_views,
#                likes, searches, is_weekend, active_days_7d)
EXPECTED_DAILY: dict[date, tuple[Any, ...]] = {
    date(2026, 3, 6): (14, 874 / 60, 5, 5.0, 0, 3, 0, False, 1),
    date(2026, 3, 7): (2, 90 / 60, 0, 0.0, 2, 0, 1, True, 2),
}


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        meta = {"source_file": "fixture.json", "ingest_id": 1, "loaded_at": datetime(2026, 3, 8)}
        full = {**row, **meta}
        cols = ", ".join(full)
        marks = ", ".join("?" for _ in full)
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(full.values()))


@pytest.fixture(scope="module")
def fixture_wh(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fixture_wh")
    db_path = root / "fixture.duckdb"
    con = connect(db_path)
    ensure_raw_schema(con)
    _insert(
        con,
        "raw_watch",
        [
            {"record_hash": vid, "event_ts_raw": ts, "link": f"/video/{v}", "video_id": v}
            for vid, ts, v in VIEWS
        ],
    )
    _insert(
        con,
        "raw_like",
        [
            {"record_hash": lid, "event_ts_raw": ts, "link": f"/video/{v}", "video_id": v}
            for lid, ts, v in LIKES
        ],
    )
    _insert(
        con,
        "raw_search",
        [{"record_hash": sid, "event_ts_raw": ts, "search_term": t} for sid, ts, t in SEARCHES],
    )
    con.close()

    settings = repo_settings(
        session_gap_min=30, view_cap_sec=180, default_last_view_sec=20, binge_threshold_min=5,
        late_night_start_hour=0, late_night_end_hour=5,
    )  # fmt: skip
    settings.timezone = TimezoneConfig(source_tz="UTC", local_tz="UTC")
    result = run_dbt(settings, db_path, quiet=True, target_path=root / "target")
    assert result.success, result.failures
    return db_path


@pytest.fixture
def fx(fixture_wh: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(fixture_wh), read_only=True)


def test_views_match_hand_computed(fx: duckdb.DuckDBPyConnection) -> None:
    rows = fx.execute(
        "SELECT view_id, session_id, est_watch_sec, is_last_in_session, is_rewatch "
        "FROM fct_views ORDER BY view_id"
    ).fetchall()
    got = {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}
    assert got == EXPECTED_VIEWS


def test_sessions_match_hand_computed(fx: duckdb.DuckDBPyConnection) -> None:
    rows = fx.execute(
        "SELECT session_id, local_date, n_views, est_watch_sec, is_binge, "
        "strftime(start_ts_local, '%Y-%m-%d %H:%M:%S'), "
        "strftime(end_ts_local, '%Y-%m-%d %H:%M:%S') "
        "FROM fct_sessions ORDER BY session_id"
    ).fetchall()
    assert {r[0]: tuple(r[1:]) for r in rows} == EXPECTED_SESSIONS


def test_daily_rollup_matches_hand_computed(fx: duckdb.DuckDBPyConnection) -> None:
    rows = fx.execute(
        "SELECT local_date, views, est_watch_min, sessions, longest_session_min, "
        "late_night_views, likes, searches, is_weekend, active_days_7d "
        "FROM agg_daily ORDER BY local_date"
    ).fetchall()
    got = {r[0]: tuple(r[1:]) for r in rows}
    assert got.keys() == EXPECTED_DAILY.keys()
    for day, expected in EXPECTED_DAILY.items():
        views, est_min, *rest = got[day]
        exp_views, exp_est_min, *exp_rest = expected
        assert views == exp_views
        assert est_min == pytest.approx(exp_est_min, abs=1e-12)
        assert rest == exp_rest


def test_midnight_session_split(fx: duckdb.DuckDBPyConnection) -> None:
    # Session E's 240 s: 150 s credited to Friday (v13, v14), 90 s to Saturday (v15, v16);
    # the session itself, and its 4.0 minutes, belong to Friday.
    per_day = fx.execute(
        "SELECT local_date, sum(est_watch_sec) FROM fct_views WHERE session_id = 'v13' "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert per_day == [(date(2026, 3, 6), 150.0), (date(2026, 3, 7), 90.0)]
    late = fx.execute(
        "SELECT view_id FROM fct_views WHERE is_late_night ORDER BY view_id"
    ).fetchall()
    assert late == [("v15",), ("v16",)]


def test_likes_and_searches(fx: duckdb.DuckDBPyConnection) -> None:
    likes = fx.execute("SELECT like_id, liked_after_watch FROM fct_likes ORDER BY 1").fetchall()
    assert likes == [("l1", True), ("l2", False), ("l3", False)]
    assert fx.execute("SELECT search_term_norm FROM fct_searches").fetchall() == [
        ("pasta recipes",)
    ]


def test_totals_reconcile(fx: duckdb.DuckDBPyConnection) -> None:
    views_total = fx.execute("SELECT sum(est_watch_sec) FROM fct_views").fetchone()
    sessions_total = fx.execute("SELECT sum(est_watch_sec) FROM fct_sessions").fetchone()
    daily_total = fx.execute("SELECT sum(est_watch_min) * 60 FROM agg_daily").fetchone()
    assert views_total == sessions_total == (964.0,)
    assert daily_total is not None and daily_total[0] == pytest.approx(964.0)
