"""End-to-end: synthetic export -> CLI ingest -> raw layer (ROADMAP Phase 1 "Done when")."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from typer.testing import CliRunner

from scrollback.cli import app
from scrollback.ingest.detect import Dataset
from scrollback.ingest.load import ingest_file
from scrollback.synth.generator import DEFAULT_END, dumps, manifest_path_for, synthesize, write
from scrollback.warehouse.db import RAW_TABLES, connect, table_counts

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = REPO_ROOT / "data" / "sample"
SAMPLES = {
    "v2_your_activity": SAMPLE_DIR / "user_data_tiktok.json",
    "v1_activity": SAMPLE_DIR / "user_data_tiktok_v1.json",
}
runner = CliRunner()


def _expected_raw_counts(manifest: dict[str, Any]) -> dict[str, int]:
    datasets = manifest["expected"]["datasets"]
    counts = {RAW_TABLES[ds]: datasets[ds.value]["valid_unique"] for ds in Dataset}
    counts["raw_rejects"] = manifest["expected"]["totals"]["rejected"]
    return counts


def _raw_only(counts: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in counts.items() if k != "ingest_log"}


@pytest.mark.parametrize("variant", sorted(SAMPLES))
def test_committed_sample_matches_generator(variant: str) -> None:
    path = SAMPLES[variant]
    result = synthesize(variant=variant)
    assert path.read_text(encoding="utf-8") == dumps(result.export), (
        f"{path.name} is stale: regenerate with `scrollback synth`"
    )
    assert manifest_path_for(path).read_text(encoding="utf-8") == dumps(result.manifest)


@pytest.mark.parametrize("variant", sorted(SAMPLES))
def test_cli_synth_then_ingest_twice(tmp_path: Path, variant: str) -> None:
    export = tmp_path / "export.json"
    db = tmp_path / "wh.duckdb"
    synth = runner.invoke(app, ["synth", "--variant", variant, "--out", str(export)])
    assert synth.exit_code == 0, synth.output
    manifest = json.loads(manifest_path_for(export).read_text())

    first = runner.invoke(app, ["ingest", str(export), "--db", str(db)])
    assert first.exit_code == 0, first.output
    assert f"variant {variant}" in first.output
    con = connect(db)
    after_first = table_counts(con)
    con.close()
    assert _raw_only(after_first) == _expected_raw_counts(manifest)
    assert after_first["ingest_log"] == 1

    second = runner.invoke(app, ["ingest", str(export), "--db", str(db)])
    assert second.exit_code == 0, second.output
    con = connect(db)
    after_second = table_counts(con)
    log = con.execute(
        "SELECT rows_new, rejects_new, rows_duplicate FROM ingest_log ORDER BY ingest_id"
    ).fetchall()
    con.close()
    assert _raw_only(after_second) == _raw_only(after_first)
    totals = manifest["expected"]["totals"]
    assert log[0] == (totals["valid_unique"], totals["rejected"], totals["duplicates"])
    assert log[1] == (0, 0, totals["valid_unique"] + totals["duplicates"])


def test_same_history_in_both_variants_dedupes(tmp_path: Path) -> None:
    con = connect(tmp_path / "wh.duckdb")
    v1, v2 = tmp_path / "v1.json", tmp_path / "v2.json"
    write(synthesize(days=30, variant="v1_activity"), v1)
    write(synthesize(days=30, variant="v2_your_activity"), v2)
    first = ingest_file(con, v1)
    second = ingest_file(con, v2)
    assert first.rows_new > 0
    assert second.rows_new == 0
    # Rejects dedupe by exact payload, so like rejects (whose keys differ by variant) may be
    # stored once per variant. That is accepted: raw_rejects is an audit table.
    con.close()


def test_overlapping_exports_merge_to_full_history(tmp_path: Path) -> None:
    early, late, full = tmp_path / "early.json", tmp_path / "late.json", tmp_path / "full.json"
    write(synthesize(days=120, end=DEFAULT_END - timedelta(days=60)), early)
    write(synthesize(days=120, end=DEFAULT_END), late)
    full_result = synthesize(days=180, end=DEFAULT_END)
    write(full_result, full)

    con = connect(tmp_path / "merged.duckdb")
    s1 = ingest_file(con, early)
    s2 = ingest_file(con, late)
    merged = table_counts(con)
    con.close()

    assert s2.rows_duplicate > 0  # the 60-day overlap was recognised
    assert _raw_only(merged) == _expected_raw_counts(full_result.manifest)
    assert s1.rows_new + s2.rows_new == full_result.manifest["expected"]["totals"]["valid_unique"]

    con = connect(tmp_path / "merged.duckdb")
    s3 = ingest_file(con, full)
    con.close()
    assert s3.rows_new == 0 and s3.rejects_new == 0


def test_sample_rejects_have_expected_reasons(tmp_path: Path) -> None:
    con = connect(tmp_path / "wh.duckdb")
    ingest_file(con, SAMPLES["v2_your_activity"])
    reasons = {r[0] for r in con.execute("SELECT DISTINCT reason FROM raw_rejects").fetchall()}
    con.close()
    assert {"invalid_date", "no_video_id"} <= reasons
    assert reasons <= {"missing_date", "invalid_date", "missing_link", "no_video_id"}


def test_cli_ingest_unknown_format_fails_loudly(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"Activities": {"x": 1}, "Profile": {}}))
    result = runner.invoke(app, ["ingest", str(bad), "--db", str(tmp_path / "wh.duckdb")])
    assert result.exit_code == 1
    assert "Unrecognised TikTok export" in result.output
    assert "'Activities'" in result.output


def test_cli_ingest_output_has_no_record_values(tmp_path: Path) -> None:
    export = tmp_path / "export.json"
    write(synthesize(days=7), export)
    result = runner.invoke(app, ["ingest", str(export), "--db", str(tmp_path / "wh.duckdb")])
    assert result.exit_code == 0
    assert "/video/" not in result.output
    assert "2026-11" not in result.output  # no timestamps either


def test_cli_ingest_uses_configured_warehouse(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("paths:\n  warehouse: wh/custom.duckdb\n")
    export = tmp_path / "export.json"
    write(synthesize(days=3), export)
    result = runner.invoke(app, ["ingest", str(export), "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "wh" / "custom.duckdb").exists()
    con = duckdb.connect(str(tmp_path / "wh" / "custom.duckdb"), read_only=True)
    row = con.execute("SELECT count(*) FROM raw_watch").fetchone()
    con.close()
    assert row is not None and row[0] > 0
