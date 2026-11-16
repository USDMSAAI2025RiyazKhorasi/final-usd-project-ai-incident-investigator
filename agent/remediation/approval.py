"""Approval lifecycle: an approval is a short-lived permission for one exact action on one
known system state.

proposed -> awaiting_approval -> approved | rejected | expired
approved -> revalidating -> blocked | executed -> verifying -> resolved | not_effective

Every transition is written to an append-only audit log (JSONL).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agent.state import ActionProposal

SYSTEM_PROPOSER = "investigator"


def _tv(p: ActionProposal) -> str:
    return getattr(p.target, "value", p.target)


class ApprovalError(RuntimeError):
    pass


def proposal_hash(p: ActionProposal) -> str:
    """What the approver actually approved: action, target, parameters and the state they assumed."""
    basis = {"action": p.action, "target": _tv(p), "params": p.params, "preconditions": p.preconditions,
             "id": p.id, "expires_at": p.expires_at.isoformat()}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ApprovalService:
    def __init__(self, policy: dict, users: dict[str, str], audit_path: str | Path | None = None):
        self.policy = policy
        self.users = users                        # user name -> role
        self.audit_path = Path(audit_path) if audit_path else None
        self.proposals: dict[str, ActionProposal] = {}
        self.approved_hash: dict[str, str] = {}
        self.proposer: dict[str, str] = {}
        self.executed_keys: set[str] = set()
        self.log: list[dict] = []

    # ---------------------------------------------------------- audit
    def _audit(self, event: str, p: ActionProposal, **kw) -> None:
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event, "proposal": p.id,
               "action": p.action, "target": _tv(p), "status": p.status, **kw}
        self.log.append(row)
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a") as f:
                f.write(json.dumps(row, default=str) + "\n")

    def _can(self, user: str, permission: str) -> bool:
        role = self.users.get(user)
        return bool(role) and permission in self.policy["actions"]["roles"].get(role, [])

    def _set(self, p: ActionProposal, status: str, **kw) -> ActionProposal:
        p2 = p.model_copy(update={"status": status, **kw})
        self.proposals[p.id] = p2
        return p2

    # ---------------------------------------------------------- lifecycle
    def submit(self, p: ActionProposal, proposer: str = SYSTEM_PROPOSER) -> ActionProposal:
        if p.action not in self.policy["actions"]["allowlist"]:
            raise ApprovalError(f"action {p.action} is not allowlisted")
        self.proposer[p.id] = proposer
        p = self._set(p, "awaiting_approval")
        self._audit("submitted", p, by=proposer, hash=proposal_hash(p))
        return p

    def approve(self, pid: str, user: str, now: datetime, seen_hash: str | None = None) -> ActionProposal:
        p = self._get(pid)
        if p.status != "awaiting_approval":
            raise ApprovalError(f"proposal is {p.status}, not awaiting approval")
        if not self._can(user, "approve"):
            self._audit("approval_denied", p, by=user, reason="role")
            raise ApprovalError(f"{user} is not allowed to approve")
        if user == self.proposer.get(pid):
            self._audit("approval_denied", p, by=user, reason="self-approval")
            raise ApprovalError("the proposer cannot approve their own action")
        if now > p.expires_at:
            self._set(p, "expired")
            self._audit("expired", p, by=user)
            raise ApprovalError("proposal expired before approval")
        h = proposal_hash(p)
        if seen_hash is not None and seen_hash != h:
            self._audit("approval_denied", p, by=user, reason="hash mismatch")
            raise ApprovalError("the proposal changed since it was shown to the approver")
        self.approved_hash[pid] = h
        p = self._set(p, "approved", approved_by=user, approved_at=now)
        self._audit("approved", p, by=user, hash=h)
        return p

    def reject(self, pid: str, user: str, now: datetime, reason: str = "") -> ActionProposal:
        p = self._get(pid)
        if not self._can(user, "approve"):
            raise ApprovalError(f"{user} is not allowed to reject")
        p = self._set(p, "rejected")
        self._audit("rejected", p, by=user, reason=reason)
        return p

    def execute(self, pid: str, env, now: datetime) -> ActionProposal:
        """Re-validate everything immediately before acting; block on any drift."""
        p = self._get(pid)
        p = self._set(p, "revalidating") if p.status == "approved" else p

        def block(reason: str) -> ActionProposal:
            q = self._set(p, "blocked", blocked_reason=reason)
            self._audit("blocked", q, reason=reason)
            return q

        if p.status != "revalidating":
            return block(f"not approved (status {p.status})")
        if p.idempotency_key in self.executed_keys:
            return block("already executed (idempotency key seen)")
        if now > p.expires_at:
            return block("approval expired")
        if not self._can(p.approved_by or "", "approve"):
            return block("approver no longer authorised")
        if self.approved_hash.get(pid) != proposal_hash(p):
            return block("proposal changed after approval")
        if p.action not in self.policy["actions"]["allowlist"]:
            return block("action not allowlisted")
        state = env.state(_tv(p))
        if state is None:
            return block("target no longer exists")
        pre = p.preconditions
        if pre.get("deploy_id") and state.get("deploy_id") != pre.get("deploy_id"):
            return block(f"deploy changed ({pre.get('deploy_id')} -> {state.get('deploy_id')})")
        if pre.get("replicas") is not None and state.get("replicas") != pre.get("replicas"):
            return block("replica count changed")
        if not env.alert_firing():
            return block("alert already cleared")
        if p.action == "rollback" and not state.get("previous_deploy_id"):
            return block("no previous release to roll back to")

        result = env.apply(p.action, _tv(p), p.params)
        self.executed_keys.add(p.idempotency_key)
        p = self._set(p, "executed")
        self._audit("executed", p, result=result)
        p = self._set(p, "verifying")
        v = env.verify(p)
        p = self._set(p, "resolved" if v.get("resolved") else "not_effective", verification=v)
        self._audit(p.status, p, verification=v)
        return p

    def _get(self, pid: str) -> ActionProposal:
        if pid not in self.proposals:
            raise ApprovalError(f"unknown proposal {pid}")
        return self.proposals[pid]
