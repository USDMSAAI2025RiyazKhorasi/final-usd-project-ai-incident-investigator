"""Temporal precedence: a claimed cause must start before the symptom. Deterministic.

Symptom onset = when the alerting service first departed from normal.
Evidence onset depends on the tool:
  query_metrics -> when that metric departed from normal
  search_logs   -> when the earliest reported pattern was first seen
  list_deploys / get_commit_diff -> the release time named in the evidence
Other tools carry no time and are not checked (None).
"""
from __future__ import annotations

import re

from tools.backend import ts as parse_ts
from tools.toolkit import SERVICES, departure, derived_series, BASELINE_S

TOLERANCE_S = 20          # one alert evaluation interval plus a few samples


def symptom_onset(backend, alert_service: str) -> float | None:
    times = []
    for m in ("error_rate", "latency_p95", "memory", "disk_free", "db_pool_in_use"):
        pts = derived_series(backend, alert_service, m, backend.window_start, backend.now)
        d = departure(pts, backend.window_start + BASELINE_S) if pts else {}
        if d.get("departed"):
            times.append(d["since"])
    return min(times) if times else None


def evidence_onset(call, raw: dict, summary: str) -> float | None:
    day = None
    if call.tool == "query_metrics":
        st = raw.get("stats", {})
        if st.get("departed_at") and raw.get("points"):
            day = raw["points"][0][0][:10]
            return parse_ts(f"{day}T{st['departed_at']}Z")
        return None
    if call.tool == "search_logs":
        pats = raw.get("patterns") or []
        firsts = [parse_ts(p["first_seen"]) for p in pats if p.get("level") in ("WARN", "ERROR")]
        return min(firsts) if firsts else None
    if call.tool in ("list_deploys", "get_commit_diff"):
        deploys = raw.get("deploys") or []
        named = [d for d in deploys if d["id"] in summary or d.get("sha", "#") in summary]
        if call.tool == "get_commit_diff" and raw.get("sha"):
            return None
        if named:
            return min(parse_ts(d["at"]) for d in named)
        return None
    return None


def precedes(call, raw: dict, summary: str, symptom: float | None) -> bool | None:
    if symptom is None or call is None:
        return None
    onset = evidence_onset(call, raw, summary)
    if onset is None:
        return None
    return onset <= symptom + TOLERANCE_S
