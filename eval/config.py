"""Experiment config loader (Jira AII-25).

An ablation is a config file, not a code branch. C0-SC (majority vote over
C0's three runs) needs no config: eval/analysis computes it from C0 results.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

CONFIG_DIR = Path(__file__).with_name("configs")


class Caps(BaseModel):
    tool_calls: int
    tokens: int
    wall_seconds: int


class ExperimentConfig(BaseModel):
    id: str
    description: str
    architecture: Literal["single", "multi", "multi_merged"]
    critic: bool
    structural_checks: bool
    semantic_verifier: bool
    runbook_search: bool
    gate: bool
    runs_per_case: int
    model: str
    temperature: float
    caps: Caps
    seeds: list[int]

    @model_validator(mode="after")
    def _consistent(self):
        if self.semantic_verifier and not self.structural_checks:
            raise ValueError("semantic verifier runs only on structurally valid evidence")
        if self.architecture == "single" and self.critic:
            raise ValueError("the single-agent baseline has no separate critic")
        if self.runs_per_case > len(self.seeds):
            raise ValueError("need one seed per run")
        return self


def load(path: str | Path) -> ExperimentConfig:
    raw = os.path.expandvars(Path(path).read_text())
    return ExperimentConfig(**yaml.safe_load(raw))


def load_all(directory: Path = CONFIG_DIR) -> dict[str, ExperimentConfig]:
    return {c.id: c for c in (load(p) for p in sorted(directory.glob("*.yaml")))}


def check_parity(configs: dict[str, ExperimentConfig]) -> None:
    """Fairness rule: every configuration shares model, temperature and caps."""
    ref = configs["C2"]
    for c in configs.values():
        for field in ("model", "temperature", "caps"):
            if getattr(c, field) != getattr(ref, field):
                raise ValueError(f"{c.id} differs from C2 on {field}; budget parity broken")
