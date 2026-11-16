"""Telemetry simulator v2: writes a scenario snapshot without running Docker.

Produces exactly the snapshot format in recorder/README.md, so agents and tools
cannot tell a simulated snapshot from a recorded one except via meta.json.
Deterministic: the same scenario seed always gives byte-identical files.

    python -m recorder.simulate --scenarios scenarios/generated --out snapshots

v2 adds the realism the EDA asked for (a simple "first abnormal service" rule
scored 99% on v1):
  * propagation delays per call edge, sometimes negative (the caller's timeouts
    register before the callee's own metrics move)
  * a "concurrent_anomaly" noise template: an unrelated service misbehaves
    shortly before or after the fault
  * partial faults that affect only some requests
  * scrape gaps (missing samples) and counter resets on restart
  * varied wording of log messages, plus routine warning noise everywhere

Signals are programmatic, never LLM-generated. Agent-visible files never
contain fault names.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scenarios.schema import Scenario

VERSION = "recorder.simulate v2"
STEP = 5                                   # seconds between samples
POST_ALERT = 300                           # seconds kept after the alert fires
SERVICES = ["gateway", "orders", "payments", "inventory", "notifications"]
CALLER = {"orders": "gateway", "payments": "orders", "inventory": "orders", "notifications": "orders"}
CALLEES = {"gateway": ["orders"], "orders": ["payments", "inventory", "notifications"],
           "payments": [], "inventory": [], "notifications": []}
DB_USERS = {"orders", "inventory", "payments"}
DEPENDENCY = {"orders": "postgres", "inventory": "postgres", "notifications": "redis", "payments": "bank"}
DEP_PORT = {"postgres": 5432, "redis": 6379, "bank": 443}
INTENSITY = {"low": 0.6, "medium": 1.0, "high": 1.6}
MEM_LIMIT = 512 * 2**20
AUTHORS = ["asha.k", "dev.patel", "m.chen", "r.iyer", "s.okafor"]
ROUTES = {"gateway": ["/checkout", "/cart", "/products"], "orders": ["/orders", "/orders/{id}"],
          "payments": ["/charge", "/refund"], "inventory": ["/stock", "/reserve"],
          "notifications": ["/email", "/sms"]}
PROPAGATION_ORDER = ["payments", "inventory", "notifications", "orders"]   # leaves first

RAMP = {"memory_leak": 300, "slow_query": 90, "bad_config_deploy": 20, "buggy_code_deploy": 20,
        "dependency_down": 10, "pool_exhausted": 150, "cpu_saturation": 120, "cascading_failure": 180,
        "expired_credential": 10, "disk_full": 240}

# Message variants. Each keeps the stable token an engineer would search for
# (the expected-evidence signature) but the surrounding wording varies.
MESSAGES = {
    "oom": ["container {svc} restarted: reason=OOMKilled exit_code=137",
            "pod {svc}-7d9f restarted (OOMKilled), restart count {n}",
            "process exited after OOMKilled signal; supervisor restarting {svc}"],
    "gc": ["gc pause {ms}ms heap_used={mb}MB", "long GC pause: {ms} ms (heap {mb} MB)",
           "memory pressure: heap at {mb}MB, pause {ms}ms"],
    "slowq": ["slow query: SELECT * FROM stock WHERE sku = $1 took {ms}ms",
              "slow query detected ({ms} ms): UPDATE stock SET reserved = reserved + $1",
              "db: slow query {ms}ms on table stock (seq scan)"],
    "timeout": ["timeout after 50ms calling {dep}", "request to {dep} failed: timeout after 50ms",
                "{dep} client: timeout after 50ms (attempt 1/1)"],
    "traceback": ["Traceback (most recent call last): File \"app/handlers/checkout.py\", line 88, in apply_discount KeyError: 'discount_code'",
                  "Unhandled exception in POST /checkout: Traceback (most recent call last) ... KeyError: 'discount_code'",
                  "Traceback (most recent call last): File \"app/pricing.py\", line 41, in total TypeError: unsupported operand type(s)"],
    "refused": ["{dep} error: Error 111 connecting to {dep}:{port}. Connection refused.",
                "could not connect to {dep}:{port}: Connection refused",
                "{dep} unavailable (Connection refused), falling back to retry queue"],
    "pool": ["pool timeout: QueuePool limit of size 20 overflow 0 reached, connection timed out after 30s",
             "db pool timeout: all 20 connections busy, waited 30s",
             "could not acquire connection: pool timeout after 30000ms (size=20)"],
    "loop": ["event loop blocked for {ms}ms", "worker heartbeat delayed {ms}ms (CPU starved)",
             "request handler stalled {ms}ms waiting for CPU"],
    "queue": ["request queue depth {n} exceeds limit 200", "backlog growing: {n} requests queued",
              "shedding load: queue depth {n}"],
    "unauth": ["bank API returned 401 Unauthorized: token expired",
               "charge failed: 401 Unauthorized from bank gateway (expired token)",
               "upstream bank responded 401 Unauthorized; credential rejected"],
    "disk": ["OSError: [Errno 28] No space left on device: /var/lib/{svc}/queue.db",
             "write failed: No space left on device (/var/lib/{svc})",
             "could not persist message: [Errno 28] No space left on device"],
    "upstream": ["upstream timeout calling {callee} after 5000ms", "{callee}: upstream timeout (5s)",
                 "gave up on {callee} after upstream timeout"],
    "retry": ["call to {callee} failed with 503, retrying (attempt 2/3)", "{callee} returned 503; retrying",
              "retrying {callee} request after 5xx"],
}
ROUTINE_WARN = ["slow request to /health took {ms}ms", "cache miss ratio above 20% for 1m",
                "config reload skipped: no changes", "deprecated header X-Client-Id seen",
                "GC pause {ms}ms", "connection reset by peer while reading response (recovered)"]
BACKGROUND_ERR = ["cache refresh failed: upstream returned 503 (will retry)",
                  "retrying request after transient error: read ECONNRESET",
                  "metrics exporter push failed: context deadline exceeded"]


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"       # millisecond precision


@dataclass
class Sample:
    rps: float
    err: float
    p95: float
    mem: float
    cpu: float
    pool: float = 0.0
    dbq: float = 0.0
    dep_up: float = 1.0
    disk_free: float = 8e9
    restarted: bool = False
    logs: list[tuple[str, str]] = field(default_factory=list)  # (level, message)


class Sim:
    def __init__(self, sc: Scenario):
        self.sc = sc
        self.tgt = getattr(sc.target, "value", sc.target)              # plain strings, never enum reprs
        self.alert_svc = getattr(sc.alert_service, "value", sc.alert_service)
        self.rng = random.Random(sc.seed)
        r = self.rng
        n = int(sc.id.split("-")[1])
        self.t0 = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=7 * n, minutes=r.randint(0, 59))
        self.k = INTENSITY[sc.intensity]
        self.fault_at = self.t0 + timedelta(seconds=sc.onset_offset_s)
        self.ramp = RAMP[sc.fault] / self.k
        # propagation lag per call edge in seconds; ~30% negative (caller notices first)
        self.lag = {e: (r.uniform(-10, 0) if r.random() < 0.3 else r.uniform(0, 25)) for e in CALLER}
        path_lag, node = 0.0, self.tgt
        while node != self.alert_svc and node in CALLER:
            path_lag += max(0.0, self.lag[node])
            node = CALLER[node]
        self.alert_at = self.fault_at + timedelta(seconds=round(self.ramp * 0.8 + 30 + path_lag))
        self.end = self.alert_at + timedelta(seconds=POST_ALERT)
        self.times = [self.t0 + timedelta(seconds=s) for s in
                      range(0, int((self.end - self.t0).total_seconds()) + 1, STEP)]
        self.base = {s: dict(rps=r.uniform(*rr), p95=r.uniform(*p), mem=r.uniform(150, 260) * 2**20,
                             cpu=r.uniform(0.15, 0.35), pool=r.uniform(3, 6), dbq=r.uniform(0.01, 0.025))
                     for s, rr, p in [("gateway", (45, 60), (0.10, 0.14)), ("orders", (35, 45), (0.07, 0.10)),
                                      ("payments", (18, 24), (0.12, 0.17)), ("inventory", (30, 40), (0.05, 0.07)),
                                      ("notifications", (12, 18), (0.04, 0.06))]}
        self.walk = {s: 0.0 for s in SERVICES}
        self.oom_count = 0
        gt = sc.ground_truth
        dep_ev = next((e for e in gt.expected_evidence if e.startswith("deploy:")), None)
        self.fault_deploy_id = dep_ev.split(":", 1)[1] if dep_ev else f"d-{r.randrange(16**3):03x}"
        self.variant = {k: r.randrange(len(v)) for k, v in MESSAGES.items()}
        # v2 realism ----------------------------------------------------------
        self.partial = r.random() < 0.3 and sc.intensity != "low" and sc.fault not in ("memory_leak", "disk_full")
        self.noise_service = r.choice([s for s in SERVICES if s not in (self.tgt, CALLER.get(self.tgt))])
        self.anomaly = self._pick_anomaly() if sc.noise == "concurrent_anomaly" else None
        self.gaps = self._pick_gaps()

    # ------------------------------------------------------------ helpers
    def msg(self, kind: str, **kw) -> str:
        return MESSAGES[kind][self.variant[kind]].format(**kw)

    def _ancestors(self, s: str) -> set[str]:
        out = set()
        while s in CALLER:
            s = CALLER[s]
            out.add(s)
        return out

    def _pick_anomaly(self) -> dict:
        """An unrelated service misbehaves near the fault time. It sits off the path from the
        faulty service to the alerting service, and is small enough not to trip the real alert early."""
        tgt = self.tgt
        banned = {tgt, self.alert_svc} | self._ancestors(tgt)
        cands = [s for s in SERVICES if s not in banned] or [s for s in SERVICES if s != tgt and s != "gateway"]
        svc = self.rng.choice(cands)
        kind = self.rng.choice(["errors", "latency", "cpu"])
        start = self.fault_at + timedelta(seconds=self.rng.uniform(-60, 40))
        return {"service": svc, "kind": kind, "start": start}

    def _pick_gaps(self) -> list[tuple[str, int, int]]:
        """Scrape gaps: (service, first index, length) of missing metric samples."""
        gaps = []
        if self.rng.random() < 0.35:
            for _ in range(self.rng.randint(1, 2)):
                svc = self.rng.choice(SERVICES)
                start = self.rng.randrange(5, max(6, len(self.times) - 10))
                gaps.append((svc, start, self.rng.randint(2, 7)))
        return gaps

    def progress(self, t: datetime) -> float:
        dt = (t - self.fault_at).total_seconds()
        return 0.0 if dt < 0 else min(1.0, dt / self.ramp)

    def jitter(self, x: float, rel: float = 0.06) -> float:
        return max(0.0, x * (1 + self.rng.gauss(0, rel)))

    # ------------------------------------------------------------ own behaviour (no propagation)
    def own(self, t: datetime) -> dict[str, Sample]:
        out = {}
        for s in SERVICES:
            b = self.base[s]
            self.walk[s] = 0.9 * self.walk[s] + self.rng.gauss(0, 0.02)
            out[s] = Sample(rps=self.jitter(b["rps"] * (1 + self.walk[s])), err=max(0, self.rng.gauss(0.002, 0.001)),
                            p95=self.jitter(b["p95"], 0.08), mem=self.jitter(b["mem"], 0.02),
                            cpu=min(1, self.jitter(b["cpu"], 0.1)),
                            pool=self.jitter(b["pool"], 0.15) if s in DB_USERS else 0.0,
                            dbq=self.jitter(b["dbq"], 0.1) if s in DB_USERS else 0.0,
                            disk_free=8e9 - (t - self.t0).total_seconds() * 1e4 if s == "notifications" else 2e10)
            if self.rng.random() < 0.02:                       # routine noise everywhere
                out[s].logs.append(("WARN", self.rng.choice(ROUTINE_WARN).format(ms=self.rng.randint(300, 900))))
        self.apply_noise(t, out)
        self.apply_fault(t, out)
        return out

    def apply_noise(self, t: datetime, out: dict[str, Sample]) -> None:
        if self.sc.noise == "background_errors":
            s = out[self.noise_service]
            s.err += 0.03
            if self.rng.random() < 0.5:
                s.logs.append(("ERROR", self.rng.choice(BACKGROUND_ERR)))
        if self.anomaly and t >= self.anomaly["start"]:
            a, s = self.anomaly, out[self.anomaly["service"]]
            if a["kind"] == "errors":
                s.err += 0.035
                if self.rng.random() < 0.5:
                    s.logs.append(("ERROR", self.rng.choice(BACKGROUND_ERR)))
            elif a["kind"] == "latency":
                s.p95 += 0.45
                if self.rng.random() < 0.3:
                    s.logs.append(("WARN", f"slow response from {DEPENDENCY.get(a['service'], 'upstream')}: "
                                           f"{self.rng.randint(400, 700)}ms"))
            else:
                s.cpu = min(0.95, s.cpu + 0.45)
                if self.rng.random() < 0.2:
                    s.logs.append(("WARN", "background job compaction running; CPU above 80%"))

    def apply_fault(self, t: datetime, out: dict[str, Sample]) -> None:
        p = self.progress(t)
        if p <= 0:
            return
        f, s, k, r = self.sc.fault, out[self.tgt], self.k, self.rng
        tgt = self.tgt
        share = 0.55 if self.partial else 1.0                  # partial faults hit only some requests
        if f == "memory_leak":
            elapsed = (t - self.fault_at).total_seconds()
            cycle = self.ramp * 1.15
            phase = (elapsed % cycle) / cycle
            if int(elapsed // cycle) > self.oom_count:
                self.oom_count += 1
                s.logs.append(("ERROR", self.msg("oom", svc=tgt, n=self.oom_count)))
                s.err += 0.25
                s.restarted = True
            s.mem = self.base[tgt]["mem"] + (MEM_LIMIT * 0.98 - self.base[tgt]["mem"]) * min(1, phase / 0.87)
            s.p95 *= 1 + 1.5 * phase
            if phase > 0.6 and r.random() < 0.3:
                s.logs.append(("WARN", self.msg("gc", ms=int(300 + 900 * phase), mb=int(s.mem / 2**20))))
        elif f == "slow_query":
            s.dbq = self.base[tgt]["dbq"] + (1.2 + 0.6 * k) * p
            s.p95 += s.dbq * (0.75 if self.partial else 1.0)
            if r.random() < 0.6 * p * share:
                s.logs.append(("WARN", self.msg("slowq", ms=int(s.dbq * 1000))))
        elif f == "bad_config_deploy":
            s.err += min(0.6, 0.35 * k) * p * share
            dep = CALLEES[tgt][0] if CALLEES[tgt] else DEPENDENCY[tgt]
            if r.random() < 0.8 * p * share:
                s.logs.append(("ERROR", self.msg("timeout", dep=dep)))
        elif f == "buggy_code_deploy":
            s.err += min(0.5, 0.25 * k) * p * share
            if r.random() < 0.7 * p * share:
                s.logs.append(("ERROR", self.msg("traceback")))
        elif f == "dependency_down":
            dep = DEPENDENCY[tgt]
            s.dep_up = 0.0
            s.err += min(0.7, 0.45 * k) * p
            if r.random() < 0.8:
                s.logs.append(("ERROR", self.msg("refused", dep=dep, port=DEP_PORT[dep])))
        elif f == "pool_exhausted":
            s.pool = self.base[tgt]["pool"] + (20.0 - self.base[tgt]["pool"]) * p
            if p >= 0.95:
                s.p95 += 1.5 + 1.5 * k
                s.err += 0.08 * k
                if r.random() < 0.7:
                    s.logs.append(("ERROR", self.msg("pool")))
        elif f == "cpu_saturation":
            s.cpu = min(0.99, self.base[tgt]["cpu"] + 0.75 * p * k)
            s.p95 += (1.2 + 0.6 * k) * p
            if p > 0.5 and r.random() < 0.15:
                s.logs.append(("WARN", self.msg("loop", ms=int(400 + 600 * p))))
        elif f == "cascading_failure":
            s.cpu = min(0.99, self.base[tgt]["cpu"] + 0.6 * p)
            s.p95 += 4.5 * p * k
            if r.random() < 0.4 * p:
                s.logs.append(("WARN", self.msg("queue", n=int(100 + 400 * p))))
        elif f == "expired_credential":
            s.err += min(0.8, 0.5 * k) * p * share
            if r.random() < 0.8 * share:
                s.logs.append(("ERROR", self.msg("unauth")))
        elif f == "disk_full":
            s.disk_free = max(0.0, 8e9 * (1 - p))
            if p >= 1:
                s.err += 0.4 * k
                if r.random() < 0.8:
                    s.logs.append(("ERROR", self.msg("disk", svc=tgt)))

    # ------------------------------------------------------------ propagation with per-edge lag
    def propagate(self, series: list[dict[str, Sample]]) -> None:
        n = len(series)
        for callee in PROPAGATION_ORDER:
            caller = CALLER[callee]
            shift = int(round(self.lag[callee] / STEP))
            weight = 0.9 if self.sc.noise == "downstream_alert" or self.sc.fault == "cascading_failure" else 0.55
            base_lat = self.base[callee]["p95"] * 1.3
            extras = [(max(0.0, x[callee].err - 0.01), max(0.0, x[callee].p95 - base_lat)) for x in series]
            for i in range(n):
                j = i - shift
                if j < 0 or j >= n:
                    continue
                extra_err, extra_lat = extras[j]
                up = series[i][caller]
                up.err += weight * extra_err
                if extra_lat > 1.5:                   # caller timeouts turn into its own errors
                    up.err += 0.25 * min(1.0, extra_lat / 4)
                up.p95 += 0.9 * extra_lat
                if extra_lat > 1.5 and self.rng.random() < 0.5:
                    up.logs.append(("ERROR", self.msg("upstream", callee=callee)))
                elif extra_err > 0.05 and self.rng.random() < 0.4:
                    up.logs.append(("WARN", self.msg("retry", callee=callee)))

    # ------------------------------------------------------------ files
    def run(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        series_by_t = [self.own(t) for t in self.times]
        self.propagate(series_by_t)

        metrics: dict[str, dict[tuple, list]] = {}
        logs: dict[str, list[dict]] = {s: [] for s in SERVICES}
        cum = {s: {"2xx": 0.0, "5xx": 0.0, "cpu": 0.0} for s in SERVICES}
        missing = {(s, i) for s, start, ln in self.gaps for i in range(start, start + ln)}

        def add(metric: str, labels: dict, i: int, t: datetime, v: float) -> None:
            if (labels["service"], i) in missing:
                return
            key = tuple(sorted(labels.items()))
            metrics.setdefault(metric, {}).setdefault(key, []).append([int(t.timestamp()), f"{v:.6g}"])

        for i, (t, snap) in enumerate(zip(self.times, series_by_t)):
            for s, x in snap.items():
                x.err = min(0.95, x.err)
                if x.restarted:                                  # counters reset on process restart
                    cum[s] = {"2xx": 0.0, "5xx": 0.0, "cpu": 0.0}
                cum[s]["2xx"] += x.rps * STEP * (1 - x.err)
                cum[s]["5xx"] += x.rps * STEP * x.err
                cum[s]["cpu"] += x.cpu * STEP
                add("http_requests_total", {"service": s, "status": "2xx"}, i, t, cum[s]["2xx"])
                add("http_requests_total", {"service": s, "status": "5xx"}, i, t, cum[s]["5xx"])
                add("http_request_duration_seconds", {"service": s, "quantile": "0.95"}, i, t, x.p95)
                add("container_memory_bytes", {"service": s}, i, t, x.mem)
                add("process_cpu_seconds_total", {"service": s}, i, t, cum[s]["cpu"])
                if s in DB_USERS:
                    add("db_pool_in_use", {"service": s}, i, t, x.pool)
                    add("db_query_seconds", {"service": s, "quantile": "0.95"}, i, t, x.dbq)
                if s in DEPENDENCY:
                    add("dependency_up", {"service": s, "dependency": DEPENDENCY[s]}, i, t, x.dep_up)
                if s == "notifications":
                    add("disk_free_bytes", {"service": s, "mount": "/var/lib"}, i, t, x.disk_free)
                route = self.rng.choice(ROUTES[s])
                status = 500 if self.rng.random() < x.err else 200
                logs[s].append({"ts": iso(t), "service": s, "level": "INFO" if status == 200 else "ERROR",
                                "message": f"POST {route} {status} {int(x.p95 * 1000 * self.rng.uniform(0.4, 1.0))}ms",
                                "trace_id": f"{self.rng.getrandbits(64):016x}"})
                for j, (lvl, m) in enumerate(x.logs):
                    logs[s].append({"ts": iso(t + timedelta(milliseconds=150 * (j + 1))), "service": s,
                                    "level": lvl, "message": m, "trace_id": f"{self.rng.getrandbits(64):016x}"})

        (out_dir / "metrics").mkdir(exist_ok=True)
        for metric, by_labels in metrics.items():
            result = [{"metric": {"__name__": metric, **dict(k)}, "values": v} for k, v in by_labels.items()]
            self._write(out_dir / "metrics" / f"{metric}.json", {"resultType": "matrix", "result": result})
        (out_dir / "logs").mkdir(exist_ok=True)
        for s, lines in logs.items():
            lines.sort(key=lambda d: d["ts"])
            (out_dir / "logs" / f"{s}.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))

        deploys, commits = self.deploys()
        self._write(out_dir / "deploys.json", deploys)
        (out_dir / "commits").mkdir(exist_ok=True)
        for c in commits:
            self._write(out_dir / "commits" / f"{c['sha']}.json", c)
        self._write(out_dir / "service_graph.json", {"calls": CALLEES, "dependencies": DEPENDENCY})
        self._write(out_dir / "window.json", {"baseline_start": iso(self.t0), "alert_at": iso(self.alert_at),
                                              "end": iso(self.end)})
        self._write(out_dir / "alert.json", self.alert())
        self._write(out_dir / "meta.json", {"source": "simulated", "simulator": VERSION, "step_seconds": STEP})
        # ground truth: harness and scorer only; tools must never read these files
        (out_dir / "scenario.yaml").write_text(yaml.safe_dump(self.sc.model_dump(mode="json"), sort_keys=False))
        self._write(out_dir / "fault_window.json", {
            "fault_at": iso(self.fault_at), "partial": self.partial,
            "lags_s": {k: round(v, 1) for k, v in self.lag.items()},
            "anomaly": None if not self.anomaly else {**self.anomaly, "start": iso(self.anomaly["start"])},
            "gaps": self.gaps})
        self._sums(out_dir)

    def deploys(self) -> tuple[list[dict], list[dict]]:
        d, commits = [], []

        def deploy(svc: str, at: datetime, dep_id: str, msg: str, files: list[dict]) -> None:
            sha = f"{self.rng.getrandbits(40):010x}"
            author = self.rng.choice(AUTHORS)
            d.append({"id": dep_id, "service": svc, "sha": sha, "author": author, "at": iso(at), "message": msg})
            commits.append({"sha": sha, "service": svc, "author": author, "message": msg, "files": files})

        routine = [("chore: bump dependencies", "requirements.txt", 6, 6),
                   ("feat: add request id to logs", "app/logging.py", 14, 3),
                   ("fix: handle empty cart", "app/handlers/cart.py", 9, 2),
                   ("refactor: tidy settings loader", "app/settings.py", 22, 18)]
        for s in SERVICES:                        # history before the window: rollback targets exist
            for j in range(2):
                m, path, a, rm = self.rng.choice(routine)
                deploy(s, self.t0 - timedelta(days=3 - j, hours=self.rng.randint(1, 8)),
                       f"d-{self.rng.randrange(16**3):03x}", m, [{"path": path, "additions": a, "deletions": rm}])
        f, tgt = self.sc.fault, self.tgt
        at = self.fault_at - timedelta(seconds=5)
        if f == "bad_config_deploy":
            dep = CALLEES[tgt][0] if CALLEES[tgt] else DEPENDENCY[tgt]
            deploy(tgt, at, self.fault_deploy_id, f"config: tune outbound timeouts for {dep}",
                   [{"path": "config/settings.yaml", "additions": 1, "deletions": 1,
                     "diff": f"-  {dep}_timeout_ms: 2000\n+  {dep}_timeout_ms: 50"}])
        elif f == "buggy_code_deploy":
            deploy(tgt, at, self.fault_deploy_id, "feat: apply discount codes at checkout",
                   [{"path": "app/handlers/checkout.py", "additions": 31, "deletions": 4,
                     "diff": "+    code = request.json['discount_code']"}])
        elif f == "expired_credential":
            deploy(tgt, at, self.fault_deploy_id, "ops: rotate bank API credentials",
                   [{"path": "config/secrets.ref.yaml", "additions": 1, "deletions": 1,
                     "diff": "-  bank_token_ref: vault://bank/token/v7\n+  bank_token_ref: vault://bank/token/v6"}])
        if self.sc.noise == "unrelated_deploy":
            m, path, a, rm = self.rng.choice(routine)
            deploy(self.noise_service, self.fault_at - timedelta(seconds=self.rng.randint(30, 150)),
                   f"d-{self.rng.randrange(16**3):03x}", m, [{"path": path, "additions": a, "deletions": rm}])
        d.sort(key=lambda x: x["at"])
        return d, commits

    def alert(self) -> dict:
        svc = self.alert_svc
        summary = {"HighErrorRate": f"{svc} 5xx ratio above 5% for 30s",
                   "HighLatency": f"{svc} p95 latency above 1s for 30s",
                   "HighMemory": f"{svc} memory above 85% of limit",
                   "ServiceUnhealthy": f"{svc} health check failing"}[self.sc.alert_name]
        return {"receiver": "investigator", "status": "firing",
                "alerts": [{"status": "firing", "labels": {"alertname": self.sc.alert_name, "service": svc,
                                                           "severity": "critical"},
                            "annotations": {"summary": summary}, "startsAt": iso(self.alert_at)}]}

    @staticmethod
    def _write(path: Path, obj) -> None:
        path.write_text(json.dumps(obj, indent=None, separators=(",", ":"), default=str))

    @staticmethod
    def _sums(out_dir: Path) -> None:
        lines = []
        for p in sorted(out_dir.rglob("*")):
            if p.is_file() and p.name != "SHA256SUMS":
                lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out_dir)}")
        (out_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def simulate_all(scenario_dir: Path, out: Path) -> int:
    n = 0
    for path in sorted(scenario_dir.rglob("sc-*.yaml")):
        sc = Scenario(**yaml.safe_load(path.read_text()))
        Sim(sc).run(out / sc.split / sc.id)
        n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=Path, default=Path("scenarios/generated"))
    ap.add_argument("--out", type=Path, default=Path("snapshots"))
    a = ap.parse_args()
    print(f"simulated {simulate_all(a.scenarios, a.out)} snapshots into {a.out}")
