"""Idempotent load of a parsed export into the raw layer (DESIGN §4.2 steps 4-5)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from scrollback.ingest.detect import Dataset, DetectedExport, detect
from scrollback.ingest.parse import ParsedRecord, ParseResult, Reject, parse_export
from scrollback.warehouse.db import RAW_COLUMNS, RAW_TABLES, ensure_raw_schema


class ExportReadError(ValueError):
    """The export file could not be read as JSON."""


@dataclass
class DatasetCounts:
    present: bool = False
    read: int = 0
    valid: int = 0
    rejected: int = 0
    new: int = 0
    duplicate: int = 0


@dataclass
class IngestSummary:
    ingest_id: int
    source_file: str
    file_sha256: str
    variant: str
    datasets: dict[str, DatasetCounts]
    rejects_new: int
    ignored_sections: list[str] = field(default_factory=list)

    @property
    def rows_read(self) -> int:
        return sum(c.read for c in self.datasets.values())

    @property
    def rows_new(self) -> int:
        return sum(c.new for c in self.datasets.values())

    @property
    def rows_duplicate(self) -> int:
        return sum(c.duplicate for c in self.datasets.values())

    @property
    def rows_rejected(self) -> int:
        return sum(c.rejected for c in self.datasets.values())


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def read_export(path: Path) -> tuple[Any, str, str]:
    """Return (parsed JSON, sha256 of the file bytes, source_file label).

    Accepts a ``.json`` file or a ``.zip`` holding exactly one ``.json``. The label is the
    file's basename (never the full path), plus the inner name for zips.
    """
    raw = path.read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    label = path.name
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            members = [
                n for n in zf.namelist() if n.lower().endswith(".json") and "__MACOSX" not in n
            ]
            if len(members) != 1:
                raise ExportReadError(
                    f"{path.name}: expected exactly one .json inside the zip, found {len(members)}"
                )
            raw = zf.read(members[0])
            label = f"{path.name}:{Path(members[0]).name}"
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Position only; never echo file content.
        raise ExportReadError(f"{label}: not valid UTF-8 JSON ({type(exc).__name__})") from None
    return document, file_sha, label


def _dedupe_records(records: list[ParsedRecord]) -> dict[str, ParsedRecord]:
    unique: dict[str, ParsedRecord] = {}
    for rec in records:
        unique.setdefault(rec.record_hash, rec)
    return unique


def _count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _insert_columns(
    con: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[str],
    values: list[list[Any]],
    constants: dict[str, Any],
) -> None:
    """Bulk ``INSERT ... ON CONFLICT DO NOTHING`` from column lists via ``unnest``."""
    if not values or not values[0]:
        return
    select_parts = [f"unnest(${i + 1}::VARCHAR[])" for i in range(len(columns))]
    params: list[Any] = list(values)
    for name in constants:
        params.append(constants[name])
        select_parts.append(f"${len(params)}")
    all_columns = ", ".join([*columns, *constants])
    con.execute(
        f"INSERT INTO {table} ({all_columns}) SELECT {', '.join(select_parts)} "
        "ON CONFLICT DO NOTHING",
        params,
    )


def _load_dataset(
    con: duckdb.DuckDBPyConnection,
    dataset: Dataset,
    records: list[ParsedRecord],
    meta: dict[str, Any],
) -> int:
    table = RAW_TABLES[dataset]
    unique = _dedupe_records(records)
    columns = ["record_hash", *RAW_COLUMNS[dataset]]
    values: list[list[Any]] = [list(unique.keys())]
    for col in RAW_COLUMNS[dataset]:
        values.append([getattr(rec, col) for rec in unique.values()])
    before = _count(con, table)
    _insert_columns(con, table, columns, values, meta)
    return _count(con, table) - before


def _load_rejects(
    con: duckdb.DuckDBPyConnection, rejects: list[Reject], meta: dict[str, Any]
) -> int:
    unique: dict[str, Reject] = {}
    for rej in rejects:
        unique.setdefault(rej.reject_hash, rej)
    before = _count(con, "raw_rejects")
    _insert_columns(
        con,
        "raw_rejects",
        ["reject_hash", "dataset", "payload", "reason"],
        [
            list(unique.keys()),
            [r.dataset.value for r in unique.values()],
            [r.payload_json for r in unique.values()],
            [r.reason.value for r in unique.values()],
        ],
        meta,
    )
    return _count(con, "raw_rejects") - before


def load_parsed(
    con: duckdb.DuckDBPyConnection,
    detected: DetectedExport,
    parsed: ParseResult,
    source_file: str,
    file_sha256: str,
) -> IngestSummary:
    """Load parsed records and rejects in one transaction and write an ``ingest_log`` row."""
    ensure_raw_schema(con)
    started_at = _utcnow()
    id_row = con.execute("SELECT nextval('ingest_id_seq')").fetchone()
    assert id_row is not None
    ingest_id = int(id_row[0])
    meta = {"source_file": source_file, "ingest_id": ingest_id, "loaded_at": started_at}

    counts: dict[str, DatasetCounts] = {}
    for dataset in Dataset:
        records = parsed.records.get(dataset, [])
        counts[dataset.value] = DatasetCounts(
            present=dataset in detected.sections,
            read=parsed.read_counts[dataset],
            valid=len(records),
            rejected=sum(1 for r in parsed.rejects if r.dataset is dataset),
        )

    con.execute("BEGIN TRANSACTION")
    try:
        for dataset in Dataset:
            c = counts[dataset.value]
            c.new = _load_dataset(con, dataset, parsed.records.get(dataset, []), meta)
            c.duplicate = c.valid - c.new
        rejects_new = _load_rejects(con, parsed.rejects, meta)
        summary = IngestSummary(
            ingest_id=ingest_id,
            source_file=source_file,
            file_sha256=file_sha256,
            variant=parsed.variant,
            datasets=counts,
            rejects_new=rejects_new,
            ignored_sections=list(detected.ignored_top_level),
        )
        con.execute(
            "INSERT INTO ingest_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ingest_id,
                source_file,
                file_sha256,
                parsed.variant,
                started_at,
                _utcnow(),
                summary.rows_read,
                summary.rows_new,
                summary.rows_duplicate,
                summary.rows_rejected,
                rejects_new,
                json.dumps({k: asdict(v) for k, v in counts.items()}),
            ],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return summary


def ingest_file(con: duckdb.DuckDBPyConnection, path: Path) -> IngestSummary:
    """Read, detect, parse and load one export file. Shape errors raise before any write."""
    document, file_sha, label = read_export(path)
    detected = detect(document)
    parsed = parse_export(detected)
    return load_parsed(con, detected, parsed, source_file=label, file_sha256=file_sha)
