"""Experiment files (DESIGN §7.5): named llm/agent overrides on top of config.yaml.

Evals apply an experiment plus the question set's pinned params (``evals.run``). Interactive
use (``scrollback ask`` and the Telegram bot) applies only the llm/agent overrides of
``interactive.experiment``, so it runs the evaluated config on the calibrated real params.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from scrollback.config import Settings

OVERRIDABLE = frozenset({"llm", "agent"})


class Experiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)  # llm / agent sections


def load_experiment(path: Path) -> Experiment:
    return Experiment.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def apply_overrides(base: Settings, exp: Experiment) -> Settings:
    """A copy of ``base`` with the experiment's llm/agent overrides applied (nothing else)."""
    bad = set(exp.overrides) - OVERRIDABLE
    if bad:
        raise ValueError(f"experiment may only override {sorted(OVERRIDABLE)}, got {sorted(bad)}")
    settings = base.model_copy(deep=True)
    if "llm" in exp.overrides:
        settings.llm = type(settings.llm).model_validate(
            {**settings.llm.model_dump(), **exp.overrides["llm"]}
        )
    if "agent" in exp.overrides:
        settings.agent = type(settings.agent).model_validate(
            {**settings.agent.model_dump(), **exp.overrides["agent"]}
        )
    return settings


def interactive_settings(
    base: Settings, experiment: Path | None = None, *, use_experiment: bool = True
) -> tuple[Settings, Experiment | None]:
    """Settings for ``ask`` and the bot: ``experiment`` if given, else
    ``interactive.experiment``; ``use_experiment=False`` keeps config.yaml's llm/agent."""
    path = experiment if experiment is not None else base.interactive.experiment
    if not use_experiment or path is None:
        return base, None
    exp = load_experiment(path)
    return apply_overrides(base, exp), exp
