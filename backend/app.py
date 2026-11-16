"""Backend API: alert intake, investigations, approval workflow, reports.

    uvicorn backend.app:app --port 8080

Endpoints
  POST /alerts                         Alertmanager webhook; one investigation per firing alert
  POST /investigations/snapshot        {"snapshot": "snapshots/test/sc-061", "config": "C2"} demo without Docker
  GET  /investigations                 list
  GET  /investigations/{id}            report, evidence, ledger, trace, proposal
  POST /investigations/{id}/approve    header X-User; body {"hash": "<shown proposal hash>"}
  POST /investigations/{id}/reject     header X-User; body {"reason": "..."}

Identity is a demo header (X-User) mapped to roles in agent/users.yaml; put real auth in
front of this before using it anywhere shared.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from agent.gate import load_policy
from agent.investigate import investigate
from agent.remediation.approval import ApprovalError, ApprovalService, proposal_hash
from agent.remediation.environment import DockerEnvironment, SimulatedEnvironment
from agent.report import render_markdown
from eval.config import load as load_config
from tools.backend import LiveBackend, SnapshotBackend

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results" / "live" / "investigations"
USERS = yaml.safe_load((ROOT / "agent" / "users.yaml").read_text())
app = FastAPI(title="AI Incident Investigator")
_lock = threading.Lock()
INV: dict[str, dict] = {}            # id -> {status, key, result, approvals, env, snapshot}


def _start(inv_id: str, backend, env_factory, config: str, key: str, snapshot: str | None = None) -> None:
    cfg = load_config(ROOT / "eval" / "configs" / f"{config}.yaml")
    with _lock:
        INV[inv_id] = {"id": inv_id, "status": "investigating", "key": key, "config": config, "snapshot": snapshot,
                       "created": datetime.now(timezone.utc).isoformat()}

    def work():
        try:
            res = investigate(backend, cfg, OUT / inv_id, investigation_id=inv_id)
            svc = ApprovalService(load_policy(), USERS, audit_path=OUT / inv_id / "audit.jsonl")
            p = res.report.proposed_action
            if p is not None:
                env = env_factory()
                st = env.state(p.target.value) or {}
                p = svc.submit(p.model_copy(update={"preconditions": {**p.preconditions,
                                                                     "deploy_id": st.get("deploy_id"),
                                                                     "replicas": st.get("replicas")}}))
                res.report = res.report.model_copy(update={"proposed_action": p})
            with _lock:
                INV[inv_id].update(status="awaiting_approval" if p else "needs_review", result=res, approvals=svc,
                                   env_factory=env_factory)
        except Exception as e:
            with _lock:
                INV[inv_id].update(status="failed", error=str(e))
    threading.Thread(target=work, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True, "investigations": len(INV)}


@app.post("/alerts")
def alerts(payload: dict):
    started = []
    for a in payload.get("alerts", []):
        if a.get("status") != "firing":
            continue
        key = f"{a['labels'].get('alertname')}|{a['labels'].get('service')}"
        with _lock:
            open_ = next((i for i in INV.values() if i["key"] == key and i["status"] not in ("closed", "failed")), None)
        if open_:
            open_.setdefault("duplicates", 0)
            open_["duplicates"] += 1
            continue
        inv_id = f"inv-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        backend = LiveBackend({"alerts": [a]}, deploy_log=ROOT / "results" / "live" / "deploys.jsonl",
                              commits_dir=ROOT / "results" / "live" / "commits")
        _start(inv_id, backend, lambda a=a: DockerEnvironment(alertname=a["labels"].get("alertname"),
                                                              alert_service=a["labels"].get("service")), "C2", key)
        started.append(inv_id)
    return {"started": started}


@app.post("/investigations/snapshot")
def from_snapshot(body: dict):
    snap = Path(body["snapshot"])
    snap = snap if snap.is_absolute() else ROOT / snap
    if not (snap / "alert.json").exists():
        raise HTTPException(404, f"no snapshot at {snap}")
    inv_id = f"inv-{snap.name}-{uuid.uuid4().hex[:4]}"
    _start(inv_id, SnapshotBackend(snap), lambda: SimulatedEnvironment(snap), body.get("config", "C2"),
           f"snapshot|{snap.name}|{inv_id}", str(snap))
    return {"id": inv_id}


def _view(i: dict) -> dict:
    v = {k: i.get(k) for k in ("id", "status", "key", "config", "snapshot", "created", "error", "duplicates")}
    res = i.get("result")
    if res is not None:
        r = res.report
        p = i["approvals"].proposals.get(r.proposed_action.id) if r.proposed_action else None
        v.update(report=json.loads(r.model_dump_json()), markdown=render_markdown(res, i.get("outcome")),
                 evidence=[e.model_dump(mode="json") for e in res.evidence],
                 ledger=[l.model_dump(mode="json") for l in res.ledger], usage=res.usage,
                 proposal=json.loads(p.model_dump_json()) if p else None,
                 proposal_hash=proposal_hash(p) if p else None, audit=i["approvals"].log, outcome=i.get("outcome"))
    return v


@app.get("/investigations")
def list_investigations():
    return [{k: i.get(k) for k in ("id", "status", "key", "created")} for i in INV.values()]


@app.get("/investigations/{inv_id}")
def get_investigation(inv_id: str):
    if inv_id not in INV:
        raise HTTPException(404)
    return _view(INV[inv_id])


@app.post("/investigations/{inv_id}/approve")
def approve(inv_id: str, body: dict, background: BackgroundTasks, x_user: str = Header(...)):
    i = INV.get(inv_id)
    if not i or i.get("status") != "awaiting_approval":
        raise HTTPException(409, "nothing awaiting approval")
    svc, pid = i["approvals"], i["result"].report.proposed_action.id
    now = datetime.now(timezone.utc)
    try:
        svc.approve(pid, x_user, now, seen_hash=body.get("hash"))
    except ApprovalError as e:
        raise HTTPException(403, str(e))
    i["status"] = "executing"

    def run():
        p = svc.execute(pid, i["env_factory"](), datetime.now(timezone.utc))
        outcome = {"action": f"{p.action} {p.target.value} {p.params}", "approved_by": p.approved_by,
                   "status": p.status, "verification": p.verification, "blocked_reason": p.blocked_reason}
        res = i["result"]
        res.report = res.report.model_copy(update={"stage": "final", "proposed_action": p, "outcome": outcome})
        (OUT / inv_id / "final_report.json").write_text(res.report.model_dump_json(indent=2))
        (OUT / inv_id / "final_report.md").write_text(render_markdown(res, outcome))
        i.update(status="closed" if p.status == "resolved" else p.status, outcome=outcome)
    background.add_task(run)
    return {"approved": pid, "by": x_user}


@app.post("/investigations/{inv_id}/reject")
def reject(inv_id: str, body: dict, x_user: str = Header(...)):
    i = INV.get(inv_id)
    if not i or i.get("status") != "awaiting_approval":
        raise HTTPException(409, "nothing awaiting approval")
    try:
        i["approvals"].reject(i["result"].report.proposed_action.id, x_user, datetime.now(timezone.utc),
                              body.get("reason", ""))
    except ApprovalError as e:
        raise HTTPException(403, str(e))
    i["status"] = "closed"
    return {"rejected": True}
