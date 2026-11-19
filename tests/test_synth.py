import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from scrollback.ingest.detect import VARIANT_NAMES, Dataset, detect
from scrollback.ingest.parse import parse_export
from scrollback.synth.generator import (
    DEFAULT_END,
    INJECTED_EVENTS,
    Event,
    NoiseKind,
    dumps,
    synthesize,
)


@pytest.fixture(scope="module")
def full() -> list[Event]:
    return synthesize().events


def _clean_views(events: list[Event]) -> list[Event]:
    return [e for e in events if e.dataset is Dataset.WATCH and e.noise is None]


def _daily_views(events: list[Event]) -> Counter[date]:
    return Counter(e.local_ts.date() for e in _clean_views(events))


def test_deterministic_for_seed() -> None:
    a = synthesize(days=30, seed=7)
    b = synthesize(days=30, seed=7)
    assert dumps(a.export) == dumps(b.export)
    assert dumps(a.manifest) == dumps(b.manifest)


def test_different_seeds_differ() -> None:
    assert dumps(synthesize(days=30, seed=1).export) != dumps(synthesize(days=30, seed=2).export)


def test_shorter_window_is_exact_subset() -> None:
    def keys(events: list[Event]) -> Counter[tuple[str, datetime, str, str]]:
        return Counter(
            (
                e.dataset.value,
                e.local_ts,
                e.video_id or e.link_override or e.term or "",
                e.noise or "",
            )
            for e in events
        )

    long = synthesize(days=60, end=DEFAULT_END).events
    short_late = synthesize(days=20, end=DEFAULT_END).events
    short_early = synthesize(days=20, end=DEFAULT_END - timedelta(days=40)).events
    long_keys = keys(long)
    assert sum(keys(short_late).values()) > 0
    assert not keys(short_late) - long_keys
    assert not keys(short_early) - long_keys


def test_window_bounds(full: list[Event]) -> None:
    dates = {e.local_ts.date() for e in full}
    assert min(dates) == DEFAULT_END - timedelta(days=179)
    assert max(dates) == DEFAULT_END


@pytest.mark.parametrize("variant", VARIANT_NAMES)
def test_variants_round_trip_through_parser(variant: str) -> None:
    result = synthesize(days=21, variant=variant)
    detected = detect(result.export)
    assert detected.variant == variant
    parsed = parse_export(detected)
    expected = result.manifest["expected"]["datasets"]
    for dataset in Dataset:
        exp = expected[dataset.value]
        got_valid = parsed.records[dataset]
        got_rejected = [r for r in parsed.rejects if r.dataset is dataset]
        assert len(got_valid) + len(got_rejected) == exp["records"], dataset
        assert len(got_rejected) == exp["rejected"], dataset
        assert len({r.record_hash for r in got_valid}) == exp["valid_unique"], dataset


def test_variants_differ_in_shape() -> None:
    v1 = synthesize(days=7, variant="v1_activity").export
    v2 = synthesize(days=7, variant="v2_your_activity").export
    assert "Video Browsing History" in v1["Activity"]
    assert "Watch History" in v2["Your Activity"]
    like_v2 = v2["Your Activity"]["Like List"]["ItemFavoriteList"][0]
    assert set(like_v2) == {"date", "link"}
    assert "tiktok.com/@" in like_v2["link"]
    like_v1 = v1["Activity"]["Like List"]["ItemFavoriteList"][0]
    assert set(like_v1) == {"Date", "Link"}


def test_timestamps_written_in_utc() -> None:
    result = synthesize(days=3)
    ev = next(e for e in result.events if e.dataset is Dataset.WATCH and e.noise is None)
    expected = (
        ev.local_ts.replace(tzinfo=ZoneInfo("Asia/Singapore"))
        .astimezone(ZoneInfo("UTC"))
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    dates = [r["Date"] for r in result.export["Your Activity"]["Watch History"]["VideoList"]]
    assert expected in dates
    assert ev.local_ts.strftime("%Y-%m-%d %H:%M:%S") not in dates  # +8h offset applied


def test_export_is_newest_first() -> None:
    rows = synthesize(days=5).export["Your Activity"]["Watch History"]["VideoList"]
    dates = [r["Date"] for r in rows if r.get("Date", "")[:2] == "20" and r["Date"][4] == "-"]
    assert dates == sorted(dates, reverse=True)


def test_behaviour_rates(full: list[Event]) -> None:
    views = _clean_views(full)
    likes = [e for e in full if e.dataset is Dataset.LIKE and e.noise is None]
    assert 0.03 <= len(likes) / len(views) <= 0.06
    searches = [e for e in full if e.dataset is Dataset.SEARCH]
    assert len(searches) >= 50 and len({e.term for e in searches}) <= 25
    rewatches = len(views) - len({e.video_id for e in views})
    assert rewatches > 0


def test_hour_of_day_curve(full: list[Event]) -> None:
    hours = Counter(e.local_ts.hour for e in _clean_views(full))
    evening = sum(hours[h] for h in range(19, 24))
    morning = sum(hours[h] for h in range(5, 10))
    late_night = sum(hours[h] for h in range(0, 5))
    assert evening > 2 * morning
    assert late_night > 0


def test_weekend_heavier_than_weekday(full: list[Event]) -> None:
    daily = _daily_views(full)
    event_days = {d for e in INJECTED_EVENTS for d in _days(e.start, e.end)}
    weekend = [n for d, n in daily.items() if d.weekday() >= 5 and d not in event_days]
    weekday = [n for d, n in daily.items() if d.weekday() < 5 and d not in event_days]
    assert statistics.mean(weekend) > statistics.mean(weekday)


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def test_injected_events_visible(full: list[Event]) -> None:
    daily = _daily_views(full)
    all_days = _days(DEFAULT_END - timedelta(days=179), DEFAULT_END)
    median = statistics.median(daily.get(d, 0) for d in all_days)
    by_name = {e.name: e for e in INJECTED_EVENTS}

    detox = by_name["detox_week"]
    detox_total = sum(daily.get(d, 0) for d in _days(detox.start, detox.end))
    assert detox_total < 0.15 * 7 * median

    binge = by_name["binge_weekend"]
    for d in _days(binge.start, binge.end):
        assert daily.get(d, 0) > 3 * median

    holiday = by_name["holiday_spike"]
    assert daily.get(holiday.start, 0) > 2 * median


def test_noise_present(full: list[Event]) -> None:
    kinds = Counter(e.noise for e in full if e.noise)
    assert kinds[NoiseKind.DUPLICATE] > 0
    assert kinds[NoiseKind.MALFORMED_DATE] > 0
    assert kinds[NoiseKind.NO_VIDEO_ID] > 0
    total = len(full)
    assert sum(kinds.values()) < 0.02 * total  # noise stays "a few"


def test_manifest_contents() -> None:
    result = synthesize(days=180)
    m = result.manifest
    assert m["seed"] == 42 and m["days"] == 180 and m["variant"] == "v2_your_activity"
    assert m["source_tz"] == "UTC" and m["local_tz"] == "Asia/Singapore"
    assert {e["name"] for e in m["injected_events"]} == {e.name for e in INJECTED_EVENTS}
    totals = m["expected"]["totals"]
    assert totals["records"] == totals["valid_unique"] + totals["duplicates"] + totals["rejected"]
    assert synthesize(days=7).manifest["injected_events"] == []


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="days"):
        synthesize(days=0)
    with pytest.raises(ValueError, match="unknown variant"):
        synthesize(variant="v9")
