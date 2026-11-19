"""Shared fixtures. Everything here uses synthetic data only."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from scrollback.config import Settings, load_config
from scrollback.ingest.load import ingest_file
from scrollback.warehouse.db import connect
from scrollback.warehouse.dbt_runner import DbtResult, run_dbt

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_EXPORT = REPO_ROOT / "data" / "sample" / "user_data_tiktok.json"


def repo_settings(**metric_overrides: int) -> Settings:
    """Repo config.yaml, ignoring the developer's .env and environment."""
    settings = load_config(
        REPO_ROOT / "config.yaml", env_file=REPO_ROOT / "no-such.env", environ={}
    )
    if metric_overrides:
        settings.metrics = settings.metrics.model_copy(update=metric_overrides)
    return settings


@dataclass
class BuiltWarehouse:
    path: Path
    settings: Settings
    result: DbtResult

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path), read_only=True)


@pytest.fixture(scope="session")
def synthetic_warehouse(tmp_path_factory: pytest.TempPathFactory) -> BuiltWarehouse:
    """The committed v2 sample, ingested and built once per test session."""
    root = tmp_path_factory.mktemp("synthetic_wh")
    db_path = root / "wh.duckdb"
    con = connect(db_path)
    ingest_file(con, SAMPLE_EXPORT)
    con.close()
    settings = repo_settings()
    result = run_dbt(settings, db_path, quiet=True, target_path=root / "target")
    return BuiltWarehouse(db_path, settings, result)


@pytest.fixture
def wh(synthetic_warehouse: BuiltWarehouse) -> Iterator[duckdb.DuckDBPyConnection]:
    con = synthetic_warehouse.connect()
    yield con
    con.close()


@pytest.fixture
def agent_settings(synthetic_warehouse: BuiltWarehouse, tmp_path: Path) -> Iterator[Settings]:
    """Repo settings pointed at a private copy of the synthetic warehouse and tmp dirs."""
    import shutil

    db_copy = tmp_path / "agent_wh.duckdb"
    shutil.copy(synthetic_warehouse.path, db_copy)
    settings = repo_settings()
    settings.paths = settings.paths.model_copy(
        update={
            "warehouse": db_copy,
            "traces_dir": tmp_path / "traces",
            "charts_dir": tmp_path / "charts",
        }
    )
    yield settings
