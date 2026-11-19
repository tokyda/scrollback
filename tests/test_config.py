from pathlib import Path

import pytest

from scrollback.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_config_missing(tmp_path: Path) -> None:
    settings = load_config(tmp_path / "config.yaml", environ={})
    assert settings.timezone.source_tz == "UTC"
    assert settings.timezone.local_tz == "Asia/Singapore"
    assert settings.metrics.session_gap_min == 30
    assert settings.metrics.view_cap_sec == 180
    assert settings.agent.result_mode == "aggregate_only"
    assert settings.paths.warehouse == tmp_path / "data/warehouse/scrollback.duckdb"
    assert settings.secrets.anthropic_api_key is None


def test_repo_config_yaml_loads() -> None:
    settings = load_config(
        REPO_ROOT / "config.yaml", env_file=REPO_ROOT / "missing.env", environ={}
    )
    assert settings.root == REPO_ROOT
    assert settings.paths.sample_dir == REPO_ROOT / "data/sample"


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.yaml",
        "timezone:\n  local_tz: Europe/Berlin\nmetrics:\n  session_gap_min: 15\n",
    )
    settings = load_config(cfg, environ={})
    assert settings.timezone.local_tz == "Europe/Berlin"
    assert settings.metrics.session_gap_min == 15
    assert settings.metrics.view_cap_sec == 180


def test_env_file_and_process_env_precedence(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "config.yaml", "timezone:\n  local_tz: Europe/Berlin\n")
    _write(tmp_path / ".env", "LOCAL_TZ=America/New_York\nLLM_MODEL=from-dotenv\n")
    settings = load_config(cfg, environ={"LLM_MODEL": "from-process"})
    assert settings.timezone.local_tz == "America/New_York"  # .env beats yaml
    assert settings.llm.model == "from-process"  # process env beats .env


def test_secrets_and_chat_ids(tmp_path: Path) -> None:
    settings = load_config(
        tmp_path / "config.yaml",
        environ={"ANTHROPIC_API_KEY": "sk-test", "TELEGRAM_ALLOWED_CHAT_IDS": "1, 22,333"},
    )
    assert settings.secrets.anthropic_api_key is not None
    assert settings.secrets.anthropic_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(settings)
    assert settings.secrets.telegram_allowed_chat_ids == [1, 22, 333]


def test_invalid_timezone_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown IANA timezone"):
        load_config(tmp_path / "config.yaml", environ={"SOURCE_TZ": "Mars/Olympus"})


def test_unknown_key_rejected(tmp_path: Path) -> None:
    cfg = _write(tmp_path / "config.yaml", "metrics:\n  sesion_gap_min: 10\n")
    with pytest.raises(ConfigError):
        load_config(cfg, environ={})


def test_invalid_result_mode_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "config.yaml", environ={"AGENT_RESULT_MODE": "everything"})


def test_bad_chat_ids_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="comma-separated integers"):
        load_config(tmp_path / "config.yaml", environ={"TELEGRAM_ALLOWED_CHAT_IDS": "abc"})


def test_last_view_default_cannot_exceed_cap(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.yaml", "metrics:\n  view_cap_sec: 60\n  default_last_view_sec: 90\n"
    )
    with pytest.raises(ConfigError, match="default_last_view_sec must be <= view_cap_sec"):
        load_config(cfg, environ={})


def test_late_night_window_must_be_ordered(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "config.yaml",
        "metrics:\n  late_night_start_hour: 5\n  late_night_end_hour: 5\n",
    )
    with pytest.raises(ConfigError, match="late_night_start_hour"):
        load_config(cfg, environ={})


def test_agent_and_llm_defaults(tmp_path: Path) -> None:
    settings = load_config(tmp_path / "config.yaml", environ={})
    assert settings.llm.model == "claude-sonnet-5-5"
    assert settings.llm.effort == "medium"
    assert settings.agent.max_steps == 8
    assert settings.agent.sql_max_retries == 2
    assert settings.agent.max_result_rows == 200
    assert settings.agent.describe_include_descriptions is False


def test_effort_and_steps_from_env(tmp_path: Path) -> None:
    settings = load_config(
        tmp_path / "config.yaml", environ={"LLM_EFFORT": "high", "AGENT_MAX_STEPS": "5"}
    )
    assert settings.llm.effort == "high"
    assert settings.agent.max_steps == 5


def test_temperature_is_no_longer_accepted(tmp_path: Path) -> None:
    # Current models reject sampling parameters; a stale config must fail loudly.
    cfg = _write(tmp_path / "config.yaml", "llm:\n  temperature: 0.0\n")
    with pytest.raises(ConfigError):
        load_config(cfg, environ={})
