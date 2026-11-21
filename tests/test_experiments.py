"""Interactive config (ask and the bot) vs eval config (DESIGN §6.7)."""

from pathlib import Path

import pytest

from scrollback.config import load_config
from scrollback.evals.questions import load_questions
from scrollback.evals.run import experiment_settings
from scrollback.experiments import apply_overrides, interactive_settings, load_experiment

REPO_ROOT = Path(__file__).resolve().parents[1]
E06B = REPO_ROOT / "eval/experiments/e06b_cannot_answer_haiku.yaml"


def _repo():
    return load_config(REPO_ROOT / "config.yaml", env_file=REPO_ROOT / "no.env", environ={})


def test_repo_interactive_default_is_e06b() -> None:
    settings = _repo()
    assert settings.interactive.experiment == E06B
    interactive, exp = interactive_settings(settings)
    assert exp is not None and exp.name == "e06b_cannot_answer_haiku"
    assert interactive.llm.model == "claude-haiku-5-5"
    assert interactive.agent.prompt_version == "3"
    assert interactive.agent.search_knowledge is True
    assert interactive.agent.kb_sources == ["metrics", "docs", "tables", "cannot_answer"]
    assert interactive.agent.result_mode == "aggregate_only"
    # Real, calibrated params stay: the experiment's eval pin is not applied.
    assert interactive.metrics == settings.metrics
    assert interactive.timezone == settings.timezone


def test_interactive_opt_out_and_explicit_path() -> None:
    settings = _repo()
    same, exp = interactive_settings(settings, use_experiment=False)
    assert exp is None and same is settings
    e00 = REPO_ROOT / "eval/experiments/e00_baseline.yaml"
    other, exp = interactive_settings(settings, e00)
    assert other.llm.model == "claude-sonnet-5-5"
    assert exp is not None and exp.name == "e00_baseline"


def test_relative_experiment_path_resolves_against_config_dir(tmp_path: Path) -> None:
    (tmp_path / "x").mkdir()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("interactive:\n  experiment: x/e.yaml\n")
    assert load_config(cfg, environ={}).interactive.experiment == tmp_path / "x/e.yaml"


def test_evals_ignore_interactive_section() -> None:
    """Owner check (Phase 6): the interactive default must not change any eval run's settings."""
    qset = load_questions()
    with_default = _repo()
    without = with_default.model_copy(deep=True)
    without.interactive.experiment = None
    for path in sorted((REPO_ROOT / "eval/experiments").glob("*.yaml")):
        exp = load_experiment(path)
        a = experiment_settings(with_default, exp, qset)
        b = experiment_settings(without, exp, qset)
        for section in ("llm", "agent", "metrics", "timezone", "kb", "paths"):
            assert getattr(a, section) == getattr(b, section), (path.name, section)
        assert a.metrics == qset.params.metrics  # evals still use the pinned params


def test_apply_overrides_rejects_other_sections() -> None:
    from scrollback.experiments import Experiment

    exp = Experiment(name="bad", description="x", overrides={"metrics": {"view_cap_sec": 1}})
    with pytest.raises(ValueError, match="may only override"):
        apply_overrides(_repo(), exp)
