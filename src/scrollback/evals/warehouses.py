"""One synthetic warehouse per ``today`` anchor (DESIGN §7.1).

For an anchor date T the warehouse holds the committed sample filtered to records whose
local date is on or before T, as if the export had been taken at the end of T. So a
question asked "on" T can never see later data, and "this month" on the 1st really is one
day. Warehouses are built with the question set's pinned parameters and cached under
``data/warehouse/eval/`` (git-ignored); the cache key covers the sample file, the anchor,
the parameters and the dbt project, so any change rebuilds.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scrollback.config import Settings
from scrollback.evals.questions import EvalParams
from scrollback.ingest.detect import detect
from scrollback.ingest.load import ingest_file
from scrollback.ingest.parse import EXPORT_DATE_FORMAT
from scrollback.warehouse.db import connect
from scrollback.warehouse.dbt_runner import run_dbt

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SAMPLE = REPO_ROOT / "data" / "sample" / "user_data_tiktok.json"
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "warehouse" / "eval"


class EvalWarehouseError(RuntimeError):
    pass


def eval_settings(base: Settings, params: EvalParams) -> Settings:
    """``base`` with the question set's pinned timezone and metric parameters."""
    settings = base.model_copy(deep=True)
    settings.timezone = params.timezone
    settings.metrics = params.metrics
    return settings


def _local_date(raw: Any, source_tz: ZoneInfo, local_tz: ZoneInfo) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        naive = datetime.strptime(raw.strip(), EXPORT_DATE_FORMAT)
    except ValueError:
        return None
    return naive.replace(tzinfo=source_tz).astimezone(local_tz).date()


def filter_export(document: dict[str, Any], today: date, params: EvalParams) -> dict[str, Any]:
    """Copy of the export with every dated record after ``today`` (local) removed.

    Records whose date doesn't parse are kept: ingestion rejects them either way.
    """
    source_tz = ZoneInfo(params.timezone.source_tz)
    local_tz = ZoneInfo(params.timezone.local_tz)
    detected = detect(document)
    out = json.loads(json.dumps(document))
    for section in detected.sections.values():
        root_key, section_key, list_key = section.path
        date_keys = [k for k, canon in section.key_map.items() if canon == "date"]
        kept = []
        for record in section.records:
            raw = next((record[k] for k in date_keys if k in record), None)
            day = _local_date(raw, source_tz, local_tz)
            if day is None or day <= today:
                kept.append(record)
        out[root_key][section_key][list_key] = kept
    return out


def _dbt_fingerprint(dbt_dir: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(dbt_dir.rglob("*")):
        skip = "target" in path.parts or "logs" in path.parts
        if path.is_file() and not skip and path.suffix in {".sql", ".yml", ".yaml"}:
            h.update(str(path.relative_to(dbt_dir)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:16]


def cache_key(sample: Path, today: date, params: EvalParams, dbt_dir: Path) -> str:
    h = hashlib.sha256()
    h.update(hashlib.sha256(sample.read_bytes()).digest())
    h.update(today.isoformat().encode())
    h.update(params.model_dump_json().encode())
    h.update(_dbt_fingerprint(dbt_dir).encode())
    return h.hexdigest()[:12]


def anchor_warehouse(
    today: date,
    *,
    base: Settings,
    params: EvalParams,
    sample: Path = DEFAULT_SAMPLE,
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> Path:
    """Path to the built warehouse for ``today``, building it if not cached."""
    settings = eval_settings(base, params)
    key = cache_key(sample, today, params, settings.paths.dbt_project_dir)
    db_path = cache_dir / f"{today.isoformat()}_{key}.duckdb"
    if db_path.exists():
        return db_path
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale in cache_dir.glob(f"{today.isoformat()}_*"):
        if stale.is_file():
            stale.unlink()

    document = json.loads(sample.read_text(encoding="utf-8"))
    export = cache_dir / f"{today.isoformat()}_{key}.json"
    export.write_text(json.dumps(filter_export(document, today, params)), encoding="utf-8")
    tmp_db = db_path.with_suffix(".building.duckdb")
    tmp_db.unlink(missing_ok=True)
    con = connect(tmp_db)
    try:
        ingest_file(con, export)
    finally:
        con.close()
    result = run_dbt(settings, tmp_db, quiet=True, target_path=cache_dir / f"target_{key}")
    if not result.success:
        raise EvalWarehouseError(f"dbt build failed for anchor {today}: {result.failures}")
    tmp_db.rename(db_path)
    export.unlink()
    return db_path


def build_anchors(
    anchors: Iterable[date], *, base: Settings, params: EvalParams, **kwargs: Any
) -> dict[date, Path]:
    return {a: anchor_warehouse(a, base=base, params=params, **kwargs) for a in sorted(anchors)}


def built_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
