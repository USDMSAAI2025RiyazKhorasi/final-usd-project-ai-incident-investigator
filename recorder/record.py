"""Record real incidents from the live Docker stack into snapshots (same format as the simulator).

    make up                                  # start the stack and wait ~2 minutes for traffic
    python -m recorder.record --scenario scenarios/generated/dev/sc-031.yaml --out snapshots-live
    python -m recorder.record --split dev --out snapshots-live     # every dev scenario, one after another

For each scenario: reset faults, run baseline traffic, inject the fault and noise, wait for
Alertmanager to fire, keep 5 more minutes, then export Prometheus series, Loki logs, the
release log and commits into snapshots/<split>/<id>/ with SHA256SUMS.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

from recorder.simulate import Sim
from scenarios.schema import Scenario

ROOT = Path(__file__).resolve().parents[1]
PROM = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
LOKI = os.environ.get("LOKI_URL", "http://localhost:3100").rstrip("/")
AM = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093").rstrip("/")
LIVE = Path(os.environ.get("LIVE_DIR", ROOT / "results" / "live"))
PORT = {"gateway": 8000, "orders": 8001, "payments": 8002, "inventory": 8003, "notifications": 8004}
SERVICES = list(PORT)
METRICS = ["http_requests_total", "http_request_duration_seconds", "container_memory_bytes",
           "process_cpu_seconds_total", "db_pool_in_use", "db_query_seconds", "dependency_up", "disk_free_bytes"]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "infra" / "docker-compose.yml")]
RELEASE_MSG = {
    "bad_config_deploy": ("config: tune outbound timeouts", "config/settings.yaml", "-  timeout_ms: 2000\n+  timeout_ms: 50"),
    "buggy_code_deploy": ("feat: apply discount codes at checkout", "app/handlers/checkout.py", "+    code = request.json['discount_code']"),
    "expired_credential": ("ops: rotate bank API credentials", "config/secrets.ref.yaml", "-  bank_token_ref: vault://bank/token/v7\n+  bank_token_ref: vault://bank/token/v6"),
}


def _req(url: str, body: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                               headers={"content-type": "application/json"})
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read() or b"{}")


def admin(service: str, path: str, body: dict | None = None) -> dict:
    return _req(f"http://localhost:{PORT[service]}/admin/{path}", body if body is not None else {})


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------ injection
def reset() -> None:
    subprocess.run([*COMPOSE, "start", "redis"], check=False, capture_output=True)
    for s in SERVICES:
        try:
            admin(s, "reset")
        except OSError:
            pass


def inject(sc: Scenario, noise_service: str) -> None:
    tgt, fault = sc.target.value, sc.fault
    params = {"intensity": sc.intensity}
    if sc.noise == "unrelated_deploy":
        admin(noise_service, "release", {"message": "chore: bump dependencies",
                                         "files": [{"path": "requirements.txt", "additions": 6, "deletions": 6}]})
    if sc.noise == "background_errors":
        admin(noise_service, "faults/background_errors", {"enabled": True})
    if sc.noise == "concurrent_anomaly":
        admin(noise_service, "faults/cpu_burst", {"enabled": True})
    if fault in RELEASE_MSG:
        msg, path, diff = RELEASE_MSG[fault]
        admin(tgt, "release", {"message": msg, "files": [{"path": path, "additions": 1, "deletions": 1, "diff": diff}]})
    if fault == "dependency_down":
        subprocess.run([*COMPOSE, "stop", "redis"], check=True, capture_output=True)
    else:
        admin(tgt, f"faults/{fault}", {"enabled": True, "params": params})


def wait_for_alert(since: float, timeout: float = 600) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for a in _req(f"{AM}/api/v2/alerts?active=true"):
            started = datetime.fromisoformat(a["startsAt"][:19] + "+00:00").timestamp()
            if started >= since - 5:
                return a
        time.sleep(5)
    return None


# ------------------------------------------------------------------ export
def export(sc: Scenario, out: Path, start: float, fault_at: float, alert: dict, end: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics").mkdir(exist_ok=True)
    for m in METRICS:
        q = urllib.parse.urlencode({"query": f'{m}{{service=~".+"}}', "start": start, "end": end, "step": "5s"})
        res = _req(f"{PROM}/api/v1/query_range?{q}")["data"]["result"]
        (out / "metrics" / f"{m}.json").write_text(json.dumps({"resultType": "matrix", "result": res}))
    (out / "logs").mkdir(exist_ok=True)
    for s in SERVICES:
        q = urllib.parse.urlencode({"query": f'{{service="{s}"}}', "start": int(start * 1e9), "end": int(end * 1e9),
                                    "limit": 5000, "direction": "forward"})
        lines = []
        for stream in _req(f"{LOKI}/loki/api/v1/query_range?{q}")["data"]["result"]:
            for _, line in stream["values"]:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        lines.sort(key=lambda d: d["ts"])
        (out / "logs" / f"{s}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    deploys = [json.loads(l) for l in (LIVE / "deploys.jsonl").read_text().splitlines()] if (LIVE / "deploys.jsonl").exists() else []
    deploys = [d for d in deploys if d["at"] <= iso(end)]
    (out / "deploys.json").write_text(json.dumps(deploys))
    (out / "commits").mkdir(exist_ok=True)
    for d in deploys:
        src = LIVE / "commits" / f"{d['sha']}.json"
        if src.exists():
            (out / "commits" / src.name).write_text(src.read_text())
    (out / "service_graph.json").write_text(json.dumps({
        "calls": {"gateway": ["orders"], "orders": ["payments", "inventory", "notifications"],
                  "payments": [], "inventory": [], "notifications": []},
        "dependencies": {"orders": "postgres", "inventory": "postgres", "notifications": "redis", "payments": "bank"}}))
    (out / "window.json").write_text(json.dumps({"baseline_start": iso(start), "alert_at": alert["startsAt"],
                                                 "end": iso(end)}))
    (out / "alert.json").write_text(json.dumps({"receiver": "investigator", "status": "firing", "alerts": [
        {"status": "firing", "labels": {k: v for k, v in alert["labels"].items() if k in ("alertname", "service", "severity")},
         "annotations": alert.get("annotations", {}), "startsAt": alert["startsAt"]}]}))
    (out / "meta.json").write_text(json.dumps({"source": "recorded", "recorder": "recorder.record v1", "step_seconds": 5}))
    (out / "scenario.yaml").write_text(yaml.safe_dump(sc.model_dump(mode="json"), sort_keys=False))
    (out / "fault_window.json").write_text(json.dumps({"fault_at": iso(fault_at),
                                                      "alert_service_observed": alert["labels"].get("service")}))
    Sim._sums(out)


def record(sc: Scenario, out_root: Path, baseline_s: int | None = None, post_s: int = 300) -> Path:
    noise_service = Sim(sc).noise_service
    reset()
    start = time.time()
    time.sleep(baseline_s if baseline_s is not None else sc.onset_offset_s)
    fault_at = time.time()
    inject(sc, noise_service)
    alert = wait_for_alert(fault_at)
    if alert is None:
        reset()
        raise RuntimeError(f"{sc.id}: no alert fired within 10 minutes")
    time.sleep(post_s)
    end = time.time()
    out = out_root / sc.split / sc.id
    export(sc, out, start, fault_at, alert, end)
    reset()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", type=Path)
    ap.add_argument("--split", choices=["train", "dev", "test"])
    ap.add_argument("--scenarios", type=Path, default=ROOT / "scenarios" / "generated")
    ap.add_argument("--out", type=Path, default=ROOT / "snapshots-live")
    ap.add_argument("--post", type=int, default=300, help="seconds to keep after the alert")
    a = ap.parse_args()
    paths = [a.scenario] if a.scenario else sorted((a.scenarios / a.split).glob("sc-*.yaml"))
    for p in paths:
        sc = Scenario(**yaml.safe_load(p.read_text()))
        try:
            print(f"recorded {record(sc, a.out, post_s=a.post)}", flush=True)
        except Exception as e:
            print(f"FAILED {sc.id}: {e}", flush=True)
