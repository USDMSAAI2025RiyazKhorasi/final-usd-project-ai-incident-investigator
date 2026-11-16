import json
from pathlib import Path

import pytest

from tools.backend import SnapshotBackend, ts
from tools.base import RawStore
from tools.runbooks import search
from tools.toolkit import ALL_TOOLS, Toolkit


def kit(snap: Path, tmp_path: Path) -> Toolkit:
    return Toolkit(SnapshotBackend(snap), RawStore(tmp_path / "raw"))


def first(snapshot_dir):
    return sorted(snapshot_dir.glob("*/sc-*"))[0]


def test_answer_files_are_invisible(snapshot_dir):
    b = SnapshotBackend(first(snapshot_dir))
    for name in ("scenario.yaml", "fault_window.json"):
        with pytest.raises(PermissionError):
            b._json(name)


def test_tools_never_see_data_after_now(snapshot_dir):
    b = SnapshotBackend(first(snapshot_dir))
    pts = b.metric("container_memory_bytes", {"service": "orders"}, 0, 1e12)
    assert pts and max(t for t, _ in pts) <= b.now
    logs = b.logs("orders", 0, 1e12)
    assert all(ts(l["ts"]) <= b.now for l in logs)


def test_every_tool_runs_and_is_rerunnable(snapshot_dir, tmp_path):
    tk = kit(first(snapshot_dir), tmp_path)
    args = {"get_alert": {}, "get_service_graph": {}, "scan_anomalies": {},
            "query_metrics": {"service": "orders", "metric": "latency_p95"},
            "search_logs": {"service": "orders"}, "list_deploys": {"hours": 2},
            "search_runbooks": {"query": "pool timeout"}}
    for tool in ALL_TOOLS:
        if tool == "get_commit_diff":
            continue
        r = tk.call(tool, args[tool])
        assert r["ok"], (tool, r["summary"])
    for c in tk.calls:
        assert tk.rerun_hash(c) == c.provenance.raw_hash


def test_summary_numbers_are_in_raw(snapshot_dir, tmp_path):
    from agent.validation.structural import quoted_values
    tk = kit(first(snapshot_dir), tmp_path)
    tk.call("query_metrics", {"service": "orders", "metric": "error_rate"})
    c = tk.calls[-1]
    raw = Path(c.provenance.raw_ref).read_text()
    assert all(v in raw for v in quoted_values(c.summary.split(".")[0]))


def test_bad_arguments_are_reported_not_raised(snapshot_dir, tmp_path):
    tk = kit(first(snapshot_dir), tmp_path)
    assert not tk.call("query_metrics", {"service": "nope", "metric": "cpu"})["ok"]
    assert not tk.call("get_commit_diff", {"sha": "../../etc"})["ok"]
    assert tk.call("search_runbooks", {"query": "x"}, permitted={"search_logs"})["call_id"] is None


def test_timestamp_parser_handles_python310_formats():
    assert ts("2026-10-01T05:12:00.00Z") == ts("2026-10-01T05:12:00Z")
    assert ts("2026-10-01T05:12:00.123Z") - ts("2026-10-01T05:12:00Z") == pytest.approx(0.123)


def test_runbook_search_finds_relevant_section():
    hits = search("401 Unauthorized bank token")
    assert hits and hits[0]["source"] == "credential-errors.md"
