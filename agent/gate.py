"""Confidence signals and the gate.

Signals per investigation:
  verbal            the critic's (or single agent's) stated confidence, 0..1
  self_consistency  share of 3 decision samples that pick the same root cause
  evidence_margin   usable support minus verified contradiction for the top hypothesis,
                    minus the same for the runner-up
  combined          logistic regression over the three, fitted on dev only (eval/calibrate.py)

The gate is code, not an LLM: pass when the chosen signal reaches the threshold
in agent/policy.yaml, otherwise needs_review (no action is proposed).
"""
from __future__ import annotations

import math
from pathlib import Path

import yaml

from agent.state import ConfidenceSignals

POLICY = Path(__file__).with_name("policy.yaml")
FEATURES = ["verbal", "self_consistency", "evidence_margin"]


def load_policy(path: Path = POLICY) -> dict:
    return yaml.safe_load(path.read_text())


def features(s: ConfidenceSignals) -> list[float]:
    """Feature vector for the combined model; missing signals get neutral values."""
    verbal = 0.5 if s.verbal is None else float(s.verbal)
    sc = verbal if s.self_consistency is None else float(s.self_consistency)
    margin = 0.0 if s.evidence_margin is None else math.tanh(float(s.evidence_margin) / 2)
    return [verbal, sc, margin]


def combined(s: ConfidenceSignals, calibration: dict | None) -> float | None:
    if not calibration:
        return None
    z = calibration["intercept"] + sum(c * x for c, x in zip(calibration["coef"], features(s)))
    return 1 / (1 + math.exp(-z))


def score(s: ConfidenceSignals, name: str) -> float | None:
    if name == "evidence_margin":
        return None if s.evidence_margin is None else math.tanh(s.evidence_margin / 2)
    return getattr(s, name)


def decide(s: ConfidenceSignals, policy: dict, enabled: bool = True) -> str:
    if not enabled:
        return "pass"
    g = policy["gate"]
    value = score(s, g["signal"])
    if value is None:
        value = s.verbal if s.verbal is not None else 0.0
    return "pass" if value >= g["threshold"] else "needs_review"
