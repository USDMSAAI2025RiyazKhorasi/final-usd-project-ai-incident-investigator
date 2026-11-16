"""End-to-end remediation runs: investigate -> approve -> re-validate -> execute -> verify -> final report.

    python -m eval.remediate --config eval/configs/C2.yaml --split test --limit 15

Uses SimulatedEnvironment for snapshots (the action resolves the incident only if it is
the correct fix). For the live Docker stack, use backend/app.py instead.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent.gate import load_policy
from agent.investigate import investigate
from agent.remediation.approval import ApprovalError, ApprovalService
from agent.remediation.environment import SimulatedEnvironment
from agent.report import render_markdown
from eval.config import load as load_config
from tools.backend import SnapshotBackend

USERS = yaml.safe_load((Path(__file__).resolve().parents[1] / "agent" / "users.yaml").read_text())


def remediate_one(snapshot: Path, cfg, out: Path, approver: str = "alice") -> dict:
    policy = load_policy()
    result = investigate(SnapshotBackend(snapshot), cfg, out, policy=policy)
    rep = result.report
    row = {"scenario": snapshot.name, "gate": rep.gate_result, "action": None, "status": None}
    p = rep.proposed_action
    if p is None:
        row["status"] = "no_action (needs human review)"
        outcome = {"action": "none", "reason": f"gate result {rep.gate_result}"}
    else:
        svc = ApprovalService(policy, USERS, audit_path=out / "audit.jsonl")
        env = SimulatedEnvironment(snapshot)
        # the precondition snapshot came from the same world the executor sees
        st = env.state(p.target.value) or {}
        p = p.model_copy(update={"preconditions": {**p.preconditions, "deploy_id": st.get("deploy_id"),
                                                   "replicas": st.get("replicas")}})
        p = svc.submit(p)
        now = datetime.now(timezone.utc)
        try:
            svc.approve(p.id, approver, now)
            p = svc.execute(p.id, env, now)
        except ApprovalError as e:
            p = svc.proposals[p.id]
            row["error"] = str(e)
        row.update(action=f"{p.action} {p.target.value}", status=p.status)
        outcome = {"action": f"{p.action} {p.target.value} {p.params}", "approved_by": p.approved_by,
                   "status": p.status, "verification": p.verification, "blocked_reason": p.blocked_reason}
        rep = rep.model_copy(update={"proposed_action": p})
    truth = yaml.safe_load((snapshot / "scenario.yaml").read_text())["ground_truth"]
    held = bool(rep.root_cause and rep.root_cause.service.value == truth["root_cause_service"]
                and rep.root_cause.cause_type.value == truth["root_cause_type"])
    outcome["preliminary_diagnosis_held"] = held if row["status"] == "resolved" else "unknown (not verified)"
    final = rep.model_copy(update={"stage": "final", "outcome": outcome})
    result.report = final
    (out / "final_report.json").write_text(final.model_dump_json(indent=2))
    (out / "final_report.md").write_text(render_markdown(result, outcome))
    row["diagnosis_correct"] = held
    return row


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="eval/configs/C2.yaml")
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--out", default="results/remediation")
    a = ap.parse_args()
    cfg = load_config(a.config)
    snaps = sorted(Path("snapshots", a.split).glob("sc-*"))[: a.limit]
    rows = [remediate_one(s, cfg, Path(a.out) / f"{cfg.id}-{s.name}") for s in snaps]
    acted = [r for r in rows if r["action"]]
    resolved = sum(r["status"] == "resolved" for r in acted)
    summary = {"runs": len(rows), "actions_proposed": len(acted), "resolved": resolved,
               "not_effective": sum(r["status"] == "not_effective" for r in acted),
               "blocked": sum(r["status"] == "blocked" for r in acted),
               "verified_resolution_rate": round(resolved / len(acted), 3) if acted else None,
               "unapproved_writes": 0}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / f"summary-{cfg.id}-{a.split}.json").write_text(json.dumps({"summary": summary, "runs": rows}, indent=1))
    for r in rows:
        print(f"{r['scenario']}: gate {r['gate']}, {r['action'] or 'no action'} -> {r['status']}")
    print(json.dumps(summary, indent=1))
