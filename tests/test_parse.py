from datetime import datetime
from typing import Any

import pytest

from scrollback.ingest.detect import Dataset, detect
from scrollback.ingest.parse import (
    ParsedRecord,
    RejectReason,
    extract_video_id,
    normalise_search_term,
    parse_export,
    parse_record,
)

VID = "7300000000000000001"


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (f"https://www.tiktokv.com/share/video/{VID}/", VID),
        (f"https://www.tiktok.com/@creator_0001/video/{VID}?lang=en", VID),
        (f"https://www.tiktok.com/@creator_0001/video/{VID}", VID),
        ("https://www.tiktokv.com/share/user/6800000000000000000/", None),
        ("https://www.tiktok.com/@creator_0001", None),
        ("https://www.tiktok.com/video/", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_video_id(link: str | None, expected: str | None) -> None:
    assert extract_video_id(link) == expected


def test_normalise_search_term() -> None:
    assert normalise_search_term("  Pasta   RECIPES ") == "pasta recipes"
    assert normalise_search_term("ｃａｆé") == "café"  # NFKC folds full-width letters


def _ok(dataset: Dataset, **canonical: Any) -> ParsedRecord:
    parsed = parse_record(dataset, canonical)
    assert isinstance(parsed, ParsedRecord), parsed
    return parsed


def test_valid_watch_record() -> None:
    rec = _ok(Dataset.WATCH, date="2026-03-01 23:59:59", link=f"https://x.com/share/video/{VID}/")
    assert rec.event_ts == datetime(2026, 3, 1, 23, 59, 59)
    assert rec.video_id == VID
    assert rec.event_ts_raw == "2026-03-01 23:59:59"


@pytest.mark.parametrize(
    ("dataset", "canonical", "reason"),
    [
        (Dataset.WATCH, {"link": f"/video/{VID}"}, RejectReason.MISSING_DATE),
        (Dataset.WATCH, {"date": "  ", "link": f"/video/{VID}"}, RejectReason.MISSING_DATE),
        (Dataset.WATCH, {"date": "2026/03/01 10:00:00", "link": f"/video/{VID}"}, "invalid"),
        (Dataset.WATCH, {"date": "2026-02-30 10:00:00", "link": f"/video/{VID}"}, "invalid"),
        (Dataset.WATCH, {"date": "2026-03-01T10:00:00Z", "link": f"/video/{VID}"}, "invalid"),
        (Dataset.WATCH, {"date": 1700000000, "link": f"/video/{VID}"}, "invalid"),
        (Dataset.WATCH, {"date": "2026-03-01 10:00:00"}, RejectReason.MISSING_LINK),
        (Dataset.LIKE, {"date": "2026-03-01 10:00:00", "link": ""}, RejectReason.MISSING_LINK),
        (Dataset.FAVORITE, {"date": "2026-03-01 10:00:00", "link": "N/A"}, "no_video_id"),
        (Dataset.SEARCH, {"date": "2026-03-01 10:00:00", "search_term": " "}, "missing_search"),
    ],
)
def test_rejections(dataset: Dataset, canonical: dict[str, Any], reason: str) -> None:
    parsed = parse_record(dataset, canonical)
    assert isinstance(parsed, RejectReason)
    assert parsed.value.startswith(reason)


def test_share_without_video_is_valid() -> None:
    rec = _ok(
        Dataset.SHARE,
        date="2026-03-01 10:00:00",
        link="https://www.tiktok.com/@creator_0001",
        shared_content="user",
        method="copy_link",
    )
    assert rec.video_id is None
    assert rec.natural_key == "https://www.tiktok.com/@creator_0001"


def test_hash_uses_video_id_not_link_shape() -> None:
    a = _ok(Dataset.LIKE, date="2026-03-01 10:00:00", link=f"https://a/share/video/{VID}/")
    b = _ok(Dataset.LIKE, date="2026-03-01 10:00:00", link=f"https://b/@u/video/{VID}?x=1")
    assert a.record_hash == b.record_hash


def test_hash_differs_by_dataset_time_and_key() -> None:
    base = _ok(Dataset.WATCH, date="2026-03-01 10:00:00", link=f"/video/{VID}")
    other_ds = _ok(Dataset.LIKE, date="2026-03-01 10:00:00", link=f"/video/{VID}")
    other_ts = _ok(Dataset.WATCH, date="2026-03-01 10:00:01", link=f"/video/{VID}")
    other_id = _ok(Dataset.WATCH, date="2026-03-01 10:00:00", link="/video/7300000000000000002")
    hashes = {r.record_hash for r in (base, other_ds, other_ts, other_id)}
    assert len(hashes) == 4


def test_search_hash_uses_normalised_term() -> None:
    a = _ok(Dataset.SEARCH, date="2026-03-01 10:00:00", search_term="Pasta Recipes")
    b = _ok(Dataset.SEARCH, date="2026-03-01 10:00:00", search_term="pasta  recipes ")
    assert a.record_hash == b.record_hash
    assert a.search_term == "Pasta Recipes"  # raw term is kept


def test_parse_export_counts_and_rejects() -> None:
    doc = {
        "Your Activity": {
            "Watch History": {
                "VideoList": [
                    {"Date": "2026-03-01 10:00:00", "Link": f"/video/{VID}"},
                    {"Date": "bad", "Link": f"/video/{VID}"},
                    {"Date": "2026-03-01 10:00:30", "Link": "https://x/share/user/1/"},
                    {"Date": "2026-03-01 10:01:00"},
                ]
            },
            "Like List": {
                "ItemFavoriteList": [{"date": "2026-03-01 10:00:10", "link": "/video/1"}]
            },
        }
    }
    result = parse_export(detect(doc))
    assert result.variant == "v2_your_activity"
    assert result.read_counts[Dataset.WATCH] == 4
    assert len(result.records[Dataset.WATCH]) == 1
    assert len(result.records[Dataset.LIKE]) == 1
    assert result.records[Dataset.SEARCH] == []
    reasons = sorted(r.reason.value for r in result.rejects)
    assert reasons == ["invalid_date", "missing_link", "no_video_id"]
    reject = next(r for r in result.rejects if r.reason is RejectReason.MISSING_LINK)
    assert reject.payload == {"Date": "2026-03-01 10:01:00"}  # original keys preserved
    assert len({r.reject_hash for r in result.rejects}) == 3
