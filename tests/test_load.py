import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import pytest

from scrollback.ingest.detect import UnknownExportFormatError
from scrollback.ingest.load import ExportReadError, ingest_file, read_export
from scrollback.warehouse.db import connect, ensure_raw_schema, table_counts

V1 = "7300000000000000001"
V2 = "7300000000000000002"
V3 = "7300000000000000003"


def _doc(watch: list[dict[str, Any]], likes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    activity: dict[str, Any] = {"Watch History": {"VideoList": watch}}
    if likes is not None:
        activity["Like List"] = {"ItemFavoriteList": likes}
    return {"Your Activity": activity, "Profile": {}}


def _w(ts: str, vid: str, shape: str = "share") -> dict[str, str]:
    link = (
        f"https://www.tiktokv.com/share/video/{vid}/"
        if shape == "share"
        else f"https://www.tiktok.com/@creator_0001/video/{vid}"
    )
    return {"Date": ts, "Link": link}


def _write(path: Path, doc: Any) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def con(tmp_path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    connection = connect(tmp_path / "wh" / "test.duckdb")
    yield connection
    connection.close()


def test_load_counts_rejects_and_log(tmp_path: Path, con: duckdb.DuckDBPyConnection) -> None:
    doc = _doc(
        watch=[
            _w("2026-03-01 10:00:00", V1),
            _w("2026-03-01 10:00:00", V1),  # exact duplicate within file
            _w("2026-03-01 10:00:20", V2),
            {"Date": "2026-13-01 10:00:40", "Link": f"/video/{V3}"},  # invalid date
            {"Date": "2026-03-01 10:01:00", "Link": "https://www.tiktok.com/@x"},  # no video id
        ],
        likes=[{"Date": "2026-03-01 10:00:05", "Link": f"/video/{V1}"}],
    )
    summary = ingest_file(con, _write(tmp_path / "export.json", doc))

    assert summary.variant == "v2_your_activity"
    assert summary.source_file == "export.json"
    watch = summary.datasets["watch"]
    assert (watch.read, watch.valid, watch.rejected, watch.new, watch.duplicate) == (5, 3, 2, 2, 1)
    assert summary.datasets["like"].new == 1
    assert summary.datasets["search"].present is False
    assert summary.rejects_new == 2
    assert summary.ignored_sections == ["Profile"]

    counts = table_counts(con)
    assert counts["raw_watch"] == 2 and counts["raw_like"] == 1
    assert counts["raw_rejects"] == 2 and counts["ingest_log"] == 1

    row = con.execute(
        "SELECT event_ts_raw, video_id, source_file FROM raw_watch ORDER BY event_ts_raw"
    ).fetchall()
    assert row == [
        ("2026-03-01 10:00:00", V1, "export.json"),
        ("2026-03-01 10:00:20", V2, "export.json"),
    ]
    reasons = con.execute("SELECT reason FROM raw_rejects ORDER BY reason").fetchall()
    assert reasons == [("invalid_date",), ("no_video_id",)]
    log = con.execute(
        "SELECT rows_read, rows_new, rows_duplicate, rows_rejected, rejects_new, variant "
        "FROM ingest_log"
    ).fetchone()
    assert log == (6, 3, 1, 2, 2, "v2_your_activity")


def test_reingest_same_file_adds_zero_rows(tmp_path: Path, con: duckdb.DuckDBPyConnection) -> None:
    path = _write(
        tmp_path / "export.json",
        _doc([_w("2026-03-01 10:00:00", V1), {"Date": "bad", "Link": f"/video/{V1}"}]),
    )
    first = ingest_file(con, path)
    before = table_counts(con)
    second = ingest_file(con, path)
    after = table_counts(con)

    assert first.rows_new == 1 and first.rejects_new == 1
    assert second.rows_new == 0 and second.rejects_new == 0
    assert second.rows_duplicate == 1
    assert second.ingest_id == first.ingest_id + 1
    # Raw data unchanged; only the audit log grows (DESIGN §4.2).
    assert {k: v for k, v in after.items() if k != "ingest_log"} == {
        k: v for k, v in before.items() if k != "ingest_log"
    }
    assert after["ingest_log"] == before["ingest_log"] + 1


def test_overlapping_exports_merge(tmp_path: Path, con: duckdb.DuckDBPyConnection) -> None:
    a = _doc([_w("2026-03-01 10:00:00", V1), _w("2026-03-01 10:00:20", V2)])
    b = _doc([_w("2026-03-01 10:00:20", V2, shape="user"), _w("2026-03-02 09:00:00", V3)])
    ingest_file(con, _write(tmp_path / "a.json", a))
    summary = ingest_file(con, _write(tmp_path / "b.json", b))
    assert (summary.rows_new, summary.rows_duplicate) == (1, 1)
    vids = [r[0] for r in con.execute("SELECT video_id FROM raw_watch ORDER BY 1").fetchall()]
    assert vids == [V1, V2, V3]


def test_unknown_format_writes_nothing(tmp_path: Path, con: duckdb.DuckDBPyConnection) -> None:
    ensure_raw_schema(con)
    with pytest.raises(UnknownExportFormatError):
        ingest_file(con, _write(tmp_path / "x.json", {"Activities": {}}))
    assert all(v == 0 for v in table_counts(con).values())


def test_read_export_zip_and_bom(tmp_path: Path) -> None:
    payload = json.dumps(_doc([_w("2026-03-01 10:00:00", V1)])).encode("utf-8-sig")
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("TikTok_Data/user_data_tiktok.json", payload)
        zf.writestr("TikTok_Data/readme.txt", b"ignored")
    document, sha, label = read_export(zpath)
    assert "Your Activity" in document
    assert label == "export.zip:user_data_tiktok.json"
    assert len(sha) == 64


def test_read_export_rejects_zip_with_two_json(tmp_path: Path) -> None:
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("a.json", b"{}")
        zf.writestr("b.json", b"{}")
    with pytest.raises(ExportReadError, match="found 2"):
        read_export(zpath)


def test_read_export_invalid_json_does_not_echo_content(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text('{"Your Activity": "secret-value', encoding="utf-8")
    with pytest.raises(ExportReadError) as err:
        read_export(path)
    assert "secret-value" not in str(err.value)
