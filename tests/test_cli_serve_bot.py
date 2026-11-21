"""`scrollback serve bot`: start-up checks and wiring (fake API, no network)."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from scrollback.agent import llm as llm_module
from scrollback.agent.llm import ScriptedLLMClient
from scrollback.bot import telegram_api, telegram_bot
from scrollback.cli import app
from scrollback.config import Settings
from tests.telegram_fake import FAKE_TOKEN, FakeTelegram

runner = CliRunner()
REPO = Path(__file__).resolve().parents[1]
E06B = REPO / "eval/experiments/e06b_cannot_answer_haiku.yaml"


def _config(settings: Settings, tmp_path: Path, extra: str = "") -> Path:
    p = settings.paths
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  warehouse: {p.warehouse}\n  traces_dir: {p.traces_dir}\n"
        f"  charts_dir: {p.charts_dir}\n  kb_index: {p.kb_index}\n"
        "kb:\n  embedding_model: hashing\n"
        f"interactive:\n  experiment: {E06B}\n" + extra
    )
    return cfg


def _env(tmp_path: Path, **values: str) -> None:
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


@pytest.fixture(autouse=True)
def _no_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "AGENT_RESULT_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_refuses_without_token_or_allowlist(agent_settings: Settings, tmp_path: Path) -> None:
    _env(tmp_path)
    result = runner.invoke(
        app, ["serve", "bot", "--config", str(_config(agent_settings, tmp_path))]
    )
    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN is not set" in result.output
    assert "TELEGRAM_ALLOWED_CHAT_IDS is empty" in result.output


def test_refuses_full_result_mode(agent_settings: Settings, tmp_path: Path) -> None:
    _env(tmp_path, TELEGRAM_BOT_TOKEN=FAKE_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS="1",
         AGENT_RESULT_MODE="full")  # fmt: skip
    cfg = str(_config(agent_settings, tmp_path))
    # E6b pins result_mode: aggregate_only, which wins over the env; without it, env decides.
    result = runner.invoke(app, ["serve", "bot", "--no-experiment", "--config", cfg])
    assert result.exit_code == 1
    assert "aggregate_only" in result.output and FAKE_TOKEN not in result.output


def test_starts_with_interactive_config(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, TELEGRAM_BOT_TOKEN=FAKE_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS="11,22")
    fake = FakeTelegram()
    real_api = telegram_api.TelegramAPI
    monkeypatch.setattr(
        telegram_api, "TelegramAPI",
        lambda token, **kw: real_api(token, transport=fake.transport, **kw),
    )  # fmt: skip
    monkeypatch.setattr(llm_module, "create_llm_client", lambda s: ScriptedLLMClient([]))
    seen: dict[str, Any] = {}

    def fake_run(self: telegram_bot.ScrollbackBot, **kw: Any) -> None:
        seen["settings"] = self.settings
        seen["allowed"] = self.allowed
        raise KeyboardInterrupt

    monkeypatch.setattr(telegram_bot.ScrollbackBot, "run", fake_run)
    result = runner.invoke(
        app, ["serve", "bot", "--config", str(_config(agent_settings, tmp_path))]
    )
    assert result.exit_code == 0, result.output
    assert "Bot running: e06b_cannot_answer_haiku (claude-haiku-5-5, prompt v3" in result.output
    assert "2 allowed chats" in result.output and "aggregate_only" in result.output
    assert result.output.rstrip().endswith("Bot stopped.")
    assert FAKE_TOKEN not in result.output
    s = seen["settings"]
    assert s.agent.search_knowledge and s.agent.prompt_version == "3"
    assert seen["allowed"] == frozenset({11, 22})
    assert agent_settings.paths.kb_index.exists()  # index built at start, not on question 1
