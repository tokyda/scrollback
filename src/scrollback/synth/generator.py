"""Synthetic TikTok export generator (DESIGN §4.5).

The generator models one deterministic "universe" of behaviour per seed. Each behavioural day
(05:00 local to 04:59 the next day) draws from its own RNG seeded by ``(seed, date)``, so an
export is a time window over that universe:

* the same seed and window always produce byte-identical output;
* a shorter window is an exact subset of a longer one with the same seed, which models
  repeated, overlapping real exports.

Events are generated in local time and written in UTC (``SOURCE_TZ``), as a real export is
assumed to be. Alongside the export the generator returns a manifest with the expected
counts per dataset, computed from its own knowledge of the events rather than by re-parsing.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scrollback.ingest.detect import VARIANT_NAMES, Dataset, variant_by_name

GENERATOR_VERSION = 1
DEFAULT_SEED = 42
DEFAULT_DAYS = 180
DEFAULT_END = date(2026, 11, 19)
DEFAULT_LOCAL_TZ = "Asia/Singapore"
DEFAULT_VARIANT = "v2_your_activity"
SOURCE_TZ = "UTC"
EXPORT_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DAY_START_HOUR = 5  # behavioural day runs 05:00 -> 04:59 next day (local)
SESSION_GAP_SEC = 30 * 60  # sessions are separated by more than this

# Relative weight of starting a session in each local hour.
_HOUR_WEIGHTS: dict[int, float] = {
    5: 0.2, 6: 0.6, 7: 1.2, 8: 1.0, 9: 0.5, 10: 0.4, 11: 0.6, 12: 1.4, 13: 1.2, 14: 0.5,
    15: 0.5, 16: 0.7, 17: 1.0, 18: 1.4, 19: 2.0, 20: 2.6, 21: 3.0, 22: 2.8, 23: 2.0,
    0: 1.2, 1: 0.7, 2: 0.35, 3: 0.15, 4: 0.05,
}  # fmt: skip
_WEEKEND_DAYTIME_BOOST = {10, 11, 12, 13, 14, 15, 16}

SEARCH_VOCAB: tuple[str, ...] = (
    "pasta recipes", "home workout", "study with me", "cat videos", "skincare routine",
    "python tutorial", "street food", "travel japan", "budget meals", "football highlights",
    "guitar cover", "productivity tips", "night routine", "dog training", "kpop dance",
    "minimalist desk", "thrift haul", "running tips", "coffee art", "book recommendations",
    "standup comedy", "car detailing", "plant care", "bubble tea", "sunset timelapse",
)  # fmt: skip
SHARE_METHODS: tuple[str, ...] = ("copy_link", "whatsapp", "telegram", "instagram_dm", "sms")


@dataclass(frozen=True)
class InjectedEvent:
    name: str
    start: date
    end: date
    session_mult: float
    length_mult: float
    description: str

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end


INJECTED_EVENTS: tuple[InjectedEvent, ...] = (
    InjectedEvent(
        "holiday_spike", date(2026, 8, 9), date(2026, 8, 9), 2.5, 1.5,
        "Public holiday (Sunday): many more and longer sessions.",
    ),
    InjectedEvent(
        "detox_week", date(2026, 9, 7), date(2026, 9, 13), 0.06, 0.4,
        "Digital detox Monday-Sunday: near-zero use.",
    ),
    InjectedEvent(
        "binge_weekend", date(2026, 10, 24), date(2026, 10, 25), 3.0, 2.5,
        "Binge weekend (Saturday-Sunday): very long sessions.",
    ),
)  # fmt: skip


class NoiseKind:
    DUPLICATE = "duplicate"
    MALFORMED_DATE = "malformed_date"
    NO_VIDEO_ID = "no_video_id"
    MISSING_LINK = "missing_link"


@dataclass
class Event:
    """One record in the universe. ``local_ts`` is the intended time even for noise."""

    dataset: Dataset
    local_ts: datetime
    video_id: str | None = None
    term: str | None = None  # canonical (normalised) search term
    term_styled: str | None = None  # as typed
    method: str | None = None
    shared_content: str | None = None
    link_override: str | None = None  # explicit link (non-video shares, noise)
    date_override: str | None = None  # malformed date text
    omit_link: bool = False
    noise: str | None = None  # NoiseKind, or None for a clean record


@dataclass
class SynthResult:
    export: dict[str, Any]
    manifest: dict[str, Any]
    events: list[Event] = field(repr=False)


# --------------------------------------------------------------------------- behaviour model


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def _new_video_id(rng: random.Random) -> str:
    return str(rng.randint(7_000_000_000_000_000_000, 7_599_999_999_999_999_999))


def _day_rng(seed: int, day: date) -> random.Random:
    return random.Random(f"scrollback-synth-v{GENERATOR_VERSION}|{seed}|{day.isoformat()}")


def _event_for(day: date) -> InjectedEvent | None:
    return next((e for e in INJECTED_EVENTS if e.covers(day)), None)


def _session_start_offsets(rng: random.Random, n: int, weekend: bool) -> list[int]:
    hours = list(_HOUR_WEIGHTS)
    weights = [
        _HOUR_WEIGHTS[h] * (2.0 if weekend and h in _WEEKEND_DAYTIME_BOOST else 1.0) for h in hours
    ]
    offsets = []
    for _ in range(n):
        hour = rng.choices(hours, weights)[0]
        hours_after_start = (hour - DAY_START_HOUR) % 24
        offsets.append(hours_after_start * 3600 + rng.randrange(3600))
    return sorted(offsets)


def _gap_seconds(rng: random.Random) -> int:
    if rng.random() < 0.03:  # occasional long pause that still stays inside the session
        return rng.randint(181, SESSION_GAP_SEC - 60)
    return max(2, min(900, round(rng.lognormvariate(math.log(22), 0.75))))


def _simulate_day(seed: int, day: date) -> list[Event]:
    """All events generated by one behavioural day, starting at 05:00 local on ``day``."""
    rng = _day_rng(seed, day)
    weekend = day.weekday() >= 5
    injected = _event_for(day)
    session_mult = injected.session_mult if injected else 1.0
    length_mult = injected.length_mult if injected else 1.0

    day_start = datetime.combine(day, datetime.min.time()) + timedelta(hours=DAY_START_HOUR)
    day_len = 24 * 3600
    n_sessions = _poisson(rng, (5.5 if weekend else 4.0) * session_mult)

    events: list[Event] = []
    views: list[Event] = []
    session_bounds: list[tuple[int, int]] = []
    seen_today: list[str] = []
    prev_end = -SESSION_GAP_SEC * 2
    for start in _session_start_offsets(rng, n_sessions, weekend):
        start = max(start, prev_end + SESSION_GAP_SEC + rng.randint(60, 3600))
        if start >= day_len:
            break
        n_views = max(1, min(400, round(rng.lognormvariate(math.log(10), 0.85) * length_mult)))
        t = start
        for i in range(n_views):
            if i:
                t += _gap_seconds(rng)
            if t >= day_len:
                break
            if seen_today and rng.random() < 0.03:
                vid = rng.choice(seen_today)  # rewatch
            else:
                vid = _new_video_id(rng)
                seen_today.append(vid)
            view = Event(Dataset.WATCH, day_start + timedelta(seconds=t), video_id=vid)
            views.append(view)
        session_bounds.append((start, t))
        prev_end = t

    liked: set[str] = set()
    for view in views:
        assert view.video_id is not None
        if view.video_id not in liked and rng.random() < 0.045:
            liked.add(view.video_id)
            events.append(
                Event(
                    Dataset.LIKE,
                    view.local_ts + timedelta(seconds=rng.randint(1, 15)),
                    video_id=view.video_id,
                )
            )
        if rng.random() < 0.006:
            events.append(
                Event(
                    Dataset.FAVORITE,
                    view.local_ts + timedelta(seconds=rng.randint(2, 20)),
                    video_id=view.video_id,
                )
            )
        if rng.random() < 0.004:
            events.append(
                Event(
                    Dataset.SHARE,
                    view.local_ts + timedelta(seconds=rng.randint(3, 30)),
                    video_id=view.video_id,
                    method=rng.choice(SHARE_METHODS),
                    shared_content="video",
                )
            )
    if views and rng.random() < 0.04:  # like from a profile page: video never in watch history
        anchor = rng.choice(views)
        events.append(
            Event(
                Dataset.LIKE,
                anchor.local_ts + timedelta(seconds=rng.randint(1, 30)),
                video_id=_new_video_id(rng),
            )
        )
    if views and rng.random() < 0.05:  # non-video share: a creator profile
        anchor = rng.choice(views)
        events.append(
            Event(
                Dataset.SHARE,
                anchor.local_ts + timedelta(seconds=rng.randint(3, 30)),
                method=rng.choice(SHARE_METHODS),
                shared_content="user",
                link_override=f"https://www.tiktok.com/@creator_{rng.randrange(5000):04d}",
            )
        )

    for _ in range(_poisson(rng, 0.7 * session_mult) if session_bounds else 0):
        s_start, _s_end = rng.choice(session_bounds)
        term = rng.choice(SEARCH_VOCAB)
        roll = rng.random()
        styled = term.title() if roll < 0.2 else (term + " " if roll < 0.3 else term)
        events.append(
            Event(
                Dataset.SEARCH,
                day_start + timedelta(seconds=max(0, s_start - rng.randint(5, 60))),
                term=term,
                term_styled=styled,
            )
        )

    events = views + events
    events.extend(_noise(rng, day_start, views, events))
    return events


def _noise(
    rng: random.Random, day_start: datetime, views: list[Event], clean: list[Event]
) -> list[Event]:
    noise: list[Event] = []
    for ev in clean:
        p = {Dataset.WATCH: 0.004, Dataset.LIKE: 0.01}.get(ev.dataset, 0.0)
        if rng.random() < p:
            noise.append(
                Event(
                    ev.dataset,
                    ev.local_ts,
                    video_id=ev.video_id,
                    noise=NoiseKind.DUPLICATE,
                )
            )
    if not views:
        return noise

    def anchor_ts() -> datetime:
        return rng.choice(views).local_ts + timedelta(seconds=rng.randint(1, 9))

    for dataset, p in ((Dataset.WATCH, 0.04), (Dataset.LIKE, 0.01)):
        if rng.random() < p:
            ts = anchor_ts()
            style = rng.randrange(4)
            bad = (
                ts.strftime("%Y/%m/%d %H:%M:%S"),
                ts.strftime("%d-%m-%Y %H:%M"),
                f"{ts.year}-{ts.month:02d}-{ts.day + 40} {ts:%H:%M:%S}",
                "",
            )[style]
            noise.append(
                Event(
                    dataset,
                    ts,
                    video_id=_new_video_id(rng),
                    date_override=bad,
                    noise=NoiseKind.MALFORMED_DATE,
                )
            )
    if rng.random() < 0.04:
        ts = anchor_ts()
        link = (
            f"https://www.tiktokv.com/share/user/{rng.randint(6_000_000_000, 6_999_999_999)}/",
            f"https://www.tiktok.com/@creator_{rng.randrange(5000):04d}",
            f"https://www.tiktok.com/music/sound-{rng.randint(10**17, 10**18 - 1)}",
        )[rng.randrange(3)]
        noise.append(Event(Dataset.WATCH, ts, link_override=link, noise=NoiseKind.NO_VIDEO_ID))
    if rng.random() < 0.006:
        noise.append(
            Event(Dataset.WATCH, anchor_ts(), omit_link=True, noise=NoiseKind.MISSING_LINK)
        )
    return noise


def generate_events(seed: int, start: date, end: date) -> list[Event]:
    """Every event whose intended local time falls in ``[start 00:00, end+1 00:00)``."""
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time())
    events: list[Event] = []
    day = start - timedelta(days=1)  # the previous day's late-night tail lands in the window
    while day <= end:
        events.extend(e for e in _simulate_day(seed, day) if lo <= e.local_ts < hi)
        day += timedelta(days=1)
    return events


# --------------------------------------------------------------------------- rendering


def _handle(video_id: str) -> str:
    return f"creator_{int(video_id) % 5000:04d}"


def _link(variant: str, ev: Event) -> str | None:
    if ev.link_override is not None:
        return ev.link_override
    if ev.video_id is None:
        return None
    if variant == "v2_your_activity" and ev.dataset in (Dataset.LIKE, Dataset.FAVORITE):
        return f"https://www.tiktok.com/@{_handle(ev.video_id)}/video/{ev.video_id}?lang=en"
    return f"https://www.tiktokv.com/share/video/{ev.video_id}/"


def _render(variant: str, ev: Event, tz: ZoneInfo) -> dict[str, Any]:
    utc_text = ev.local_ts.replace(tzinfo=tz).astimezone(UTC).strftime(EXPORT_TS_FORMAT)
    date_text = ev.date_override if ev.date_override is not None else utc_text
    lower = variant == "v2_your_activity" and ev.dataset is Dataset.LIKE
    date_key, link_key = ("date", "link") if lower else ("Date", "Link")

    record: dict[str, Any] = {date_key: date_text}
    if ev.dataset is Dataset.SEARCH:
        record["SearchTerm"] = ev.term_styled
        return record
    if ev.dataset is Dataset.SHARE:
        record["SharedContent"] = ev.shared_content
    if not ev.omit_link:
        record[link_key] = _link(variant, ev)
    if ev.dataset is Dataset.SHARE:
        record["Method"] = ev.method
    return record


def _decoy_sections(variant: str) -> dict[str, Any]:
    profile = {"userName": "synthetic_user", "bioDescription": "synthetic data, not a real person"}
    if variant == "v1_activity":
        return {
            "Profile": {"Profile Information": {"ProfileMap": profile}},
            "App Settings": {"Settings": {"SettingsMap": {"Language": "en"}}},
            "Comment": {"Comments": {"CommentsList": []}},
            "Direct Messages": {"Chat History": {}},
        }
    return {
        "Profile And Settings": {"Profile Info": {"ProfileMap": profile}},
        "Comment": {"Comments": {"CommentsList": []}},
        "Direct Message": {"Direct Messages": {"ChatHistory": {}}},
        "Ads and data": {"Off TikTok Activity": {"OffTikTokActivityDataList": []}},
    }


def render_export(variant: str, events: list[Event], local_tz: str) -> dict[str, Any]:
    spec = variant_by_name(variant)
    tz = ZoneInfo(local_tz)
    ordered = sorted(
        enumerate(events), key=lambda pair: (pair[1].local_ts, pair[0]), reverse=True
    )  # newest first, stable for equal timestamps
    lists: dict[Dataset, list[dict[str, Any]]] = {ds: [] for ds in Dataset}
    for _, ev in ordered:
        lists[ev.dataset].append(_render(variant, ev, tz))

    root: dict[str, Any] = {}
    for dataset in (Dataset.WATCH, Dataset.LIKE, Dataset.FAVORITE, Dataset.SEARCH, Dataset.SHARE):
        section, list_key = spec.paths[dataset]
        root[section] = {list_key: lists[dataset]}
    root["Login History"] = {"LoginHistoryList": []}

    decoys = _decoy_sections(variant)
    first_decoy = next(iter(decoys))
    return {first_decoy: decoys.pop(first_decoy), spec.root: root, **decoys}


# --------------------------------------------------------------------------- manifest


def _expected_key(ev: Event, tz: ZoneInfo) -> tuple[str, str, str]:
    ts = ev.local_ts.replace(tzinfo=tz).astimezone(UTC).strftime(EXPORT_TS_FORMAT)
    if ev.dataset is Dataset.SEARCH:
        key = ev.term or ""
    else:
        key = ev.video_id or ev.link_override or ev.shared_content or ""
    return (ev.dataset.value, ts, key)


def build_manifest(
    events: list[Event],
    *,
    seed: int,
    start: date,
    end: date,
    variant: str,
    local_tz: str,
) -> dict[str, Any]:
    tz = ZoneInfo(local_tz)
    rejected_kinds = {NoiseKind.MALFORMED_DATE, NoiseKind.NO_VIDEO_ID, NoiseKind.MISSING_LINK}
    datasets: dict[str, dict[str, int]] = {}
    for dataset in Dataset:
        evs = [e for e in events if e.dataset is dataset]
        valid = [e for e in evs if e.noise not in rejected_kinds]
        unique = len({_expected_key(e, tz) for e in valid})
        datasets[dataset.value] = {
            "records": len(evs),
            "valid_unique": unique,
            "duplicates": len(valid) - unique,
            "rejected": len(evs) - len(valid),
        }
    totals = {k: sum(d[k] for d in datasets.values()) for k in next(iter(datasets.values()))}
    return {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
        "variant": variant,
        "local_tz": local_tz,
        "source_tz": SOURCE_TZ,
        "injected_events": [
            {
                "name": e.name,
                "start": e.start.isoformat(),
                "end": e.end.isoformat(),
                "description": e.description,
            }
            for e in INJECTED_EVENTS
            if e.start <= end and e.end >= start
        ],
        "expected": {"datasets": datasets, "totals": totals},
        "note": "Synthetic data. Not a real person's viewing history.",
    }


# --------------------------------------------------------------------------- public API


def synthesize(
    *,
    days: int = DEFAULT_DAYS,
    seed: int = DEFAULT_SEED,
    end: date = DEFAULT_END,
    variant: str = DEFAULT_VARIANT,
    local_tz: str = DEFAULT_LOCAL_TZ,
) -> SynthResult:
    if days < 1:
        raise ValueError("days must be >= 1")
    if variant not in VARIANT_NAMES:
        raise ValueError(f"unknown variant {variant!r}; known: {', '.join(VARIANT_NAMES)}")
    start = end - timedelta(days=days - 1)
    events = generate_events(seed, start, end)
    export = render_export(variant, events, local_tz)
    manifest = build_manifest(
        events, seed=seed, start=start, end=end, variant=variant, local_tz=local_tz
    )
    return SynthResult(export=export, manifest=manifest, events=events)


def manifest_path_for(out: Path) -> Path:
    return out.with_name(out.stem + ".manifest.json")


def dumps(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write(result: SynthResult, out: Path) -> Path:
    """Write the export to ``out`` and the manifest next to it; return the manifest path."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps(result.export), encoding="utf-8")
    manifest_path = manifest_path_for(out)
    manifest_path.write_text(dumps(result.manifest), encoding="utf-8")
    return manifest_path
