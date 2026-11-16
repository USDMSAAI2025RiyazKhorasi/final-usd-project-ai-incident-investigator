"""Remediation rule table (Jira AII-23): (cause_type, service) -> one allowlisted action. No LLM.

Called only when the confidence gate passes. Returns None when no safe action fits,
so the report goes to a human instead.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from agent.state import ActionProposal, CauseType, Hypothesis, Service

POLICY = Path(__file__).resolve().parents[1] / "policy.yaml"

# cause type -> action. Matches correct_fix in scenarios/catalogue.yaml.
RULES: dict[CauseType, str] = {
    CauseType.config_change: "rollback",
    CauseType.code_change: "rollback",
    CauseType.database_performance: "rollback",
    CauseType.credential_failure: "rollback",
    CauseType.resource_exhaustion: "restart",       # "scale" when the DB pool is saturated
    CauseType.dependency_unavailable: "restart",    # restarts the dependency, not the caller
}

# service -> the dependency to restart when that dependency is unavailable
DEPENDENCY_OF: dict[Service, Service] = {
    Service.notifications: Service.redis,
    Service.orders: Service.postgres,
    Service.inventory: Service.postgres,
}


def load_allowlist(path: Path = POLICY) -> set[str]:
    return set(yaml.safe_load(path.read_text())["actions"]["allowlist"])


def _idempotency_key(action: str, target: Service, preconditions: dict) -> str:
    basis = json.dumps({"action": action, "target": target.value,
                        "deploy_id": preconditions.get("deploy_id")}, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def propose_action(
    root_cause: Hypothesis,
    evidence_ids: list[str],
    preconditions: dict,
    now: datetime,
    ttl_seconds: int = 900,
    allowlist: set[str] | None = None,
) -> ActionProposal | None:
    """Map an accepted root cause to at most one allowlisted action."""
    allowlist = allowlist if allowlist is not None else load_allowlist()
    action = RULES.get(root_cause.cause_type)
    target = root_cause.service

    if root_cause.cause_type == CauseType.resource_exhaustion and preconditions.get("pool_saturated"):
        action = "scale"
    if root_cause.cause_type == CauseType.dependency_unavailable:
        target = DEPENDENCY_OF.get(root_cause.service)
        if target is None:                      # external dependency (e.g. the bank): nothing we can restart
            return None
    if action == "rollback" and not preconditions.get("previous_deploy_id"):
        return None                             # nothing to roll back to
    if action is None or action not in allowlist:
        return None

    params = {"to_deploy": preconditions["previous_deploy_id"]} if action == "rollback" else {}
    if action == "scale":
        params = {"replicas": preconditions.get("replicas", 1) + 1}

    return ActionProposal(
        id=f"act-{uuid.uuid4().hex[:8]}",
        idempotency_key=_idempotency_key(action, target, preconditions),
        action=action,
        target=target,
        params=params,
        evidence_ids=evidence_ids,
        preconditions=preconditions,
        proposed_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        status="proposed",
    )
