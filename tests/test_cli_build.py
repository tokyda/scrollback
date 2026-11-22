"""`scrollback build` and the dbt step of `scrollback ingest`."""

from pathlib import Path

import duckdb
from typer.testing import CliRunner

from scrollback.cli import app
from tests.conftest import REPO_ROOT, SAMPLE_EXPORT

runner = CliRunner()


def _config(tmp_path: Path, extra_metrics: str = "") -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"paths:\n  dbt_project_dir: {REPO_ROOT / 'dbt'}\n  warehouse: wh.duckdb\n"
        + (f"metrics:\n{extra_metrics}" if extra_metrics else "")
    )
    return cfg


def test_build_without_data_fails_clearly(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build", "--config", str(_config(tmp_path))])
    assert result.exit_code == 1
    assert "run `scrollback ingest <export>` first" in result.output


def test_ingest_runs_dbt_build(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = runner.invoke(app, ["ingest", str(SAMPLE_EXPORT), "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "dbt build: ok" in result.output
    con = duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
    row = con.execute("SELECT count(*) FROM agg_daily").fetchone()
    con.close()
    assert row == (180,)


def test_build_after_no_build_ingest(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    ingest = runner.invoke(app, ["ingest", str(SAMPLE_EXPORT), "--config", str(cfg), "--no-build"])
    assert ingest.exit_code == 0
    assert "skipped (--no-build)" in ingest.output
    build = runner.invoke(app, ["build", "--config", str(cfg), "--quiet"])
    assert build.exit_code == 0, build.output
    assert "dbt build: ok" in build.output


def test_failing_data_test_fails_build_and_ingest(tmp_path: Path) -> None:
    # A daily-views bound of 1 makes assert_daily_views_below_sanity_bound fail.
    cfg = _config(tmp_path, "  max_daily_views: 1\n")
    ingest = runner.invoke(app, ["ingest", str(SAMPLE_EXPORT), "--config", str(cfg)])
    assert ingest.exit_code == 1
    assert "dbt build: FAILED" in ingest.output
    assert "assert_daily_views_below_sanity_bound" in ingest.output
    assert "Raw data was loaded" in ingest.output
    build = runner.invoke(app, ["build", "--config", str(cfg), "-q"])
    assert build.exit_code == 1


def test_docs_builds_one_static_page(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    nodata = runner.invoke(app, ["docs", "--config", str(cfg)])
    assert nodata.exit_code == 1
    assert "run `scrollback ingest <export>` first" in nodata.output
    assert runner.invoke(app, ["ingest", str(SAMPLE_EXPORT), "--config", str(cfg)]).exit_code == 0
    out = tmp_path / "docs"
    result = runner.invoke(app, ["docs", "--config", str(cfg), "--out", str(out), "-q"])
    assert result.exit_code == 0, result.output
    assert "dbt docs: ok" in result.output
    page = out / "static_index.html"
    assert page.exists()
    assert "fct_views" in page.read_text(encoding="utf-8")
