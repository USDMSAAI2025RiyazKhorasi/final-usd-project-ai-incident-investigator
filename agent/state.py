"""Shared contract for every investigation (Jira AII-7). Frozen at Gate 1.

Every agent, tool, validator, the gate, the action layer and the evaluation
harness read and write these types. Change only with agreement from A, B and C.
"""
from __future__ import annotations

import operator
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------- fixed vocab
class Service(str, Enum):
    gateway = "gateway"
    orders = "orders"
    payments = "payments"
    inventory = "inventory"
    notifications = "notifications"
    postgres = "postgres"
    redis = "redis"


class CauseType(str, Enum):
    """The fixed list a hypothesis must choose from. Scored by exact match."""
    resource_exhaustion = "resource_exhaustion"      # memory, CPU, disk, pools
    database_performance = "database_performance"    # slow queries, missing index
    config_change = "config_change"                  # bad config in a deploy
    code_change = "code_change"                      # buggy commit in a deploy
    dependency_unavailable = "dependency_unavailable"  # Redis, Postgres, bank down
    credential_failure = "credential_failure"        # expired or wrong credential


AgentName = Literal["supervisor", "logs", "metrics", "changes", "investigator", "critic", "single"]


# ---------------------------------------------------------------- hypotheses
class Hypothesis(BaseModel):
    id: str                       # "h1".."h4"
    service: Service
    cause_type: CauseType
    description: str
    created_by: AgentName = "supervisor"


# ---------------------------------------------------------------- evidence
class Provenance(BaseModel):
    """Where an evidence item came from, precise enough to re-run the call."""
    tool: str
    args: dict
    called_at: datetime
    data_start: datetime
    data_end: datetime
    raw_ref: str                  # path of the stored raw tool result
    raw_hash: str                 # sha256 of the raw result
    mode: Literal["live", "snapshot"]
    ok: bool = True               # False if the tool call failed or timed out


class Evidence(BaseModel):
    id: str
    agent: AgentName
    hypothesis_id: str
    claimed: Literal["supports", "contradicts"]
    summary: str                  # any number or quoted pattern must be in the raw result
    provenance: Provenance
    call_id: str | None = None    # the tool call this evidence was built from

    # set by agent/validation/structural.py
    structural: Literal["pending", "valid", "invalid"] = "pending"
    structural_reason: str | None = None
    # set by agent/validation/semantic.py
    verdict: Literal["SUPPORTS", "CONTRADICTS", "IRRELEVANT", "INSUFFICIENT"] | None = None
    verdict_reason: str | None = None
    # set by agent/validation/precedence.py (None = not applicable)
    precedes_symptom: bool | None = None

    @property
    def usable(self) -> bool:
        """Only usable evidence may influence the critic or the gate."""
        if self.structural != "valid" or self.verdict is None:
            return False
        expected = "SUPPORTS" if self.claimed == "supports" else "CONTRADICTS"
        return self.verdict == expected and self.precedes_symptom is not False


class LedgerEntry(BaseModel):
    """A verified contradiction the critic must answer before accepting."""
    hypothesis_id: str
    evidence_id: str
    resolved_by_critic: str | None = None


# ---------------------------------------------------------------- decision
class ConfidenceSignals(BaseModel):
    verbal: float | None = None            # critic's stated confidence, 0..1
    self_consistency: float | None = None  # share of 3 critic samples agreeing
    evidence_margin: float | None = None   # usable support minus contradiction, vs runner-up
    combined: float | None = None          # calibrated model output (dev-fitted)


class CriticVerdict(BaseModel):
    decision: Literal["accept", "more_evidence"]
    hypothesis_id: str | None = None
    requests: list[str] = Field(default_factory=list)  # specific evidence asked for
    confidence: float | None = None
    reason: str


# ---------------------------------------------------------------- actions
ActionStatus = Literal[
    "proposed", "awaiting_approval", "approved", "rejected", "expired",
    "revalidating", "blocked", "executed", "verifying", "resolved", "not_effective",
]


class ActionProposal(BaseModel):
    id: str
    idempotency_key: str
    action: Literal["restart", "rollback", "scale"]
    target: Service
    params: dict = Field(default_factory=dict)
    evidence_ids: list[str]
    preconditions: dict           # deploy_id, replicas, health, alert_firing
    proposed_at: datetime
    expires_at: datetime
    required_role: Literal["approver"] = "approver"
    status: ActionStatus = "proposed"
    approved_by: str | None = None
    approved_at: datetime | None = None
    blocked_reason: str | None = None
    verification: dict | None = None  # alert_cleared, metric_in_band, checked_at

    @model_validator(mode="after")
    def _expiry_after_proposal(self):
        if self.expires_at <= self.proposed_at:
            raise ValueError("expires_at must be after proposed_at")
        return self


# ---------------------------------------------------------------- reports
class Report(BaseModel):
    investigation_id: str
    stage: Literal["preliminary", "final"]
    root_cause: Hypothesis | None
    top_hypotheses: list[Hypothesis]          # ranked, at most 3; used for top-3 accuracy
    confidence: ConfidenceSignals
    gate_result: Literal["pass", "needs_review", "capped"]
    evidence_ids: list[str]                   # usable evidence cited
    open_contradictions: list[LedgerEntry]
    proposed_action: ActionProposal | None
    summary: str
    timeline: list[dict] = Field(default_factory=list)
    outcome: dict | None = None               # final report only: what ran, verification


# ---------------------------------------------------------------- graph state
class InvestigationState(TypedDict, total=False):
    investigation_id: str
    config_id: str                            # C0, C2, A1, ...
    alert: dict                               # Alertmanager payload
    alert_window: tuple[datetime, datetime]
    hypotheses: list[Hypothesis]
    evidence: Annotated[list[Evidence], operator.add]   # never deleted, only marked
    ledger: list[LedgerEntry]
    critic_verdicts: Annotated[list[CriticVerdict], operator.add]
    signals: ConfidenceSignals
    gate_result: Literal["pass", "needs_review", "capped"] | None
    root_cause: Hypothesis | None
    proposed_action: ActionProposal | None
    reports: Annotated[list[Report], operator.add]
    steps: int
    tool_calls: int
    tokens: int
