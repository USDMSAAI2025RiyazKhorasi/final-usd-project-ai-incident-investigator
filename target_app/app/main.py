"""The live target app: one FastAPI image run as five services (plus a mock bank) via SERVICE_NAME.

gateway -> orders -> payments (-> bank), inventory, notifications (-> redis)
orders, payments, inventory use Postgres through a pool of 20 connections.

Metrics carry no `service` label; Prometheus adds it from the scrape target, so the
recorded series match the snapshot format exactly. Logs are JSON lines on stdout.
"""
from __future__ import annotations

import collections
import json
import os
import random
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from target_app.app.faults import FAULTS, FaultState

SERVICE = os.environ.get("SERVICE_NAME", "gateway")
CALLS = {"gateway": ["orders"], "orders": ["payments", "inventory", "notifications"],
         "payments": [], "inventory": [], "notifications": [], "mockbank": []}
DEPENDS = {"orders": "postgres", "inventory": "postgres", "payments": "bank", "notifications": "redis"}
PORT = {"gateway": 8000, "orders": 8001, "payments": 8002, "inventory": 8003, "notifications": 8004, "mockbank": 8005}
DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/postgres")
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
BANK_URL = os.environ.get("BANK_URL", "http://mockbank:8005")
DEPLOY_DIR = Path(os.environ.get("DEPLOY_DIR", "/deploys"))
NORMAL_TIMEOUT_S = 2.0
POOL_SIZE = 20

app = FastAPI(title=SERVICE)
faults = FaultState(SERVICE)

REQS = Counter("http_requests", "Requests by status class", ["status"])
LAT = Gauge("http_request_duration_seconds", "Rolling p95 latency", ["quantile"])
MEM = Gauge("container_memory_bytes", "Resident memory of the service process")
POOL = Gauge("db_pool_in_use", "Busy database connections")
DBQ = Gauge("db_query_seconds", "Rolling p95 database query time", ["quantile"])
DEP = Gauge("dependency_up", "Dependency reachable", ["dependency"])
DISK = Gauge("disk_free_bytes", "Free disk space", ["mount"])
_lat, _dbq = collections.deque(maxlen=2000), collections.deque(maxlen=2000)


def log(level: str, message: str) -> None:
    sys.stdout.write(json.dumps({"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                                 "service": SERVICE, "level": level, "message": message,
                                 "trace_id": uuid.uuid4().hex[:16]}) + "\n")
    sys.stdout.flush()


def p95(dq: collections.deque, window: float = 30) -> float:
    now = time.time()
    vals = sorted(d for t, d in dq if now - t <= window)
    return vals[int(0.95 * (len(vals) - 1))] if vals else 0.0


# ------------------------------------------------------------------ database pool
class Pool:
    """A pool of POOL_SIZE Postgres connections; falls back to simulated queries without a database."""

    def __init__(self):
        self.sem = threading.BoundedSemaphore(POOL_SIZE)
        self.in_use = 0
        self.lock = threading.Lock()
        self._held: list = []
        try:
            import psycopg
            self.psycopg = psycopg
        except ImportError:
            self.psycopg = None

    def query(self, sql: str, args: tuple = (), timeout: float = 30.0) -> float:
        if not self.sem.acquire(timeout=timeout if not faults.is_on("pool_exhausted") else 2.0):
            raise TimeoutError(f"pool timeout: QueuePool limit of size {POOL_SIZE} overflow 0 reached, "
                               f"connection timed out after {int(timeout)}s")
        with self.lock:
            self.in_use += 1
        start = time.time()
        try:
            if self.psycopg:
                with self.psycopg.connect(DB_URL, connect_timeout=3) as conn:
                    conn.execute(sql, args)
            else:
                time.sleep(0.01)
            return time.time() - start
        finally:
            with self.lock:
                self.in_use -= 1
            self.sem.release()
            _dbq.append((time.time(), time.time() - start))

    def hold_all(self) -> None:
        """Fault: leak connections until the pool is exhausted."""
        def hog():
            if self.sem.acquire(timeout=60):
                with self.lock:
                    self.in_use += 1
                while faults.is_on("pool_exhausted"):
                    time.sleep(0.5)
                with self.lock:
                    self.in_use -= 1
                self.sem.release()
        for _ in range(POOL_SIZE):
            threading.Thread(target=hog, daemon=True).start()
            time.sleep(4.0 / faults.intensity("pool_exhausted"))


pool = Pool()


# ------------------------------------------------------------------ background: gauges and health
def _rss() -> float:
    try:
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _ping(dep: str) -> bool:
    try:
        if dep == "postgres":
            pool.query("SELECT 1", timeout=2)
        elif dep == "redis":
            import redis
            redis.Redis(host=REDIS_HOST, socket_timeout=1).ping()
        elif dep == "bank":
            httpx.get(f"{BANK_URL}/health", timeout=1)
        return True
    except Exception:
        return False


def _ticker() -> None:
    while True:
        LAT.labels("0.95").set(p95(_lat))
        MEM.set(_rss())
        POOL.set(pool.in_use)
        DBQ.labels("0.95").set(p95(_dbq))
        if SERVICE in DEPENDS:
            DEP.labels(DEPENDS[SERVICE]).set(1.0 if _ping(DEPENDS[SERVICE]) else 0.0)
        if SERVICE == "notifications":
            faults.data_dir.mkdir(parents=True, exist_ok=True)
            st = os.statvfs(faults.data_dir)
            DISK.labels("/var/lib").set(st.f_bavail * st.f_frsize)
        if faults.is_on("memory_leak") and _rss() > 300 * 2**20 and random.random() < 0.3:
            log("WARN", f"memory pressure: heap at {int(_rss() / 2**20)}MB, pause {random.randint(300, 900)}ms")
        time.sleep(5)


threading.Thread(target=_ticker, daemon=True).start()
log("INFO", f"{SERVICE} started")


# ------------------------------------------------------------------ request handling
@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path in ("/metrics", "/health") or request.url.path.startswith("/admin"):
        return await call_next(request)
    start = time.time()
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as e:                                       # unhandled: log the traceback like a real app
        tb = traceback.format_exc().strip().splitlines()
        log("ERROR", f"Traceback (most recent call last): {tb[-2].strip() if len(tb) > 1 else ''} {tb[-1]}")
        response, status = JSONResponse({"error": str(e)}, status_code=500), 500
    dur = time.time() - start
    _lat.append((time.time(), dur))
    REQS.labels("5xx" if status >= 500 else "2xx").inc()
    log("INFO" if status < 500 else "ERROR", f"{request.method} {request.url.path} {status} {int(dur * 1000)}ms")
    return response


def call(callee: str, path: str) -> None:
    timeout = 0.05 if faults.is_on("bad_config_deploy") else NORMAL_TIMEOUT_S
    try:
        r = httpx.post(f"http://{callee}:{PORT[callee]}{path}", timeout=timeout)
    except httpx.TimeoutException:
        if faults.is_on("bad_config_deploy"):
            log("ERROR", f"timeout after 50ms calling {callee}")
        else:
            log("ERROR", f"upstream timeout calling {callee} after {int(timeout * 1000)}ms")
        raise HTTPException(502, f"{callee} timed out")
    except httpx.HTTPError as e:
        log("ERROR", f"call to {callee} failed: {e}")
        raise HTTPException(502, f"{callee} failed")
    if r.status_code >= 500:
        log("WARN", f"call to {callee} failed with {r.status_code}, retrying (attempt 2/3)")
        raise HTTPException(502, f"{callee} returned {r.status_code}")


def work() -> None:
    """Service-specific work for one request; faults change what happens here."""
    faults.on_request()
    if faults.is_on("background_errors") and random.random() < 0.03:
        log("ERROR", "cache refresh failed: upstream returned 503 (will retry)")
        raise HTTPException(500, "background error")
    if SERVICE in ("orders", "payments", "inventory"):
        try:
            if SERVICE == "inventory" and faults.is_on("slow_query"):
                d = pool.query("SELECT pg_sleep(%s)", ((1.2 + 0.6 * faults.intensity("slow_query")),))
                log("WARN", f"slow query: SELECT * FROM stock WHERE sku = $1 took {int(d * 1000)}ms")
            else:
                pool.query("SELECT 1")
        except TimeoutError as e:
            log("ERROR", str(e))
            raise HTTPException(503, "database pool exhausted")
        except Exception as e:
            log("ERROR", f"postgres error: {e}")
            raise HTTPException(503, "database unavailable")
    if SERVICE == "gateway" and faults.is_on("buggy_code_deploy") and random.random() < 0.5:
        payload: dict = {}
        payload["discount_code"]                                # the bug: KeyError on the checkout path
    if SERVICE == "payments":
        token = "v6" if faults.is_on("expired_credential") else "v7"
        try:
            r = httpx.post(f"{BANK_URL}/charge", headers={"authorization": f"Bearer {token}"}, timeout=2)
        except httpx.HTTPError as e:
            log("ERROR", f"bank unavailable: {e}")
            raise HTTPException(502, "bank unavailable")
        if r.status_code == 401:
            log("ERROR", "bank API returned 401 Unauthorized: token expired")
            raise HTTPException(502, "bank rejected credentials")
    if SERVICE == "notifications":
        try:
            import redis
            redis.Redis(host=REDIS_HOST, socket_timeout=1).lpush("outbox", "msg")
        except Exception as e:
            log("ERROR", f"redis error: {e}")
            raise HTTPException(503, "queue unavailable")
        try:
            faults.data_dir.mkdir(parents=True, exist_ok=True)
            with open(faults.data_dir / "queue.db", "ab") as f:
                f.write(b"x" * 512)
        except OSError as e:
            log("ERROR", f"OSError: {e}")
            raise HTTPException(500, "disk error")
    for callee in CALLS.get(SERVICE, []):
        call(callee, "/api/work")


@app.post("/api/work")
@app.get("/api/work")
def api_work():
    work()
    return {"service": SERVICE, "ok": True}


# ------------------------------------------------------------------ mock bank
@app.post("/charge")
def charge(request: Request):
    if SERVICE != "mockbank":
        raise HTTPException(404)
    if request.headers.get("authorization") != "Bearer v7":
        raise HTTPException(401, "token expired")
    return {"charged": True}


# ------------------------------------------------------------------ ops endpoints
@app.get("/health")
def health():
    return {"service": SERVICE, "status": "ok", "faults": faults.active()}


@app.get("/metrics")
def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/admin/faults/{name}")
def set_fault(name: str, body: dict):
    if name not in FAULTS:
        raise HTTPException(404, f"unknown fault {name}")
    faults.set(name, enabled=body.get("enabled", True), params=body.get("params", {}))
    if name == "pool_exhausted" and body.get("enabled", True):
        threading.Thread(target=pool.hold_all, daemon=True).start()
    return {"fault": name, "enabled": faults.is_on(name)}


@app.post("/admin/reset")
def reset():
    faults.reset()
    return {"service": SERVICE, "faults": faults.active()}


def _record_release(message: str, files: list[dict]) -> dict:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    sha = uuid.uuid4().hex[:10]
    entry = {"id": f"d-{uuid.uuid4().hex[:3]}", "service": SERVICE, "sha": sha,
             "author": os.environ.get("RELEASE_AUTHOR", "ci-bot"),
             "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "message": message}
    with open(DEPLOY_DIR / "deploys.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")
    (DEPLOY_DIR / "commits").mkdir(exist_ok=True)
    (DEPLOY_DIR / "commits" / f"{sha}.json").write_text(json.dumps({"sha": sha, "service": SERVICE,
                                                                     "author": entry["author"], "message": message,
                                                                     "files": files}))
    return entry


@app.post("/admin/release")
def release(body: dict):
    return _record_release(body.get("message", "release"), body.get("files", []))


@app.post("/admin/rollback")
def rollback():
    undone = faults.rollback()
    entry = _record_release("rollback to previous release", [{"path": "-", "additions": 0, "deletions": 0}])
    log("INFO", f"rolled back release; undone: {undone}")
    return {"rolled_back": undone, "release": entry}
