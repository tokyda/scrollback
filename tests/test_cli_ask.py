"""`scrollback ask` with a scripted LLM (no network)."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from scrollback.agent import llm as llm_module
from scrollback.agent.llm import LLMError, ScriptedLLMClient, mock_turn
from scrollback.cli import app
from scrollback.config import Settings

runner = CliRunner()
SQL = "SELECT sum(est_watch_sec) / 60.0 AS minutes FROM fct_views WHERE local_date = '2026-11-19'"


def _config(settings: Settings, tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    p = settings.paths
    cfg.write_text(
        "paths:\n"
        f"  warehouse: {p.warehouse}\n  traces_dir: {p.traces_dir}\n"
        f"  charts_dir: {p.charts_dir}\n  dbt_project_dir: {p.dbt_project_dir}\n"
    )
    return cfg


def _script(monkeypatch: pytest.MonkeyPatch, turns: list[Any]) -> ScriptedLLMClient:
    client = ScriptedLLMClient(turns)
    monkeypatch.setattr(llm_module, "create_llm_client", lambda settings: client)
    return client


ANSWER = (
    "final_answer",
    {
        "text": "You watched about 42 minutes yesterday.",
        "result_ids": ["r1"],
        "metrics_used": ["est_watch_minutes"],
        "assumptions": ["yesterday = 2026-11-19 (Asia/Singapore)"],
        "chart_id": "",
    },
)


def test_ask_prints_answer_sql_and_trace(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _script(monkeypatch, [mock_turn(("run_sql", {"sql": SQL})), mock_turn(ANSWER)])
    result = runner.invoke(
        app,
        ["ask", "how many minutes did I watch yesterday", "--show-sql", "--today", "2026-11-20",
         "--config", str(_config(agent_settings, tmp_path))],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.startswith("You watched about 42 minutes yesterday.")
    assert "Metrics: est_watch_minutes" in out
    assert "  - yesterday = 2026-11-19 (Asia/Singapore)" in out
    assert "SQL (1 query):" in out and "LIMIT 200" in out
    assert "· answered · 2 steps · 2 tool calls ·" in out
    assert "Today is 2026-11-20" in client.requests[0]["messages"][0]["content"]
    assert list(agent_settings.paths.traces_dir.glob("*.jsonl"))


def test_ask_hides_sql_by_default(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, [mock_turn(("run_sql", {"sql": SQL})), mock_turn(ANSWER)])
    result = runner.invoke(
        app,
        ["ask", "q", "--today", "2026-11-20", "--config", str(_config(agent_settings, tmp_path))],
    )
    assert result.exit_code == 0
    assert "SQL: 1 query (use --show-sql to see)" in result.output
    assert "SELECT" not in result.output


def test_ask_chart_flag(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daily = "SELECT local_date, est_watch_min FROM agg_daily ORDER BY 1 DESC LIMIT 7"
    chart_answer = ("final_answer", {**ANSWER[1], "chart_id": "c1"})
    client = _script(
        monkeypatch,
        [
            mock_turn(("run_sql", {"sql": daily})),
            mock_turn(("make_chart", {"result_id": "r1", "kind": "bar", "x": "local_date",
                                      "y": "est_watch_min", "title": "Last 7 days"})),
            mock_turn(chart_answer),
        ],
    )  # fmt: skip
    result = runner.invoke(
        app,
        ["ask", "q", "--chart", "--today", "2026-11-20",
         "--config", str(_config(agent_settings, tmp_path))],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Chart: " in result.output and result.output.count(".png") == 1
    assert "Include a chart" in client.requests[0]["messages"][0]["content"]


def test_ask_step_limit_exits_nonzero(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script(monkeypatch, [mock_turn(("describe_table", {"table": "agg_daily"}))] * 8)
    result = runner.invoke(
        app,
        ["ask", "q", "--today", "2026-11-20", "--config", str(_config(agent_settings, tmp_path))],
    )
    assert result.exit_code == 1
    assert "reached its step limit" in result.output
    assert "· step_limit · 8 steps" in result.output


def test_ask_missing_key(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(settings: Settings) -> Any:
        raise LLMError("config", "ANTHROPIC_API_KEY is not set; add it to .env")

    monkeypatch.setattr(llm_module, "create_llm_client", no_key)
    result = runner.invoke(app, ["ask", "q", "--config", str(_config(agent_settings, tmp_path))])
    assert result.exit_code == 1 and "ANTHROPIC_API_KEY is not set" in result.output


def test_ask_without_data(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ask", "q", "--db", str(tmp_path / "none.duckdb")])
    assert result.exit_code == 1 and "run `scrollback ingest" in result.output


def test_ask_bad_today(agent_settings: Settings, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["ask", "q", "--today", "yesterday", "--config", str(_config(agent_settings, tmp_path))],
    )
    assert result.exit_code == 2 and "--today must be YYYY-MM-DD" in result.output


def test_ask_uses_interactive_experiment(
    agent_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ask runs interactive.experiment (as the bot does): E6b's prompt and tools."""
    exp = Path(__file__).resolve().parents[1] / "eval/experiments/e06b_cannot_answer_haiku.yaml"
    cfg = _config(agent_settings, tmp_path)
    cfg.write_text(cfg.read_text() + f"interactive:\n  experiment: {exp}\n")
    client = _script(monkeypatch, [mock_turn(("run_sql", {"sql": SQL})), mock_turn(ANSWER)])
    result = runner.invoke(app, ["ask", "q", "--today", "2026-11-20", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "· e06b_cannot_answer_haiku" in result.output
    tools = [t["name"] for t in client.requests[0]["tools"]]
    assert tools[0] == "search_knowledge"

    client = _script(monkeypatch, [mock_turn(("run_sql", {"sql": SQL})), mock_turn(ANSWER)])
    result = runner.invoke(
        app, ["ask", "q", "--today", "2026-11-20", "--no-experiment", "--config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    assert "· config.yaml" in result.output
    assert "search_knowledge" not in [t["name"] for t in client.requests[0]["tools"]]


def test_ask_bad_experiment_path(agent_settings: Settings, tmp_path: Path) -> None:
    cfg = _config(agent_settings, tmp_path)
    result = runner.invoke(
        app, ["ask", "q", "--experiment", str(tmp_path / "nope.yaml"), "--config", str(cfg)]
    )
    assert result.exit_code == 1
    assert "experiment could not be loaded" in result.output
