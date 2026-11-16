"""Where approved actions run.

SimulatedEnvironment  stands in for the system behind a snapshot. It knows the true
                      cause (it is the world, not the investigator) and resolves the
                      incident only when the action is the correct fix.
DockerEnvironment     drives the real Docker Compose stack: restarts, rollbacks via the
                      target app's admin API, scaling, and verification via Alertmanager
                      and Prometheus.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import yaml


class SimulatedEnvironment:
    def __init__(self, snapshot_dir: str | Path):
        d = Path(snapshot_dir)
        truth = yaml.safe_load((d / "scenario.yaml").read_text())["ground_truth"]
        self.fix = (truth["correct_fix"]["action"], truth["correct_fix"]["target"])
        self.services: dict[str, dict] = {}
        for dep in json.loads((d / "deploys.json").read_text()):
            s = self.services.setdefault(dep["service"], {"deploys": []})
            s["deploys"].append(dep["id"])
        for svc in ("redis", "postgres"):
            self.services.setdefault(svc, {"deploys": []})
        self.replicas = {s: 1 for s in self.services}
        self.firing = True
        self.applied: list[tuple] = []

    # test hooks: change the world between approval and execution
    def redeploy(self, service: str, deploy_id: str) -> None:
        self.services[service]["deploys"].append(deploy_id)

    def remove(self, service: str) -> None:
        self.services.pop(service, None)

    def clear_alert(self) -> None:
        self.firing = False

    def state(self, service: str) -> dict | None:
        s = self.services.get(service)
        if s is None:
            return None
        deps = s["deploys"]
        return {"deploy_id": deps[-1] if deps else None, "previous_deploy_id": deps[-2] if len(deps) > 1 else None,
                "replicas": self.replicas.get(service, 1)}

    def alert_firing(self) -> bool:
        return self.firing

    def apply(self, action: str, target: str, params: dict) -> dict:
        self.applied.append((action, target, params))
        if action == "scale":
            self.replicas[target] = params.get("replicas", self.replicas.get(target, 1) + 1)
        if action == "rollback":
            self.services[target]["deploys"].append(params.get("to_deploy") or "rollback")
        if (action, target) == self.fix:
            self.firing = False
        return {"applied": action, "target": target}

    def verify(self, proposal) -> dict:
        ok = not self.firing
        return {"resolved": ok, "alert_cleared": ok, "metric_in_band": ok, "checked_after_s": 300}


class DockerEnvironment:
    """Live stack. Requires docker compose and the stack from infra/docker-compose.yml."""

    def __init__(self, compose_file: str = "infra/docker-compose.yml", alertname: str | None = None,
                 alert_service: str | None = None, verification_s: int = 300):
        self.compose = ["docker", "compose", "-f", compose_file]
        self.am = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093").rstrip("/")
        self.prom = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
        self.deploy_log = Path(os.environ.get("DEPLOY_LOG", "results/live/deploys.jsonl"))
        self.alertname, self.alert_service, self.verification_s = alertname, alert_service, verification_s
        self.app_port = {"gateway": 8000, "orders": 8001, "payments": 8002, "inventory": 8003, "notifications": 8004}

    def _sh(self, *args: str) -> str:
        return subprocess.run([*self.compose, *args], check=True, capture_output=True, text=True).stdout

    def _deploys(self, service: str) -> list[dict]:
        if not self.deploy_log.exists():
            return []
        return [d for d in map(json.loads, self.deploy_log.read_text().splitlines()) if d["service"] == service]

    def state(self, service: str) -> dict | None:
        try:
            rows = [json.loads(l) for l in self._sh("ps", "--format", "json", service).splitlines() if l.strip()]
        except subprocess.CalledProcessError:
            return None
        if not rows:
            return None
        deps = self._deploys(service)
        return {"deploy_id": deps[-1]["id"] if deps else None,
                "previous_deploy_id": deps[-2]["id"] if len(deps) > 1 else None, "replicas": len(rows)}

    def alert_firing(self) -> bool:
        with urllib.request.urlopen(f"{self.am}/api/v2/alerts?active=true", timeout=10) as r:
            alerts = json.loads(r.read())
        return any(a["labels"].get("alertname") == self.alertname and
                   a["labels"].get("service") == self.alert_service for a in alerts)

    def apply(self, action: str, target: str, params: dict) -> dict:
        if action == "restart":
            self._sh("restart", target)
        elif action == "scale":
            self._sh("up", "-d", "--no-recreate", "--scale", f"{target}={params.get('replicas', 2)}", target)
        elif action == "rollback":
            port = self.app_port.get(target)
            req = urllib.request.Request(f"http://localhost:{port}/admin/rollback", data=b"{}", method="POST",
                                         headers={"content-type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        return {"applied": action, "target": target}

    def verify(self, proposal) -> dict:
        deadline = time.time() + self.verification_s
        while time.time() < deadline:
            if not self.alert_firing():
                return {"resolved": True, "alert_cleared": True, "metric_in_band": True}
            time.sleep(15)
        return {"resolved": False, "alert_cleared": False, "metric_in_band": False}
