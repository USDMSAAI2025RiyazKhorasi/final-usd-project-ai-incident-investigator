"""Data backends behind the read-only tools.

SnapshotBackend answers every call from snapshots/<split>/<id>/ and reads only
agent-visible files (an allowlist), never scenario.yaml or fault_window.json.
LiveBackend answers the same calls from Prometheus, Loki and a deploy log.

Both expose the same methods, so the tools and agents are identical in either mode.
Times are POSIX seconds (float).
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AGENT_VISIBLE = {"alert.json", "window.json", "service_graph.json", "deploys.json", "meta.json"}
INVESTIGATION_DELAY_S = 30          # the investigation starts this long after the alert fires


_ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$")


def ts(s: str) -> float:
    """ISO-8601 to POSIX seconds. Accepts any fractional precision and Z, so it works on Python 3.10."""
    m = _ISO.match(s.strip())
    if not m:
        return datetime.fromisoformat(s).timestamp()
    base, frac, tz = m.groups()
    tz = "+00:00" if tz in (None, "Z") else (tz if ":" in tz else f"{tz[:3]}:{tz[3:]}")
    t = datetime.fromisoformat(base + tz).timestamp()
    return t + (float(f"0.{frac}") if frac else 0.0)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SnapshotBackend:
    mode = "snapshot"

    def __init__(self, snapshot_dir: str | Path):
        self.dir = Path(snapshot_dir)
        alert = self._json("alert.json")["alerts"][0]
        window = self._json("window.json")
        self.window_start = ts(window["baseline_start"])
        self.alert_at = ts(alert["startsAt"])
        self.now = self.alert_at + INVESTIGATION_DELAY_S       # tools never see data after "now"
        self._metrics: dict[str, list] = {}

    def _json(self, name: str):
        if name not in AGENT_VISIBLE:
            raise PermissionError(f"{name} is not visible to the investigator")
        return json.loads((self.dir / name).read_text())

    # -------------------------------------------------------------- API used by tools
    def alert(self) -> dict:
        return self._json("alert.json")

    def service_graph(self) -> dict:
        return self._json("service_graph.json")

    def metric(self, name: str, labels: dict, start: float, end: float) -> list[tuple[float, float]]:
        if name not in self._metrics:
            path = self.dir / "metrics" / f"{name}.json"
            self._metrics[name] = json.loads(path.read_text())["result"] if path.exists() else []
        for r in self._metrics[name]:
            if all(r["metric"].get(k) == v for k, v in labels.items()):
                end = min(end, self.now)
                return [(float(t), float(v)) for t, v in r["values"] if start <= float(t) <= end]
        return []

    def logs(self, service: str, start: float, end: float) -> list[dict]:
        path = self.dir / "logs" / f"{service}.jsonl"
        if not path.exists():
            return []
        end = min(end, self.now)
        out = []
        for line in path.read_text().splitlines():
            d = json.loads(line)
            t = ts(d["ts"])
            if start <= t <= end:
                out.append(d)
        return out

    def deploys(self, start: float, end: float) -> list[dict]:
        end = min(end, self.now)
        return [d for d in self._json("deploys.json") if start <= ts(d["at"]) <= end]

    def commit(self, sha: str) -> dict | None:
        if not all(c in "0123456789abcdef" for c in sha.lower()):
            return None
        path = self.dir / "commits" / f"{sha}.json"
        return json.loads(path.read_text()) if path.exists() else None


class LiveBackend:
    """Reads a running stack. Deploy log is a JSONL file written by recorder/inject.py."""
    mode = "live"

    def __init__(self, alert: dict, prometheus: str | None = None, loki: str | None = None,
                 deploy_log: str | Path | None = None, commits_dir: str | Path | None = None,
                 service_graph: dict | None = None):
        self._alert = alert
        self.prom = (prometheus or os.environ.get("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
        self.loki = (loki or os.environ.get("LOKI_URL", "http://localhost:3100")).rstrip("/")
        self.deploy_log = Path(deploy_log or os.environ.get("DEPLOY_LOG", "results/live/deploys.jsonl"))
        self.commits_dir = Path(commits_dir or os.environ.get("COMMITS_DIR", "results/live/commits"))
        self._graph = service_graph or {
            "calls": {"gateway": ["orders"], "orders": ["payments", "inventory", "notifications"],
                      "payments": [], "inventory": [], "notifications": []},
            "dependencies": {"orders": "postgres", "inventory": "postgres", "notifications": "redis",
                             "payments": "bank"}}
        self.alert_at = ts(alert["alerts"][0]["startsAt"])
        self.window_start = self.alert_at - 15 * 60
        self.now = datetime.now(timezone.utc).timestamp()

    def _get(self, url: str) -> dict:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())

    def alert(self) -> dict:
        return self._alert

    def service_graph(self) -> dict:
        return self._graph

    def metric(self, name: str, labels: dict, start: float, end: float) -> list[tuple[float, float]]:
        sel = ",".join(f'{k}="{v}"' for k, v in labels.items())
        q = urllib.parse.urlencode({"query": f"{name}{{{sel}}}", "start": start, "end": min(end, self.now),
                                    "step": "5s"})
        res = self._get(f"{self.prom}/api/v1/query_range?{q}")["data"]["result"]
        return [(float(t), float(v)) for t, v in res[0]["values"]] if res else []

    def logs(self, service: str, start: float, end: float) -> list[dict]:
        q = urllib.parse.urlencode({"query": f'{{service="{service}"}}', "start": int(start * 1e9),
                                    "end": int(min(end, self.now) * 1e9), "limit": 5000, "direction": "forward"})
        out = []
        for stream in self._get(f"{self.loki}/loki/api/v1/query_range?{q}")["data"]["result"]:
            for _, line in stream["values"]:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"ts": iso(start), "service": service, "level": "INFO", "message": line})
        return sorted(out, key=lambda d: d["ts"])

    def deploys(self, start: float, end: float) -> list[dict]:
        if not self.deploy_log.exists():
            return []
        rows = [json.loads(l) for l in self.deploy_log.read_text().splitlines() if l.strip()]
        return [d for d in rows if start <= ts(d["at"]) <= min(end, self.now)]

    def commit(self, sha: str) -> dict | None:
        path = self.commits_dir / f"{sha}.json"
        return json.loads(path.read_text()) if path.exists() else None
