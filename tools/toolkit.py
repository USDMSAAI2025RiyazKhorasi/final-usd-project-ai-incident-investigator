"""The read-only tools the agents call, with provenance for every result.

Each call returns a short text summary for the agent and stores the full raw
result (JSON) through RawStore, so evidence built from it can be re-checked.
Numbers in a summary appear verbatim in the stored raw result, which is what the
structural "grounded values" check relies on.

Write actions (restart, rollback, scale) are not tools: only agent/remediation/
executor.py can perform them, and only after approval.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from agent.state import Provenance
from tools import runbooks
from tools.base import RawStore

SERVICES = ["gateway", "orders", "payments", "inventory", "notifications"]
METRICS = {
    # agent-facing name: (description, unit)
    "error_rate": ("share of requests failing with a server error", "ratio"),
    "request_rate": ("requests per second", "req/s"),
    "latency_p95": ("95th percentile response time", "s"),
    "cpu": ("CPU use, cores", "cores"),
    "memory": ("container memory used", "MB"),
    "db_pool_in_use": ("busy database connections (pool of 20)", "connections"),
    "db_query_p95": ("95th percentile database query time", "s"),
    "dependency_up": ("dependency reachable (1 yes, 0 no)", "0/1"),
    "disk_free": ("free disk space", "GB"),
}
SPECS = {
    "get_alert": ("get_alert()", "The alert that started this investigation."),
    "get_service_graph": ("get_service_graph()", "Which service calls which, and what each depends on."),
    "scan_anomalies": ("scan_anomalies()", "For every service and metric, whether and when it departed from its "
                       "normal level, sorted by time. Good first step."),
    "query_metrics": ("query_metrics(service, metric)", f"One metric for one service. metric is one of: "
                      f"{', '.join(METRICS)}."),
    "search_logs": ("search_logs(service, query='', level='WARN')", "Warning and error log messages for a service, "
                    "grouped by pattern with counts and first/last time seen. level='ALL' includes routine lines; "
                    "query filters by text."),
    "list_deploys": ("list_deploys(service=None, hours=72)", "Releases in the last N hours, newest last."),
    "get_commit_diff": ("get_commit_diff(sha)", "Files and diff of the commit behind a release."),
    "search_runbooks": ("search_runbooks(query)", "Search the team's runbooks for guidance."),
}
ALL_TOOLS = list(SPECS)
BASELINE_S = 60          # the first minute of the window is treated as normal


def hhmmss(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S")


def isot(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt(x: float) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "n/a"
    if abs(x) >= 100:
        return f"{x:.0f}"
    if abs(x) >= 1:
        return f"{x:.2f}"
    return f"{x:.3f}"


# ------------------------------------------------------------------ metric derivation
def derived_series(backend, service: str, metric: str, start: float, end: float) -> list[tuple[float, float]]:
    """Agent-facing metric as (time, value) pairs. Counters become per-interval rates; resets handled."""
    def rate(points, per_second=True):
        out = []
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            dv = v1 - v0 if v1 >= v0 else v1          # counter reset on restart
            out.append((t1, dv / (t1 - t0) if per_second else dv))
        return out

    if metric in ("error_rate", "request_rate"):
        ok = backend.metric("http_requests_total", {"service": service, "status": "2xx"}, start, end)
        bad = dict(backend.metric("http_requests_total", {"service": service, "status": "5xx"}, start, end))
        pairs = [(t, v, bad[t]) for t, v in ok if t in bad]
        out = []
        for (t0, o0, b0), (t1, o1, b1) in zip(pairs, pairs[1:]):
            do = o1 - o0 if o1 >= o0 else o1
            db = b1 - b0 if b1 >= b0 else b1
            tot = do + db
            out.append((t1, (db / tot if tot > 0 else 0.0) if metric == "error_rate" else tot / (t1 - t0)))
        return out
    if metric == "latency_p95":
        return backend.metric("http_request_duration_seconds", {"service": service}, start, end)
    if metric == "cpu":
        return rate(backend.metric("process_cpu_seconds_total", {"service": service}, start, end))
    if metric == "memory":
        return [(t, v / 2**20) for t, v in backend.metric("container_memory_bytes", {"service": service}, start, end)]
    if metric == "db_pool_in_use":
        return backend.metric("db_pool_in_use", {"service": service}, start, end)
    if metric == "db_query_p95":
        return backend.metric("db_query_seconds", {"service": service}, start, end)
    if metric == "dependency_up":
        return backend.metric("dependency_up", {"service": service}, start, end)
    if metric == "disk_free":
        return [(t, v / 1e9) for t, v in backend.metric("disk_free_bytes", {"service": service}, start, end)]
    raise ValueError(f"unknown metric {metric}")


def departure(points: list[tuple[float, float]], baseline_end: float, z: float = 8, sustain: int = 3) -> dict:
    """When (if ever) a series left its normal band for `sustain` samples in a row."""
    if len(points) < 4:
        return {"departed": False}
    t = np.array([p[0] for p in points])
    x = np.array([p[1] for p in points])
    pre = x[t < baseline_end]
    if len(pre) < 3:
        return {"departed": False}
    mean, sd = float(pre.mean()), float(pre.std())
    sd = max(sd, abs(mean) * 0.05, 1e-6)
    out = (np.abs(x - mean) > z * sd) & (t >= baseline_end)
    run = np.convolve(out.astype(int), np.ones(sustain, int), "valid") == sustain
    hit = np.flatnonzero(run)
    now = float(x[-1])
    res = {"baseline": mean, "now": now, "max": float(x.max()), "min": float(x.min()),
           "z_now": (now - mean) / sd, "departed": bool(len(hit))}
    if len(hit):
        res["since"] = float(t[hit[0]])
        res["direction"] = "up" if x[hit[0]] > mean else "down"
    return res


# ------------------------------------------------------------------ toolkit
@dataclass
class ToolCall:
    call_id: str
    tool: str
    args: dict
    summary: str
    provenance: Provenance
    agent: str


@dataclass
class Toolkit:
    backend: object
    store: RawStore
    allowed: set[str] = field(default_factory=lambda: set(ALL_TOOLS))
    calls: list[ToolCall] = field(default_factory=list)

    def specs(self, names: set[str] | None = None) -> str:
        names = [n for n in ALL_TOOLS if n in self.allowed and (names is None or n in names)]
        return "\n".join(f"- {SPECS[n][0]}: {SPECS[n][1]}" for n in names)

    def get(self, call_id: str) -> ToolCall | None:
        return next((c for c in self.calls if c.call_id == call_id), None)

    # ---------------------------------------------------------- dispatch
    def call(self, tool: str, args: dict | None = None, agent: str = "single",
             permitted: set[str] | None = None) -> dict:
        args = dict(args or {})
        cid = f"c{len(self.calls) + 1}"
        if tool not in self.allowed or (permitted is not None and tool not in permitted):
            return {"call_id": None, "ok": False, "summary": f"tool '{tool}' is not available to you"}
        try:
            raw, summary, start, end = getattr(self, f"_{tool}")(**args)
            ok = True
        except (TypeError, ValueError, KeyError) as e:
            raw, summary, start, end = {"error": str(e)}, f"error: {e}", self.backend.window_start, self.backend.now
            ok = False
        prov = self.store.put(tool, args, raw, self.backend.mode, datetime.fromtimestamp(start, tz=timezone.utc),
                              datetime.fromtimestamp(end, tz=timezone.utc), ok=ok)
        self.calls.append(ToolCall(cid, tool, args, summary, prov, agent))
        return {"call_id": cid, "ok": ok, "summary": summary}

    def rerun_hash(self, call: ToolCall) -> str:
        """Re-execute a call without storing it; used by the structural re-run check."""
        raw, *_ = getattr(self, f"_{call.tool}")(**call.args)
        import hashlib
        return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()

    # ---------------------------------------------------------- tools
    def _span(self, minutes: float = 15) -> tuple[float, float]:
        b = self.backend
        return max(b.window_start, b.now - minutes * 60), b.now

    def _get_alert(self):
        a = self.backend.alert()["alerts"][0]
        raw = {"alertname": a["labels"]["alertname"], "service": a["labels"]["service"],
               "summary": a["annotations"]["summary"], "startsAt": a["startsAt"],
               "fired_at": hhmmss(self.backend.alert_at), "investigation_now": hhmmss(self.backend.now)}
        s = (f"Alert {raw['alertname']} on {raw['service']} fired at {raw['fired_at']} UTC: {raw['summary']}. "
             f"Now is {raw['investigation_now']} UTC; data before {hhmmss(self.backend.window_start)} is unavailable.")
        return raw, s, self.backend.alert_at, self.backend.alert_at

    def _get_service_graph(self):
        g = self.backend.service_graph()
        callers = {c: s for s, cs in g["calls"].items() for c in cs}
        lines = [f"{s} calls {', '.join(cs) if cs else 'no other service'}; depends on "
                 f"{g['dependencies'].get(s, 'nothing external')}; called by {callers.get(s, 'users directly')}"
                 for s, cs in g["calls"].items()]
        return g, "\n".join(lines), self.backend.window_start, self.backend.now

    def _scan_anomalies(self, minutes: float = 15):
        start, end = self._span(minutes)
        base_end = self.backend.window_start + BASELINE_S
        rows = []
        for s in SERVICES:
            for m in METRICS:
                pts = derived_series(self.backend, s, m, start, end)
                if not pts:
                    continue
                d = departure(pts, base_end)
                if d.get("departed"):
                    rows.append({"service": s, "metric": m, "since": hhmmss(d["since"]), "since_ts": d["since"],
                                 "direction": d["direction"], "baseline": fmt(d["baseline"]), "now": fmt(d["now"])})
        rows.sort(key=lambda r: r["since_ts"])
        raw = {"window": [isot(start), isot(end)], "baseline_until": hhmmss(base_end), "departures": rows}
        if not rows:
            return raw, "No metric departed from its normal level.", start, end
        lines = [f"{r['since']} {r['service']} {r['metric']} went {r['direction']} "
                 f"(normal {r['baseline']}, now {r['now']})" for r in rows]
        return raw, "Departures from normal, earliest first:\n" + "\n".join(lines), start, end

    def _query_metrics(self, service: str, metric: str, minutes: float = 15):
        if service not in SERVICES:
            raise ValueError(f"unknown service {service}")
        if metric not in METRICS:
            raise ValueError(f"unknown metric {metric}; use one of {', '.join(METRICS)}")
        start, end = self._span(minutes)
        pts = derived_series(self.backend, service, metric, start, end)
        if not pts:
            raw = {"service": service, "metric": metric, "points": []}
            return raw, f"{service} has no {metric} metric.", start, end
        d = departure(pts, self.backend.window_start + BASELINE_S)
        expected = int((end - start) // 5)
        missing = max(0, expected - len(pts))
        stats = {"normal": fmt(d["baseline"]), "now": fmt(d["now"]), "max": fmt(d["max"]), "min": fmt(d["min"]),
                 "departed_at": hhmmss(d["since"]) if d.get("departed") else None,
                 "direction": d.get("direction"), "missing_samples": missing}
        unit = METRICS[metric][1]
        raw = {"service": service, "metric": metric, "unit": unit, "stats": stats,
               "points": [[isot(t), fmt(v)] for t, v in pts]}
        s = (f"{service} {metric} ({unit}): normal {stats['normal']}, now {stats['now']}, "
             f"max {stats['max']}, min {stats['min']}. ")
        s += (f"Departed from normal ({stats['direction']}) at {stats['departed_at']}." if stats["departed_at"]
              else "No sustained departure from normal.")
        if missing:
            s += f" {missing} samples missing (scrape gaps)."
        return raw, s, start, end

    def _search_logs(self, service: str, query: str = "", level: str = "WARN", minutes: float = 15, limit: int = 8):
        if service not in SERVICES:
            raise ValueError(f"unknown service {service}")
        start, end = self._span(minutes)
        rank = {"INFO": 0, "WARN": 1, "ERROR": 2}
        floor = 0 if level.upper() == "ALL" else rank.get(level.upper(), 1)
        lines = [l for l in self.backend.logs(service, start, end)
                 if rank.get(l["level"], 0) >= floor and query.lower() in l["message"].lower()]
        groups: dict[str, dict] = defaultdict(lambda: {"count": 0})
        for l in lines:
            key = re.sub(r"\d+", "#", l["message"])
            g = groups[key]
            g["count"] += 1
            g.setdefault("first_seen", l["ts"])
            g["last_seen"] = l["ts"]
            g.setdefault("example", l["message"])
            g["level"] = l["level"]
        top = sorted(groups.values(), key=lambda g: (-g["count"], g["first_seen"]))[:limit]
        for g in top:
            g["first"], g["last"] = g["first_seen"][11:19], g["last_seen"][11:19]
        raw = {"service": service, "query": query, "level": level, "matched_lines": len(lines),
               "patterns": top, "lines": lines[:200]}
        if not lines:
            return raw, f"No {level.upper()}+ log lines for {service}" + (f" matching '{query}'." if query else "."), start, end
        out = [f"{len(lines)} matching lines in {len(groups)} patterns for {service}:"]
        out += [f"- {g['count']}x {g['level']} first {g['first']} last {g['last']}: {g['example']}" for g in top]
        return raw, "\n".join(out), start, end

    def _list_deploys(self, service: str | None = None, hours: float = 72):
        end = self.backend.now
        start = end - hours * 3600
        rows = [d for d in self.backend.deploys(start, end) if service in (None, "", d["service"])]
        rows = [{**d, "time": d["at"][:19].replace("T", " ")} for d in rows]
        raw = {"service": service, "hours": hours, "deploys": rows}
        if not rows:
            return raw, "No releases in that period.", start, end
        out = [f"- {d['time']} {d['service']} release {d['id']} (commit {d['sha']}, by {d['author']}): {d['message']}"
               for d in rows]
        return raw, f"{len(rows)} releases, oldest first:\n" + "\n".join(out), start, end

    def _get_commit_diff(self, sha: str):
        c = self.backend.commit(sha)
        if c is None:
            raise ValueError(f"unknown commit {sha}")
        files = "\n".join(f"- {f['path']} (+{f['additions']} -{f['deletions']})" + (f"\n{f['diff']}" if f.get("diff") else "")
                          for f in c["files"])
        return c, f"Commit {sha} by {c['author']} on {c['service']}: {c['message']}\n{files}", \
            self.backend.window_start, self.backend.now

    def _search_runbooks(self, query: str, k: int = 3):
        hits = runbooks.search(query, k=k)
        raw = {"query": query, "hits": hits}
        if not hits:
            return raw, "No relevant runbook sections.", self.backend.window_start, self.backend.now
        out = [f"[{h['id']} score {h['score']}] {h['title']} / {h['section']}: {h['text'][:400]}" for h in hits]
        return raw, "\n".join(out), self.backend.window_start, self.backend.now
