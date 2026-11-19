"""DuckDB connection and raw-layer schema (DESIGN §4.4 "raw")."""

from __future__ import annotations

from pathlib import Path

import duckdb

from scrollback.ingest.detect import Dataset

RAW_TABLES: dict[Dataset, str] = {
    Dataset.WATCH: "raw_watch",
    Dataset.LIKE: "raw_like",
    Dataset.FAVORITE: "raw_favorite",
    Dataset.SEARCH: "raw_search",
    Dataset.SHARE: "raw_share",
}

# Columns specific to each raw table, between record_hash and the load metadata.
RAW_COLUMNS: dict[Dataset, tuple[str, ...]] = {
    Dataset.WATCH: ("event_ts_raw", "link", "video_id"),
    Dataset.LIKE: ("event_ts_raw", "link", "video_id"),
    Dataset.FAVORITE: ("event_ts_raw", "link", "video_id"),
    Dataset.SEARCH: ("event_ts_raw", "search_term"),
    Dataset.SHARE: ("event_ts_raw", "link", "video_id", "method", "shared_content"),
}

_LOAD_METADATA = (
    "source_file VARCHAR NOT NULL, ingest_id BIGINT NOT NULL, loaded_at TIMESTAMP NOT NULL"
)


def _raw_table_ddl(dataset: Dataset) -> str:
    cols = ",\n    ".join(
        f"{c} VARCHAR NOT NULL" if c == "event_ts_raw" else f"{c} VARCHAR"
        for c in RAW_COLUMNS[dataset]
    )
    return (
        f"CREATE TABLE IF NOT EXISTS {RAW_TABLES[dataset]} (\n"
        f"    record_hash VARCHAR PRIMARY KEY,\n    {cols},\n    {_LOAD_METADATA}\n)"
    )


SCHEMA_DDL: tuple[str, ...] = (
    "CREATE SEQUENCE IF NOT EXISTS ingest_id_seq START 1",
    *(_raw_table_ddl(ds) for ds in Dataset),
    """
    CREATE TABLE IF NOT EXISTS raw_rejects (
        reject_hash VARCHAR PRIMARY KEY,
        dataset VARCHAR NOT NULL,
        payload JSON NOT NULL,
        reason VARCHAR NOT NULL,
        source_file VARCHAR NOT NULL,
        ingest_id BIGINT NOT NULL,
        loaded_at TIMESTAMP NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingest_log (
        ingest_id BIGINT PRIMARY KEY,
        source_file VARCHAR NOT NULL,
        file_sha256 VARCHAR NOT NULL,
        variant VARCHAR NOT NULL,
        started_at TIMESTAMP NOT NULL,
        finished_at TIMESTAMP NOT NULL,
        rows_read BIGINT NOT NULL,
        rows_new BIGINT NOT NULL,
        rows_duplicate BIGINT NOT NULL,
        rows_rejected BIGINT NOT NULL,
        rejects_new BIGINT NOT NULL,
        dataset_counts JSON NOT NULL
    )
    """,
)


def connect(path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def ensure_raw_schema(con: duckdb.DuckDBPyConnection) -> None:
    for statement in SCHEMA_DDL:
        con.execute(statement)


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count for every raw table plus raw_rejects and ingest_log."""
    names = [*RAW_TABLES.values(), "raw_rejects", "ingest_log"]
    counts: dict[str, int] = {}
    for name in names:
        row = con.execute(f"SELECT count(*) FROM {name}").fetchone()
        counts[name] = int(row[0]) if row else 0
    return counts
