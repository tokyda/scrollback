"""Eval runner end to end with mock agents (no network).

An "oracle" agent answers each question by running its gold SQL and stating the result, so the
whole pipeline (anchor warehouses -> agent loop -> scoring -> labels -> report) must score
100%. A "wrong" agent must fail every answerable question with labelled failures.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from scrollback.agent.llm import LLMResponse, mock_turn
from scrollback.cli import app
from scrollback.config import Settings
from scrollback.evals.questions import Question, load_questions
from scrollback.evals.report import compare
from scrollback.evals.run import (
    Experiment,
    estimate_cost,
    experiment_settings,
    load_experiment,
    load_records,
    run_eval,
)
from tests.conftest import REPO_ROOT, repo_settings

QSET = load_questions()
E0 = load_experiment(REPO_ROOT / "eval" / "experiments" / "e00_baseline.yaml")


def _question_for(messages: list[dict[str, Any]]) -> Question:
    first = messages[0]["content"]
    text = first.split("Question: ", 1)[1].split("\n")[0].strip()
    return next(q for q in QSET.questions if q.question == text)


def _fmt(value: Any, unit: str) -> str:
    if unit == "hour_of_day":
        return f"hour {value}"
    if unit == "ratio":
        return f"{float(value):.4f}"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _final(text: str, result_ids: list[str]) -> tuple[str, dict[str, Any]]:
    return (
        "final_answer",
        {"text": text, "result_ids": result_ids, "metrics_used": [],
         "assumptions": ["reading as stated"], "chart_id": ""},
    )  # fmt: skip


class OracleLLM:
    model = "mock-oracle"

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        q = _question_for(messages)
        if q.answer_type == "refusal":
            return mock_turn(_final("I can't answer that: the data has no such information.", []))
        if q.answer_type == "no_data":
            return mock_turn(_final("There's no data for today yet in your export.", []))
        if len(messages) == 1:
            assert q.gold_sql is not None
            return mock_turn(("run_sql", {"sql": q.gold_sql}))
        result = json.loads(messages[-1]["content"][0]["content"])
        rows = result["rows"]
        values = [_fmt(v, q.units[i % len(q.units)]) for row in rows for i, v in enumerate(row)]
        text = "Answer: " + ", ".join(values) + "."
        if q.requires_caveat:
            text += " This is an estimate."
        return mock_turn(_final(text, [result["result_id"]]))


class WrongLLM:
    model = "mock-wrong"

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        if len(messages) == 1:
            return mock_turn(
                ("run_sql", {"sql": "SELECT count(*) * 0 + 123456 AS n FROM fct_views"})
            )
        result = json.loads(messages[-1]["content"][0]["content"])
        return mock_turn(_final("You watched 123456.", [result["result_id"]]))


@pytest.fixture(scope="module")
def oracle_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("evalrun")
    return run_eval(
        E0, QSET, base=repo_settings(), llm_factory=lambda s: OracleLLM(), split="all",
        repeat=1, workers=1, runs_dir=root / "runs", labels_dir=root / "labels",
    )  # fmt: skip


def test_oracle_scores_100_percent(oracle_run: Path) -> None:
    records = load_records(oracle_run)
    assert len(records) == len(QSET.questions)
    failed = [
        (r["question"], r.get("label_reason"), r["answer"]) for r in records if not r["passed"]
    ]
    assert failed == []
    summary = json.loads((oracle_run / "summary.json").read_text())
    assert summary["accuracy"]["overall"]["mean"] == 1.0
    assert summary["accuracy"]["test"]["mean"] == 1.0


def test_run_artifacts(oracle_run: Path) -> None:
    for name in ("config.json", "results.jsonl", "traces.jsonl", "summary.json", "report.md"):
        assert (oracle_run / name).exists(), name
    config = json.loads((oracle_run / "config.json").read_text())
    assert config["experiment"]["name"] == "e00_baseline"
    assert config["config"]["metrics"]["session_gap_min"] == 30
    assert "anthropic_api_key" not in json.dumps(config)
    report = (oracle_run / "report.md").read_text()
    assert "Test (held out; overall only)" in report
    assert "### Dev, by category" in report
    # Per-category rows are dev-only: no test-split category breakdown.
    assert "Test, by category" not in report
    traces = (oracle_run / "traces.jsonl").read_text().splitlines()
    assert len(traces) == len(QSET.questions)


def test_wrong_agent_fails_with_labels(tmp_path: Path) -> None:
    only = ["q001", "q013", "q042", "q063", "q071", "q072"]
    run_dir = run_eval(
        E0, QSET, base=repo_settings(), llm_factory=lambda s: WrongLLM(), split="all",
        repeat=2, workers=1, only=only, runs_dir=tmp_path / "runs", labels_dir=tmp_path / "labels",
    )  # fmt: skip
    records = load_records(run_dir)
    assert len(records) == 12 and not any(r["passed"] for r in records)
    by_q = {r["question"]: r for r in records if r["repeat"] == 1}
    assert by_q["q063"]["label_suggested"] == "failed_refusal"  # stated a number
    assert by_q["q072"]["label_suggested"] == "failed_refusal"
    assert by_q["q013"]["label_suggested"] in ("wrong_time_window", "aggregation_error")
    labels = yaml.safe_load((tmp_path / "labels" / f"{run_dir.name}.yaml").read_text())
    assert len(labels["failures"]) == 12
    assert all(f["label"] is None and f["suggested"] for f in labels["failures"])
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["accuracy"]["overall"] == {"mean": 0.0, "min": 0.0, "max": 0.0}


def test_compare_runs(oracle_run: Path, tmp_path: Path) -> None:
    run_dir = run_eval(
        E0, QSET, base=repo_settings(), llm_factory=lambda s: WrongLLM(), split="all",
        repeat=1, workers=1, only=["q001", "q002"], runs_dir=tmp_path / "r",
        labels_dir=tmp_path / "l",
    )  # fmt: skip
    a = json.loads((oracle_run / "summary.json").read_text())
    b = json.loads((run_dir / "summary.json").read_text())
    text = compare(a, b)
    assert "Regressed (2): q001 1/1->0/1, q002 1/1->0/1" in text


def test_experiment_settings() -> None:
    settings = experiment_settings(repo_settings(), E0, QSET)
    assert settings.llm.model == "claude-sonnet-5-5" and settings.llm.effort == "medium"
    assert (
        settings.agent.prompt_version == 1 and settings.agent.describe_include_descriptions is False
    )
    assert settings.timezone.local_tz == "Asia/Singapore"
    bad = Experiment(name="x", description="x", overrides={"metrics": {"view_cap_sec": 1}})
    with pytest.raises(ValueError, match="may only override"):
        experiment_settings(repo_settings(), bad, QSET)


def test_cost_estimate(tmp_path: Path) -> None:
    traces = tmp_path / "t.jsonl"
    traces.write_text(
        "\n".join(json.dumps({"model": "claude-sonnet-5-5", "cost_usd": c}) for c in (0.01, 0.02))
        + "\n" + json.dumps({"model": "other", "cost_usd": 5}) + "\n"
    )  # fmt: skip
    est = estimate_cost(100, "claude-sonnet-5-5", [traces])
    assert est.per_run_usd == pytest.approx(0.015 * 1.3)
    assert est.total_usd == pytest.approx(0.015 * 1.3 * 100)
    assert "2 past traces" in est.basis
    fallback = estimate_cost(10, "claude-sonnet-5-5", [])
    assert fallback.total_usd > 0 and "assumption" in fallback.basis


def test_cli_run_without_yes_only_estimates(
    monkeypatch: pytest.MonkeyPatch, agent_settings: Settings
) -> None:
    from scrollback.agent import llm as llm_module

    def boom(settings: Settings) -> Any:
        raise AssertionError("must not create an LLM client without --yes")

    monkeypatch.setattr(llm_module, "create_llm_client", boom)
    result = CliRunner().invoke(
        app, ["eval", "run", "--config", str(REPO_ROOT / "eval/experiments/e00_baseline.yaml"),
              "--split", "dev", "--repeat", "3"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    n_dev = len(QSET.by_split("dev"))
    expected = f"{n_dev} questions (dev) x 3 repeats = {3 * n_dev} agent runs on claude-sonnet-5-5"
    assert expected in result.output
    assert "Estimated cost: $" in result.output
    assert "Not running. Re-run with --yes" in result.output


def test_rescore_is_stable_and_keeps_labels(tmp_path: Path) -> None:
    from scrollback.evals.run import rescore_run

    labels_dir = tmp_path / "labels"
    run_dir = run_eval(
        E0, QSET, base=repo_settings(), llm_factory=lambda s: WrongLLM(), split="all",
        repeat=1, workers=1, only=["q001", "q063"], runs_dir=tmp_path / "runs",
        labels_dir=labels_dir,
    )  # fmt: skip
    label_path = labels_dir / f"{run_dir.name}.yaml"
    doc = yaml.safe_load(label_path.read_text())
    doc["failures"][0]["label"] = "aggregation_error"
    doc["confirmed"] = {"by": "owner"}
    label_path.write_text(yaml.safe_dump(doc))

    for _ in range(2):  # re-scoring twice changes nothing and loses nothing
        result = rescore_run(run_dir, QSET, base=repo_settings(), labels_dir=labels_dir)
        assert result["changed"] == []
    doc = yaml.safe_load(label_path.read_text())
    assert doc["confirmed"] == {"by": "owner"}
    assert doc["failures"][0]["label"] == "aggregation_error"
    assert (run_dir / "results.original.jsonl").exists()
    assert len(load_records(run_dir)) == 2
