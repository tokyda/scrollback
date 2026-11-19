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


def test_unknown_record_key_fails_and_names_keys_not_values() -> None:
    doc = v1_doc()
    secret_value = "https://www.tiktokv.com/share/video/7399999999999999999/"
    doc["Activity"]["Video Browsing History"]["VideoList"] = [
        {"Date": "2026-01-01 10:00:00", "Link": secret_value, "WatchDuration": 12}
    ]
    with pytest.raises(UnknownExportFormatError) as err:
        detect(doc)
    message = str(err.value)
    assert "'WatchDuration'" in message
    assert "'Date'" in message and "'Link'" in message
    assert secret_value not in message
    assert "2026-01-01" not in message


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


def test_field_not_allowed_for_dataset() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"]["VideoList"] = [
        {"Date": "2026-01-01 10:00:00", "Link": LINK, "SearchTerm": "x"}
    ]
    with pytest.raises(UnknownExportFormatError, match="'SearchTerm'"):
        detect(doc)


def test_empty_watch_list_is_valid() -> None:
    doc = v1_doc()
    doc["Activity"]["Video Browsing History"]["VideoList"] = []
    assert detect(doc).sections[Dataset.WATCH].records == []
