"""Dataset checks for simulated snapshots (Jira AII-15)."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from tools.backend import ts as parse_ts

from recorder.simulate import Sim
from scenarios.generator import generate, load_catalogue

SCENARIOS = generate(590)
FAULT_NAMES = list(load_catalogue()["faults"])
AGENT_VISIBLE = ["metrics", "logs", "commits", "deploys.json", "alert.json", "service_graph.json", "window.json"]


def ts(s: str) -> float:
    return parse_ts(s)


def series(snap: Path, metric: str, **labels) -> list[tuple[int, float]]:
    data = json.loads((snap / "metrics" / f"{metric}.json").read_text())
    for r in data["result"]:
        if all(r["metric"].get(k) == v for k, v in labels.items()):
            return [(t, float(v)) for t, v in r["values"]]
    raise KeyError((metric, labels))


def at(points, t):
    return min(points, key=lambda p: abs(p[0] - t))[1]


def error_ratio(snap, svc, t, window=60):
    ok, bad = series(snap, "http_requests_total", service=svc, status="2xx"), \
        series(snap, "http_requests_total", service=svc, status="5xx")
    d_ok, d_bad = at(ok, t) - at(ok, t - window), at(bad, t) - at(bad, t - window)
    return d_bad / max(1.0, d_ok + d_bad)


def alert_condition(snap, name, svc, t):
    if name == "HighErrorRate":
        return error_ratio(snap, svc, t) > 0.05
    if name == "HighLatency":
        return at(series(snap, "http_request_duration_seconds", service=svc), t) > 1.0
    if name == "HighMemory":
        return at(series(snap, "container_memory_bytes", service=svc), t) > 0.85 * 512 * 2**20
    if name == "ServiceUnhealthy":
        return at(series(snap, "disk_free_bytes", service=svc), t) < 0.15 * 8e9
    raise ValueError(name)


@pytest.fixture(scope="module")
def snaps(tmp_path_factory):
    root = tmp_path_factory.mktemp("snaps")
    for sc in SCENARIOS:
        Sim(sc).run(root / sc.id)
    return root


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: f"{s.id}-{s.fault}-{s.noise}-{s.intensity}")
def test_alert_fires_on_alert_service_and_not_before_fault(snaps, sc):
    snap = snaps / sc.id
    w = json.loads((snap / "window.json").read_text())
    fault_at = ts(json.loads((snap / "fault_window.json").read_text())["fault_at"])
    alert_at = ts(w["alert_at"])
    assert alert_condition(snap, sc.alert_name, sc.alert_service, alert_at + 30), "alert condition not met"
    for t in range(int(ts(w["baseline_start"])) + 60, int(fault_at) - 10, 30):
        assert not alert_condition(snap, sc.alert_name, sc.alert_service, t), "false alarm before fault"


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_expected_evidence_present(snaps, sc):
    snap = snaps / sc.id
    logs = "".join((snap / "logs" / f).read_text() for f in sorted(p.name for p in (snap / "logs").iterdir()))
    deploys = (snap / "deploys.json").read_text()
    for sig in sc.ground_truth.expected_evidence:
        kind, _, needle = sig.partition(":")
        if kind == "log":
            assert needle in logs, sig
        elif kind == "deploy":
            assert needle in deploys, sig
        elif kind == "metric":
            assert (snap / "metrics" / f"{needle}.json").exists(), sig


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_no_fault_names_or_ids_in_agent_visible_files(snaps, sc):
    snap = snaps / sc.id
    pat = re.compile("|".join([r"sc-\d{3}", r"Service\.", r"CauseType\."] + FAULT_NAMES), re.I)
    for item in AGENT_VISIBLE:
        for p in ([snap / item] if (snap / item).is_file() else (snap / item).rglob("*")):
            assert not pat.search(p.read_text()), p


def test_deterministic(tmp_path):
    sc = SCENARIOS[0]
    Sim(sc).run(tmp_path / "a")
    Sim(sc).run(tmp_path / "b")
    assert (tmp_path / "a" / "SHA256SUMS").read_text() == (tmp_path / "b" / "SHA256SUMS").read_text()


def test_rollback_targets_exist(snaps):
    for sc in SCENARIOS:
        deploys = json.loads((snaps / sc.id / "deploys.json").read_text())
        assert sum(d["service"] == sc.target for d in deploys) >= 2
