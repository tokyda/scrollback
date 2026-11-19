"""Run dbt against the warehouse with parameters from ``config.yaml`` (DESIGN §4.3).

``config.yaml`` is the single source of truth for every dbt var. ``dbt_project.yml`` has no
defaults, so a dbt invocation that bypasses this module fails loudly instead of silently
using stale values.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scrollback.config import Settings

DUCKDB_PATH_ENV = "SCROLLBACK_DUCKDB_PATH"

# config.yaml `metrics:` keys passed to dbt as vars, plus the two timezones.
METRIC_VARS: tuple[str, ...] = (
    "session_gap_min",
    "view_cap_sec",
    "default_last_view_sec",
    "binge_threshold_min",
    "late_night_start_hour",
    "late_night_end_hour",
    "max_daily_views",
    "freshness_warn_days",
)


class DbtError(RuntimeError):
    """dbt could not be invoked at all (as opposed to a model or test failing)."""


@dataclass
class DbtResult:
    command: str
    success: bool
    statuses: Counter[str] = field(default_factory=Counter)  # e.g. {"pass": 40, "warn": 1}
    failures: list[str] = field(default_factory=list)  # node names only, never row data

    def summary(self) -> str:
        parts = [f"{n} {status}" for status, n in sorted(self.statuses.items())]
        verdict = "ok" if self.success else "FAILED"
        return f"dbt {self.command}: {verdict} ({', '.join(parts) or 'nothing run'})"


def dbt_vars(settings: Settings) -> dict[str, Any]:
    values: dict[str, Any] = {name: getattr(settings.metrics, name) for name in METRIC_VARS}
    values["source_tz"] = settings.timezone.source_tz
    values["local_tz"] = settings.timezone.local_tz
    return values


def _dbt_executable() -> str:
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("dbt")
    if found is None:
        raise DbtError("dbt executable not found; run `uv sync`")
    return found


def run_dbt(
    settings: Settings,
    db_path: Path | None = None,
    command: str = "build",
    extra_args: Sequence[str] = (),
    quiet: bool = False,
    target_path: Path | None = None,
) -> DbtResult:
    """Run ``dbt <command>`` against ``db_path`` (default: the configured warehouse).

    dbt runs in a subprocess: dbt-duckdb keeps its DuckDB connection open for the life of the
    process, which would block later read-only connections and leak state between builds.
    dbt's own output (model and test names, counts; never row data) goes to our stdout unless
    ``quiet`` is set.
    """
    project_dir = settings.paths.dbt_project_dir.resolve()
    warehouse = (db_path or settings.paths.warehouse).resolve()
    target = (target_path or project_dir / "target").resolve()
    args = [
        _dbt_executable(),
        *(["--quiet"] if quiet else []),
        command,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
        "--target-path",
        str(target),
        "--vars",
        json.dumps(dbt_vars(settings)),
        *extra_args,
    ]
    env = {**os.environ, DUCKDB_PATH_ENV: str(warehouse), "DO_NOT_TRACK": "1"}
    run_results = target / "run_results.json"
    run_results.unlink(missing_ok=True)
    completed = subprocess.run(args, env=env, check=False)

    result = DbtResult(command=command, success=completed.returncode == 0)
    if not run_results.exists():
        if result.success:
            return result
        raise DbtError(
            f"dbt {command} failed before running any node (exit {completed.returncode})"
        )
    for node in json.loads(run_results.read_text(encoding="utf-8")).get("results", []):
        status = str(node.get("status"))
        result.statuses[status] += 1
        if status in {"error", "fail"}:
            result.failures.append(str(node.get("unique_id")))
    return result
