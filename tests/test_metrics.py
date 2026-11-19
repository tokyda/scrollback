"""knowledge/metrics.yml: schema, and every metric runs and reconciles with the marts."""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from scrollback.semantic.metrics import (
    DEFAULT_METRICS_PATH,
    PARAM_NAMES,
    MetricsFile,
    load_metrics,
    metric_query,
    placeholders,
    render,
)
from tests.conftest import REPO_ROOT, repo_settings

STARTER_SET = {
    "views", "unique_videos", "rewatch_rate", "est_watch_minutes", "active_days",
    "active_days_7d", "sessions", "avg_session_minutes", "binge_sessions", "late_night_share",
    "weekend_share", "likes", "like_rate", "searches", "streak_days",
}  # fmt: skip
DOCS_DIR = REPO_ROOT / "knowledge" / "docs"

METRICS = load_metrics()


def _value(con: duckdb.DuckDBPyConnection, name: str, start: date, end: date) -> Any:
    row = con.execute(metric_query(METRICS.get(name)), {"start": start, "end": end}).fetchone()
    assert row is not None
    return row[0]


def _range(con: duckdb.DuckDBPyConnection) -> tuple[date, date]:
    row = con.execute("SELECT min(local_date), max(local_date) FROM dim_date").fetchone()
    assert row is not None
    return row[0], row[1]


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]


# --------------------------------------------------------------------------- schema


def test_starter_set_is_complete() -> None:
    assert {m.name for m in METRICS.metrics} == STARTER_SET


def test_caveats_and_params_are_known() -> None:
    for metric in METRICS.metrics:
        assert set(metric.caveats) <= set(METRICS.caveats)
        assert set(metric.params) <= PARAM_NAMES


def test_placeholders_are_known_params() -> None:
    texts = METRICS.texts() + [p.read_text() for p in sorted(DOCS_DIR.glob("*.md"))]
    for text in texts:
        assert placeholders(text) <= PARAM_NAMES, placeholders(text) - PARAM_NAMES


def test_render_fills_placeholders_from_config() -> None:
    settings = repo_settings(view_cap_sec=240)
    text = render(METRICS.caveats["pauses_counted_up_to_cap"], settings)
    assert "240 seconds" in text and "{" not in text


def _raw() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_METRICS_PATH.read_text())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["metrics"].append(dict(d["metrics"][0])), "duplicate metric names"),
        (lambda d: d["metrics"][0].update(caveats=["nope"]), "unknown caveats"),
        (lambda d: d["metrics"][0].update(params=["nope"]), "unknown params"),
        (lambda d: d["metrics"][0].update(table="raw_watch"), "table must be a mart"),
        (lambda d: d["metrics"][0].update(sql="COUNT(*) FROM x"), "aggregate expression only"),
        (lambda d: d["metrics"][0].update(sql="COUNT(*); DROP TABLE x"), "without ';'"),
        (
            lambda d: d["metrics"][0].update(type="query", sql="SELECT 1 AS value"),
            "must use",
        ),
        (lambda d: d["metrics"][0].update(extra_field=1), "Extra inputs"),
    ],
)
def test_schema_rejects_bad_definitions(mutate: Any, message: str) -> None:
    data = _raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        MetricsFile.model_validate(data)


def test_load_from_path(tmp_path: Path) -> None:
    path = tmp_path / "metrics.yml"
    path.write_text(DEFAULT_METRICS_PATH.read_text())
    assert load_metrics(path) == METRICS


# --------------------------------------------------------------------------- runs on warehouse


@pytest.mark.parametrize("name", sorted(STARTER_SET))
def test_every_metric_runs_over_full_range(wh: duckdb.DuckDBPyConnection, name: str) -> None:
    start, end = _range(wh)
    value = _value(wh, name, start, end)
    assert value is not None
    assert value >= 0


@pytest.mark.parametrize("name", sorted(m.name for m in METRICS.metrics if m.type == "simple"))
def test_every_simple_metric_runs_per_day(wh: duckdb.DuckDBPyConnection, name: str) -> None:
    start, end = _range(wh)
    rows = wh.execute(metric_query(METRICS.get(name), by_day=True), {"start": start, "end": end})
    series = rows.fetchall()
    assert series
    zero_view_days = {
        d for (d,) in wh.execute("SELECT local_date FROM agg_daily WHERE views = 0").fetchall()
    }
    for day, value in series:
        if value is None:
            # Only a ratio over views may be undefined, and only on a day with no views.
            assert METRICS.get(name).unit == "ratio" and day in zero_view_days, (name, day)


def test_metrics_on_empty_range(wh: duckdb.DuckDBPyConnection) -> None:
    # A range before the data: counts are 0, ratios are NULL (no views), never an error.
    start = end = date(2000, 1, 1)
    assert _value(wh, "views", start, end) == 0
    assert _value(wh, "streak_days", start, end) == 0
    assert _value(wh, "late_night_share", start, end) is None


def test_query_metric_has_no_per_day_series() -> None:
    with pytest.raises(ValueError, match="per-day series not supported"):
        metric_query(METRICS.get("streak_days"), by_day=True)


# --------------------------------------------------------------------------- reconciliation


def test_metrics_reconcile_with_marts(wh: duckdb.DuckDBPyConnection) -> None:
    start, end = _range(wh)

    def v(name: str) -> Any:
        return _value(wh, name, start, end)

    agg = wh.execute(
        "SELECT sum(views), sum(est_watch_min), sum(sessions), sum(late_night_views), "
        "sum(likes), sum(searches), count(*) FILTER (WHERE is_active) FROM agg_daily"
    ).fetchone()
    assert agg is not None
    views, est_min, sessions, late, likes, searches, active = agg

    assert v("views") == views
    assert v("est_watch_minutes") == pytest.approx(est_min)
    assert v("sessions") == sessions
    assert v("late_night_share") * views == pytest.approx(late)
    assert v("likes") == likes
    assert v("searches") == searches
    assert v("like_rate") == pytest.approx(likes / views)
    assert v("active_days") == active
    assert v("binge_sessions") == _scalar(wh, "SELECT count(*) FROM fct_sessions WHERE is_binge")
    assert v("avg_session_minutes") == pytest.approx(
        _scalar(wh, "SELECT sum(est_watch_sec) FROM fct_sessions") / 60 / sessions
    )
    assert v("unique_videos") == _scalar(wh, "SELECT count(DISTINCT video_id) FROM fct_views")
    assert v("rewatch_rate") == pytest.approx(
        _scalar(wh, "SELECT count(*) FILTER (WHERE is_rewatch) FROM fct_views") / views
    )
    weekend = _scalar(wh, "SELECT count(*) FROM fct_views WHERE dow IN (6, 7)")
    assert v("weekend_share") == pytest.approx(weekend / views)


def test_streak_and_trailing_window_match_python(wh: duckdb.DuckDBPyConnection) -> None:
    days = wh.execute("SELECT local_date, is_active FROM agg_daily ORDER BY 1").fetchall()
    active = {d for d, is_active in days if is_active}

    def longest_run(lo: date, hi: date) -> int:
        best = run = 0
        d = lo
        while d <= hi:
            run = run + 1 if d in active else 0
            best = max(best, run)
            d += timedelta(days=1)
        return best

    start, end = days[0][0], days[-1][0]
    windows = [(start, end), (date(2026, 9, 1), date(2026, 9, 30)), (end - timedelta(6), end)]
    for lo, hi in windows:
        assert _value(wh, "streak_days", lo, hi) == longest_run(lo, hi), (lo, hi)
        expected_7d = sum(1 for i in range(7) if hi - timedelta(days=i) in active)
        assert _value(wh, "active_days_7d", lo, hi) == expected_7d, (lo, hi)
