import logging
from typing import Any

import pytest

from scrollback.ingest.detect import Dataset, UnknownExportFormatError, detect

LINK = "https://www.tiktokv.com/share/video/7300000000000000001/"


def v1_doc() -> dict[str, Any]:
    return {
        "Activity": {
            "Video Browsing History": {
                "VideoList": [{"Date": "2026-01-01 10:00:00", "Link": LINK}]
            },
            "Like List": {"ItemFavoriteList": [{"Date": "2026-01-01 10:00:05", "Link": LINK}]},
            "Search History": {"SearchList": [{"Date": "2026-01-01 09:59:00", "SearchTerm": "x"}]},
            "Login History": {"LoginHistoryList": []},
        },
        "Profile": {"Profile Information": {}},
    }


def v2_doc() -> dict[str, Any]:
    return {
        "Your Activity": {
            "Watch History": {"VideoList": [{"Date": "2026-01-01 10:00:00", "Link": LINK}]},
            "Like List": {"ItemFavoriteList": [{"date": "2026-01-01 10:00:05", "link": LINK}]},
            "Favorite Videos": {"FavoriteVideoList": None},
            "Share History": {
                "ShareHistoryList": [
                    {"Date": "2026-01-01 10:01:00", "SharedContent": "video", "Method": "x"}
                ]
            },
        },
    }


def v3_doc() -> dict[str, Any]:
    """The layout of the first real export (2026-11), with made-up values: likes and
    favourites under a top-level "Likes and Favorites", searches under "Searches"."""
    return {
        "Comment": {"Comments": {}},
        "Likes and Favorites": {
            "Collection": {},
            "Favorite Videos": {
                "App": 0,
                "FavoriteVideoList": [{"Date": "2026-01-01 10:00:07", "Link": LINK}],
            },
            "Like List": {
                "App": 0,
                "ItemFavoriteList": [{"date": "2026-01-01 10:00:05", "link": LINK}],
            },
        },
        "Your Activity": {
            "Watch History": {"VideoList": [{"Date": "2026-01-01 10:00:00", "Link": LINK}]},
            "Searches": {"SearchList": [{"Date": "2026-01-01 09:59:00", "SearchTerm": "x"}]},
            "Share History": {"ShareHistoryList": None},
            "Login History": {"LoginHistoryList": []},
        },
    }


def test_detects_v3_likes_favorites_layout() -> None:
    detected = detect(v3_doc())
    assert detected.variant == "v3_likes_favorites"
    assert set(detected.sections) == set(Dataset) and detected.absent == []
    like = detected.sections[Dataset.LIKE]
    assert like.path == ("Likes and Favorites", "Like List", "ItemFavoriteList")
    assert like.key_map == {"date": "date", "link": "link"} and like.unknown_keys == []
    assert detected.sections[Dataset.SEARCH].path == ("Your Activity", "Searches", "SearchList")
    assert "Comment" in detected.ignored_top_level
    assert "Likes and Favorites > Collection" in detected.ignored_top_level
    assert "Your Activity > Login History" in detected.ignored_top_level


def test_moved_section_fails_loudly_instead_of_dropping_it() -> None:
    # The real export under the v2 rules: likes moved out of the activity root. Before v3 this
    # loaded without likes, favourites and searches, silently.
    doc = v3_doc()
    doc["Likes & Favs"] = doc.pop("Likes and Favorites")  # an unknown container name
    with pytest.raises(UnknownExportFormatError) as err:
        detect(doc)
    message = str(err.value)
    assert "v2_your_activity" in message and "'ItemFavoriteList'" in message
    assert "Likes & Favs > Like List > ItemFavoriteList" in message
    assert LINK not in message and "2026-01-01" not in message  # keys only, never values


def test_detects_v1() -> None:
    detected = detect(v1_doc())
    assert detected.variant == "v1_activity"
    assert set(detected.sections) == {Dataset.WATCH, Dataset.LIKE, Dataset.SEARCH}
    assert set(detected.absent) == {Dataset.FAVORITE, Dataset.SHARE}
    assert "Profile" in detected.ignored_top_level
    assert "Activity > Login History" in detected.ignored_top_level
    watch = detected.sections[Dataset.WATCH]
    assert watch.path == ("Activity", "Video Browsing History", "VideoList")
    assert watch.canonical(watch.records[0]) == {"date": "2026-01-01 10:00:00", "link": LINK}


def test_detects_v2_with_lowercase_keys_and_null_list() -> None:
    detected = detect(v2_doc())
    assert detected.variant == "v2_your_activity"
    like = detected.sections[Dataset.LIKE]
    assert like.key_map == {"date": "date", "link": "link"}
    assert detected.sections[Dataset.FAVORITE].records == []
    share = detected.sections[Dataset.SHARE]
    assert share.key_map["SharedContent"] == "shared_content"


def test_unknown_root_names_top_level_keys() -> None:
    with pytest.raises(UnknownExportFormatError) as err:
        detect({"Profile": {}, "Activities": {}})
    message = str(err.value)
    assert "'Activities'" in message and "'Profile'" in message
    assert "'Activity'" in message and "'Your Activity'" in message


def test_top_level_not_object() -> None:
    with pytest.raises(UnknownExportFormatError, match="top level is list"):
        detect([1, 2])


def test_ambiguous_roots() -> None:
    doc = {**v1_doc(), **v2_doc()}
    with pytest.raises(UnknownExportFormatError, match="Ambiguous"):
        detect(doc)


def test_missing_watch_section_names_keys() -> None:
    doc = v1_doc()
    del doc["Activity"]["Video Browsing History"]
    with pytest.raises(UnknownExportFormatError) as err:
        detect(doc)
    assert "'Like List'" in str(err.value)
    assert "Video Browsing History" in str(err.value)


def test_missing_list_key_names_keys() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"] = {"Videos": []}
    with pytest.raises(UnknownExportFormatError, match=r"missing list key 'VideoList'.*'Videos'"):
        detect(doc)


def test_unknown_record_key_warns_and_is_kept_as_extra(caplog: pytest.LogCaptureFixture) -> None:
    doc = v1_doc()
    secret_value = "https://www.tiktokv.com/share/video/7399999999999999999/"
    record = {"Date": "2026-01-01 10:00:00", "Link": secret_value, "WatchDuration": 12}
    doc["Activity"]["Video Browsing History"]["VideoList"] = [record]
    with caplog.at_level(logging.WARNING, logger="scrollback.ingest.detect"):
        detected = detect(doc)

    watch = detected.sections[Dataset.WATCH]
    assert watch.unknown_keys == ["WatchDuration"]
    assert watch.canonical(record) == {"date": "2026-01-01 10:00:00", "link": secret_value}
    assert watch.extras(record) == {"WatchDuration": 12}

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "'WatchDuration'" in warnings[0]
    assert "extras" in warnings[0]
    # Key names only: no values from the record leak into the log.
    assert secret_value not in warnings[0]
    assert "2026-01-01" not in warnings[0]
    assert "12" not in warnings[0]


def test_no_warning_when_all_keys_known(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="scrollback.ingest.detect"):
        detected = detect(v1_doc())
    assert not caplog.records
    assert all(not s.unknown_keys for s in detected.sections.values())


def test_unknown_keys_still_require_known_fields() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"]["VideoList"] = [
        {"Timestamp": "2026-01-01 10:00:00", "URL": LINK}
    ]
    with pytest.raises(UnknownExportFormatError) as err:
        detect(doc)
    message = str(err.value)
    assert "required field(s) ['date', 'link']" in message
    assert "'Timestamp'" in message and "'URL'" in message
    assert LINK not in message


def test_required_field_absent_from_every_record() -> None:
    doc = v1_doc()
    doc["Activity"]["Search History"]["SearchList"] = [{"Date": "2026-01-01 10:00:00"}]
    with pytest.raises(UnknownExportFormatError, match=r"required field\(s\) \['search_term'\]"):
        detect(doc)


def test_non_object_record_fails() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"]["VideoList"] = ["2026-01-01 10:00:00"]
    with pytest.raises(UnknownExportFormatError, match=r"VideoList\[0\]: expected an object"):
        detect(doc)


def test_list_not_a_list_fails() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"]["VideoList"] = {"a": 1}
    with pytest.raises(UnknownExportFormatError, match="expected a list, found dict"):
        detect(doc)


def test_field_from_another_dataset_is_an_extra() -> None:
    doc = v1_doc()
    record = {"Date": "2026-01-01 10:00:00", "Link": LINK, "SearchTerm": "x"}
    doc["Activity"]["Video Browsing History"]["VideoList"] = [record]
    watch = detect(doc).sections[Dataset.WATCH]
    assert watch.unknown_keys == ["SearchTerm"]
    assert watch.extras(record) == {"SearchTerm": "x"}


def test_empty_watch_list_is_valid() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"]["VideoList"] = []
    assert detect(doc).sections[Dataset.WATCH].records == []
