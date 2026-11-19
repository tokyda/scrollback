"""Configuration loading.

Precedence, highest first: process environment > ``.env`` file > ``config.yaml`` > defaults.
Relative paths are resolved against the directory that holds ``config.yaml`` (the repo root).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

DEFAULT_CONFIG_FILE = "config.yaml"
CONFIG_PATH_ENV = "SCROLLBACK_CONFIG"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimezoneConfig(_Strict):
    source_tz: str = "UTC"
    local_tz: str = "Asia/Singapore"

    @field_validator("source_tz", "local_tz")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value


class PathsConfig(_Strict):
    raw_dir: Path = Path("data/raw")
    sample_dir: Path = Path("data/sample")
    warehouse: Path = Path("data/warehouse/scrollback.duckdb")
    dbt_project_dir: Path = Path("dbt")
    traces_dir: Path = Path("traces")
    eval_runs_dir: Path = Path("eval/runs")

    def resolved(self, root: Path) -> PathsConfig:
        values = {
            name: (value if value.is_absolute() else (root / value))
            for name, value in self.model_dump().items()
        }
        return PathsConfig(**values)


class MetricsConfig(_Strict):
    session_gap_min: int = Field(30, gt=0)
    view_cap_sec: int = Field(180, gt=0)
    default_last_view_sec: int = Field(20, gt=0)
    binge_threshold_min: int = Field(60, gt=0)
    late_night_start_hour: int = Field(0, ge=0, le=23)
    late_night_end_hour: int = Field(5, ge=0, le=24)
    max_daily_views: int = Field(3000, gt=0)
    freshness_warn_days: int = Field(14, gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> MetricsConfig:
        if self.default_last_view_sec > self.view_cap_sec:
            raise ValueError("default_last_view_sec must be <= view_cap_sec")
        if self.late_night_start_hour >= self.late_night_end_hour:
            raise ValueError("late_night_start_hour must be < late_night_end_hour")
        return self


class LLMConfig(_Strict):
    provider: str = "anthropic"
    model: str = "claude-sonnet-5-5"
    temperature: float = Field(0.0, ge=0.0, le=1.0)
    max_steps: int = Field(8, gt=0)
    retrieval_k: int = Field(6, gt=0)


class AgentConfig(_Strict):
    result_mode: Literal["aggregate_only", "full"] = "aggregate_only"


class Secrets(_Strict):
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    api_bearer_token: SecretStr | None = None


class Settings(_Strict):
    timezone: TimezoneConfig = Field(default_factory=TimezoneConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    secrets: Secrets = Field(default_factory=Secrets)
    root: Path = Field(default_factory=Path.cwd)


# env var -> (section, key). Secrets are handled separately.
_ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "SOURCE_TZ": ("timezone", "source_tz"),
    "LOCAL_TZ": ("timezone", "local_tz"),
    "WAREHOUSE_PATH": ("paths", "warehouse"),
    "LLM_PROVIDER": ("llm", "provider"),
    "LLM_MODEL": ("llm", "model"),
    "LLM_TEMPERATURE": ("llm", "temperature"),
    "AGENT_MAX_STEPS": ("llm", "max_steps"),
    "AGENT_RESULT_MODE": ("agent", "result_mode"),
}

_SECRET_ENV: dict[str, str] = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_ALLOWED_CHAT_IDS": "telegram_allowed_chat_ids",
    "API_BEARER_TOKEN": "api_bearer_token",
}


def _parse_chat_ids(raw: str) -> list[int]:
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ConfigError("TELEGRAM_ALLOWED_CHAT_IDS must be comma-separated integers") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a mapping at the top level")
    return data


def load_config(
    config_path: Path | None = None,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load settings from defaults, ``config.yaml``, ``.env`` and the environment."""
    process_env: Mapping[str, str] = os.environ if environ is None else environ

    if config_path is None:
        config_path = Path(process_env.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_FILE))
    config_path = config_path.resolve()
    root = config_path.parent

    data = _read_yaml(config_path)

    dotenv_path = env_file if env_file is not None else root / ".env"
    file_env = {k: v for k, v in dotenv_values(dotenv_path).items() if v is not None}
    merged_env: dict[str, str] = {**file_env, **process_env}

    for env_name, (section, key) in _ENV_OVERRIDES.items():
        value = merged_env.get(env_name)
        if value not in (None, ""):
            data.setdefault(section, {})[key] = value

    secrets: dict[str, Any] = {}
    for env_name, key in _SECRET_ENV.items():
        value = merged_env.get(env_name)
        if not value:
            continue
        secrets[key] = _parse_chat_ids(value) if key == "telegram_allowed_chat_ids" else value
    data["secrets"] = secrets
    data["root"] = root

    try:
        settings = Settings.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
    settings.paths = settings.paths.resolved(root)
    return settings
