"""Scenario and ground-truth format (Jira AII-5). Frozen at Gate 1."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from agent.state import CauseType, Service

Split = Literal["train", "dev", "test"]
Difficulty = Literal["easy", "medium", "hard"]


class Fix(BaseModel):
    action: Literal["restart", "rollback", "scale"]
    target: Service


class GroundTruth(BaseModel):
    """Written by the fault injector. Agents never see it."""
    root_cause_service: Service
    root_cause_type: CauseType
    expected_evidence: list[str]      # signatures, e.g. "deploy:d-7f3", "log:timeout after"
    correct_fix: Fix
    relevant_runbooks: list[str] = []


class Scenario(BaseModel):
    id: str                           # e.g. "sc-017"
    family: str                       # fault|target|noise; never crosses splits
    split: Split
    fault: str
    target: Service
    noise: str
    difficulty: Difficulty
    intensity: Literal["low", "medium", "high"]
    onset_offset_s: int               # seconds of baseline traffic before the fault fires
    alert_name: str
    alert_service: Service            # where the alert fires (may differ from the cause)
    held_out_fault: bool
    seed: int
    ground_truth: GroundTruth
