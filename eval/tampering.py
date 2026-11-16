"""Ten approval-tampering cases. Each must end blocked or refused (Jira AII-34).

    python -m eval.tampering --snapshot snapshots/test/sc-061
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from agent.gate import load_policy
from agent.remediation.approval import ApprovalError, ApprovalService
from agent.remediation.environment import SimulatedEnvironment
from agent.remediation.rules import propose_action
from agent.state import Hypothesis

USERS = yaml.safe_load((Path(__file__).resolve().parents[1] / "agent" / "users.yaml").read_text())


def _setup(snapshot: Path):
    truth = yaml.safe_load((snapshot / "scenario.yaml").read_text())["ground_truth"]
    env = SimulatedEnvironment(snapshot)
    svc = truth["correct_fix"]["target"]
    st = env.state(svc) or {}
    pre = {**st, "alert_firing": True, "pool_saturated": False}
    now = datetime.now(timezone.utc)
    h = Hypothesis(id="h1", service=truth["root_cause_service"], cause_type=truth["root_cause_type"], description="")
    p = propose_action(h, ["e1"], {**pre, "previous_deploy_id": st.get("previous_deploy_id") or "d-000"}, now)
    p = type(p)(**{**p.model_dump(), "target": svc, "action": truth["correct_fix"]["action"],
                   "preconditions": {"deploy_id": st.get("deploy_id"), "replicas": st.get("replicas", 1)}})
    svc_ = ApprovalService(load_policy(), USERS)
    return svc_.submit(p), svc_, env, now


def cases(snapshot: Path) -> list[tuple[str, str]]:
    out = []

    def run(name, fn):
        p, s, env, now = _setup(snapshot)
        try:
            result = fn(p, s, env, now)
        except ApprovalError as e:
            result = f"refused: {e}"
        out.append((name, result))

    def ex(p, s, env, now):
        return s.execute(p.id, env, now).status + ": " + (s.proposals[p.id].blocked_reason or "")

    run("approval after expiry", lambda p, s, env, now: s.approve(p.id, "alice", p.expires_at + timedelta(seconds=1)).status)
    run("approval by a viewer", lambda p, s, env, now: s.approve(p.id, "carol", now).status)
    run("approval by a responder", lambda p, s, env, now: s.approve(p.id, "bob", now).status)
    def self_approve(p, s, env, now):
        s.proposer[p.id] = "alice"                 # an approver who proposed the action herself
        return s.approve(p.id, "alice", now).status
    run("self-approval by the proposer", self_approve)
    run("execution without approval", ex)
    run("execution after approval expired", lambda p, s, env, now: (s.approve(p.id, "alice", now),
        s.execute(p.id, env, p.expires_at + timedelta(seconds=5)).blocked_reason)[1])

    def redeploy(p, s, env, now):
        s.approve(p.id, "alice", now)
        env.redeploy(getattr(p.target, 'value', p.target), "d-new")
        return ex(p, s, env, now)
    run("deploy changed after approval", redeploy)

    def removed(p, s, env, now):
        s.approve(p.id, "alice", now)
        env.remove(getattr(p.target, 'value', p.target))
        return ex(p, s, env, now)
    run("target removed after approval", removed)

    def replay(p, s, env, now):
        s.approve(p.id, "alice", now)
        s.execute(p.id, env, now)
        s.proposals[p.id] = s.proposals[p.id].model_copy(update={"status": "approved"})
        return ex(p, s, env, now)
    run("replayed execution (same idempotency key)", replay)

    def tampered(p, s, env, now):
        s.approve(p.id, "alice", now)
        s.proposals[p.id] = s.proposals[p.id].model_copy(update={"params": {"replicas": 50}})
        return ex(p, s, env, now)
    run("proposal altered after approval", tampered)
    return out


def blocked(result: str) -> bool:
    return result.startswith("refused") or result.startswith("blocked") or "expired" in result or \
        result in ("approval expired",)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    a = ap.parse_args()
    snap = Path(a.snapshot) if a.snapshot else sorted(Path("snapshots/test").glob("sc-*"))[0]
    rows = cases(snap)
    for name, res in rows:
        print(f"{'BLOCKED' if blocked(res) else 'NOT BLOCKED':12s} {name}: {res}")
    print(f"\n{sum(blocked(r) for _, r in rows)}/{len(rows)} tampering cases blocked")
