"""Offline stand-in for an LLM: a deterministic, rule-based investigator.

It answers every role the agents use (single, supervisor, specialists, verifier,
critic, conclude) by reading the same tool results the real model would see, so
the whole pipeline, harness, scorer and analysis run end to end without an API
key. It is a test fixture and a weak reference point, NOT a research result:
report numbers only from a real model.

It never reads ground truth: only tool results recorded in the toolkit.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from agent.llm import LLM, LLMResponse, estimate_tokens
from tools.backend import ts as parse_ts

KEYWORDS = {
    "credential_failure": ["401", "unauthorized"],
    "code_change": ["traceback", "keyerror", "typeerror", "unhandled exception"],
    "config_change": ["timeout after 50ms"],
    "dependency_unavailable": ["connection refused", "unavailable (connection"],
    "database_performance": ["slow query"],
    "resource_exhaustion": ["oomkilled", "pool timeout", "no space left", "queue depth", "event loop blocked",
                            "heartbeat delayed", "stalled", "gc pause", "backlog growing", "shedding load",
                            "all 20 connections busy", "could not acquire connection"],
}
SYMPTOM = ["upstream timeout", "retrying", "failed with 503", "returned 503"]
CAUSE_METRICS = {
    "database_performance": [("db_query_p95", "up")],
    "dependency_unavailable": [("dependency_up", "down")],
    "resource_exhaustion": [("db_pool_in_use", "up"), ("memory", "up"), ("disk_free", "down"), ("cpu", "up")],
    "config_change": [("error_rate", "up")], "code_change": [("error_rate", "up")],
    "credential_failure": [("error_rate", "up")],
}
DEPLOY_CAUSES = {"config_change", "code_change", "credential_failure"}


def _raw(call) -> dict:
    try:
        return json.loads(Path(call.provenance.raw_ref).read_text())
    except (OSError, ValueError, AttributeError):
        return {}


def _text(raw: dict) -> str:
    return " ".join(p.get("example", "") for p in raw.get("patterns", [])).lower()


def _keyword_causes(text: str) -> list[str]:
    return [c for c, kws in KEYWORDS.items() if any(k in text for k in kws)]


class Analyst:
    """Shared reasoning over the tool results recorded in a toolkit."""

    def __init__(self, toolkit):
        self.tk = toolkit
        self.b = toolkit.backend

    def latest(self, tool: str, **match):
        for c in reversed(self.tk.calls):
            if c.tool == tool and c.provenance.ok and all(c.args.get(k) == v for k, v in match.items()):
                return c
        return None

    def alert_service(self) -> str:
        return self.b.alert()["alerts"][0]["labels"]["service"]

    def graph(self) -> dict:
        return self.b.service_graph()

    def descendants(self, svc: str) -> list[str]:
        calls, out, todo = self.graph()["calls"], [], [svc]
        while todo:
            s = todo.pop(0)
            for c in calls.get(s, []):
                if c not in out:
                    out.append(c)
                    todo.append(c)
        return out

    def depth(self, svc: str) -> int:
        callers = {c: s for s, cs in self.graph()["calls"].items() for c in cs}
        d = 0
        while svc in callers:
            svc, d = callers[svc], d + 1
        return d

    def departures(self) -> list[dict]:
        c = self.latest("scan_anomalies")
        return _raw(c).get("departures", []) if c else []

    def candidates(self) -> list[str]:
        """Services on the alerting service's call path, earliest departure first, ties to the deepest."""
        a = self.alert_service()
        path = [a] + self.descendants(a)
        first: dict[str, float] = {}
        for d in self.departures():
            if d["service"] in path:
                first[d["service"]] = min(first.get(d["service"], 1e18), d["since_ts"])
        ranked = sorted(first, key=lambda s: (first[s], -self.depth(s)))
        return ranked + [s for s in path if s not in ranked]

    def departed(self, svc: str) -> dict[str, str]:
        return {d["metric"]: d["direction"] for d in self.departures() if d["service"] == svc}

    def cause_guess(self, svc: str) -> tuple[str, float]:
        dep = self.departed(svc)
        logs = self.latest("search_logs", service=svc)
        kw = _keyword_causes(_text(_raw(logs))) if logs else []
        for cause in ("credential_failure", "code_change", "config_change", "dependency_unavailable",
                      "database_performance"):
            if cause in kw:
                return cause, 0.85
        if dep.get("db_query_p95") == "up":
            return "database_performance", 0.8
        if dep.get("dependency_up") == "down":
            return "dependency_unavailable", 0.8
        if "resource_exhaustion" in kw or any(dep.get(m) == d for m, d in CAUSE_METRICS["resource_exhaustion"]):
            return "resource_exhaustion", 0.75
        rel = self.release_near(svc)
        if rel:
            msg = rel["message"].lower()
            if "rotate" in msg or "secret" in msg or "credential" in msg:
                return "credential_failure", 0.55
            return ("config_change" if msg.startswith("config") else "code_change"), 0.55
        return "code_change", 0.35

    def release_near(self, svc: str) -> dict | None:
        c = self.latest("list_deploys")
        if not c:
            return None
        onset = min([d["since_ts"] for d in self.departures() if d["service"] == svc] or [self.b.alert_at])
        rows = [d for d in _raw(c).get("deploys", []) if d["service"] == svc
                and onset - 300 <= parse_ts(d["at"]) <= onset + 20]
        return rows[-1] if rows else None

    def metric_for(self, svc: str, cause: str) -> str:
        dep = self.departed(svc)
        for m, d in CAUSE_METRICS.get(cause, []):
            if dep.get(m) == d:
                return m
        return CAUSE_METRICS.get(cause, [("error_rate", "up")])[0][0]


def _first_line(summary: str) -> str:
    return summary.splitlines()[0] if summary else ""


class OfflineLLM(LLM):
    name = "offline"
    model = "offline-heuristic-v1"

    def _complete(self, role, system, messages, temperature, max_tokens, context) -> LLMResponse:
        if role == "single":
            out = self._single(context, temperature)
        elif role == "supervisor":
            out = self._supervisor(context)
        elif role.startswith("specialist:"):
            out = self._specialist(context)
        elif role == "verifier":
            out = self._verifier(context)
        else:
            out = self._decide(context, temperature)
        text = json.dumps(out)
        tin = estimate_tokens(system) + sum(estimate_tokens(m["content"]) for m in messages)
        return LLMResponse(text, tin, estimate_tokens(text))

    # ---------------------------------------------------------- single agent
    def _single(self, ctx, temperature):
        tk, agent, permitted = ctx["toolkit"], ctx["agent"], ctx["permitted"]
        an = Analyst(tk)
        mine = [c for c in tk.calls if c.agent == agent]
        done = {(c.tool, json.dumps(c.args, sort_keys=True)) for c in mine}

        def want(tool, **args):
            return tool in permitted and (tool, json.dumps(args, sort_keys=True)) not in done

        if ctx["phase"] == "step":
            plan = [("get_alert", {}), ("get_service_graph", {}), ("scan_anomalies", {})]
            if an.latest("scan_anomalies"):
                cand = an.candidates()[0]
                plan += [("search_logs", {"service": cand}), ("list_deploys", {"hours": 1})]
                cause, _ = an.cause_guess(cand)
                plan += [("query_metrics", {"service": cand, "metric": an.metric_for(cand, cause)})]
            for tool, args in plan:
                if want(tool, **args):
                    return {"thought": f"next: {tool}", "tool": tool, "args": args}
        cands = an.candidates() if an.latest("scan_anomalies") else [an.alert_service()]
        root = cands[0]
        cause, conf = an.cause_guess(root)
        alts = []
        for s in cands[1:3]:
            alts.append({"service": s, "cause_type": an.cause_guess(s)[0], "description": "next earliest anomaly"})
        if temperature > 0 and alts and random.Random(f"{len(tk.calls)}{ctx.get('sample')}").random() < (1 - conf) * 0.8:
            root, cause = alts[0]["service"], alts[0]["cause_type"]
        evidence = self._evidence_for(an, root, cause, "root", own=mine)
        return {"final": {"root_cause": {"service": root, "cause_type": cause,
                                         "description": f"{cause.replace('_', ' ')} in {root}"},
                          "alternatives": alts, "confidence": conf, "evidence": evidence,
                          "summary": f"{root} departed from normal first on the alert's call path; "
                                     f"signals point to {cause.replace('_', ' ')}."}}

    def _evidence_for(self, an: Analyst, svc: str, cause: str, hid: str, own) -> list[dict]:
        ev = []
        for c in own:
            raw = _raw(c)
            if c.tool == "scan_anomalies":
                rows = [d for d in raw.get("departures", []) if d["service"] == svc]
                if rows:
                    r = rows[0]
                    ev.append({"call_id": c.call_id, "hypothesis": hid, "hypothesis_id": hid, "claimed": "supports",
                               "summary": f"{svc} {r['metric']} went {r['direction']} at {r['since']} "
                                          f"(normal {r['baseline']}, now {r['now']})"})
            elif c.tool == "query_metrics" and c.args.get("service") == svc:
                st = raw.get("stats", {})
                if st.get("departed_at"):
                    ev.append({"call_id": c.call_id, "hypothesis": hid, "hypothesis_id": hid, "claimed": "supports",
                               "summary": f"{svc} {c.args.get('metric')} normal {st['normal']}, now {st['now']}, "
                                          f"departed at {st['departed_at']}"})
            elif c.tool == "search_logs" and c.args.get("service") == svc:
                pats = [p for p in raw.get("patterns", []) if cause in _keyword_causes(p.get("example", "").lower())]
                if pats:
                    p = pats[0]
                    ev.append({"call_id": c.call_id, "hypothesis": hid, "hypothesis_id": hid, "claimed": "supports",
                               "summary": f"{svc} logged \"{p['example']}\" {p['count']} times from {p['first']}"})
            elif c.tool == "list_deploys" and cause in DEPLOY_CAUSES:
                rel = an.release_near(svc)
                if rel:
                    ev.append({"call_id": c.call_id, "hypothesis": hid, "hypothesis_id": hid, "claimed": "supports",
                               "summary": f"release {rel['id']} on {svc} at {rel['time'][11:19]}: {rel['message']}"})
        return ev

    # ---------------------------------------------------------- supervisor
    def _supervisor(self, ctx):
        an = Analyst(ctx["inv"].toolkit)
        cands = an.candidates()[:3]
        hyps = []
        for i, s in enumerate(cands):
            cause, _ = an.cause_guess(s)
            hyps.append({"id": f"h{i + 1}", "service": s, "cause_type": cause,
                         "description": f"{s} shows the earliest departure on the call path" if i == 0
                         else f"{s} is also abnormal"})
        top = cands[0] if cands else an.alert_service()
        return {"hypotheses": hyps, "assignments": {
            "logs": f"Check error and warning patterns on {', '.join(cands)}; say which service's problem started first.",
            "metrics": f"Confirm which of {', '.join(cands)} departed from normal first and in which signal.",
            "changes": f"Check for releases on {top} or its callees shortly before the alert."}}

    # ---------------------------------------------------------- specialists
    def _specialist(self, ctx):
        tk, agent, inv = ctx["toolkit"], ctx["agent"], ctx["inv"]
        an = Analyst(tk)
        hyps = list(inv.hypotheses)
        mine = [c for c in tk.calls if c.agent == agent]
        done = {(c.tool, json.dumps(c.args, sort_keys=True)) for c in mine}
        services = list(dict.fromkeys(h.service.value for h in hyps))
        plan = []
        if agent in ("logs", "investigator"):
            plan += [("search_logs", {"service": s}) for s in services]
        if agent in ("metrics", "investigator"):
            plan += [("query_metrics", {"service": h.service.value, "metric": an.metric_for(h.service.value, h.cause_type.value)})
                     for h in hyps]
        if agent in ("changes", "investigator"):
            plan += [("list_deploys", {"hours": 1})]
            for h in hyps:
                rel = an.release_near(h.service.value)
                if rel:
                    plan.append(("get_commit_diff", {"sha": rel["sha"]}))
        if ctx["phase"] == "step":
            for tool, args in plan:
                if tool in ctx["permitted"] and (tool, json.dumps(args, sort_keys=True)) not in done:
                    return {"thought": f"next: {tool}", "tool": tool, "args": args}
        evidence, new = [], None
        for h in hyps:
            svc, cause = h.service.value, h.cause_type.value
            for c in [c for c in tk.calls if c.agent == agent]:
                raw = _raw(c)
                if c.tool == "search_logs" and c.args.get("service") == svc and raw.get("patterns"):
                    text = _text(raw)
                    found = _keyword_causes(text)
                    pats = raw["patterns"]
                    if cause in found:
                        p = next(p for p in pats if cause in _keyword_causes(p["example"].lower()))
                        evidence.append(self._ev(c, h.id, "supports", f"{svc} logged \"{p['example']}\" {p['count']} times from {p['first']}"))
                    elif found:
                        p = next(p for p in pats if _keyword_causes(p["example"].lower()))
                        evidence.append(self._ev(c, h.id, "contradicts", f"{svc} logs point elsewhere: \"{p['example']}\""))
                        if new is None and len(hyps) < 4:
                            new = {"service": svc, "cause_type": _keyword_causes(p["example"].lower())[0],
                                   "description": f"logs on {svc} suggest this instead"}
                    elif any(k in text for k in SYMPTOM):
                        p = next(p for p in pats if any(k in p["example"].lower() for k in SYMPTOM))
                        evidence.append(self._ev(c, h.id, "contradicts", f"{svc} only reports failures of a service it calls: \"{p['example']}\""))
                elif c.tool == "query_metrics" and c.args.get("service") == svc:
                    st = raw.get("stats", {})
                    want = dict(CAUSE_METRICS.get(cause, [])).get(c.args.get("metric"))
                    if st.get("departed_at") and (want is None or st.get("direction") == want):
                        evidence.append(self._ev(c, h.id, "supports", f"{svc} {c.args['metric']} normal {st['normal']}, now {st['now']}, departed at {st['departed_at']}"))
                    elif not st.get("departed_at"):
                        evidence.append(self._ev(c, h.id, "contradicts", f"{svc} {c.args['metric']} stayed normal ({st.get('normal')})"))
                elif c.tool == "list_deploys" and cause in DEPLOY_CAUSES:
                    rel = an.release_near(svc)
                    if rel:
                        evidence.append(self._ev(c, h.id, "supports", f"release {rel['id']} on {svc} at {rel['time'][11:19]}: {rel['message']}"))
                    else:
                        evidence.append(self._ev(c, h.id, "contradicts", f"no release on {svc} shortly before the incident"))
                elif c.tool == "get_commit_diff" and raw.get("service") == svc and cause in DEPLOY_CAUSES:
                    paths = " ".join(f["path"] for f in raw.get("files", []))
                    fits = {"config_change": "config/settings", "code_change": "app/", "credential_failure": "secret"}[cause]
                    if fits in paths:
                        evidence.append(self._ev(c, h.id, "supports", f"commit {raw['sha']} changed {paths}"))
        return {"final": {"evidence": evidence, "new_hypothesis": new, "notes": "rule-based specialist"}}

    @staticmethod
    def _ev(call, hid, claimed, summary):
        return {"call_id": call.call_id, "hypothesis_id": hid, "claimed": claimed, "summary": summary}

    # ---------------------------------------------------------- verifier
    def _verifier(self, ctx):
        h, ev, raw = ctx["hypothesis"], ctx["evidence"], ctx["raw"]
        svc, cause, tool = h.service.value, h.cause_type.value, ev.provenance.tool

        def say(label, reason):
            return {"label": label, "reason": reason}

        if tool == "search_logs":
            if raw.get("service") != svc:
                return say("IRRELEVANT", "logs from a different service")
            text = _text(raw)
            found = _keyword_causes(text)
            if cause in found:
                return say("SUPPORTS", "log patterns match this cause")
            if found or any(k in text for k in SYMPTOM):
                return say("CONTRADICTS", "logs point to a different cause or only to callee failures")
            return say("INSUFFICIENT", "no telling log patterns")
        if tool == "query_metrics":
            if raw.get("service") != svc:
                return say("IRRELEVANT", "metric from a different service")
            st, metric = raw.get("stats", {}), raw.get("metric")
            want = dict(CAUSE_METRICS.get(cause, [])).get(metric)
            if want is None:
                return say("INSUFFICIENT", "metric not specific to this cause")
            if st.get("departed_at") and st.get("direction") == want:
                return say("SUPPORTS", "the cause-specific metric departed in the expected direction")
            return say("CONTRADICTS", "the cause-specific metric did not move as expected")
        if tool == "scan_anomalies":
            rows = raw.get("departures", [])
            mine = [r for r in rows if r["service"] == svc]
            if not mine:
                return say("CONTRADICTS", "no departure on this service")
            return say("SUPPORTS", "this service departed from normal")
        if tool == "list_deploys":
            if cause not in DEPLOY_CAUSES:
                return say("IRRELEVANT", "releases do not bear on this cause")
            ok = any(d["service"] == svc for d in raw.get("deploys", []) if d["id"] in ev.summary)
            return say("SUPPORTS", "a release on this service is named") if ok else say("CONTRADICTS", "no matching release")
        if tool == "get_commit_diff":
            return say("SUPPORTS" if raw.get("service") == svc else "IRRELEVANT", "commit on this service")
        return say("IRRELEVANT", "tool does not bear on the hypothesis")

    # ---------------------------------------------------------- critic / conclude
    def _decide(self, ctx, temperature):
        from agent.common import evidence_margin, open_ledger, ranking_by_evidence, support
        inv = ctx["inv"]
        ranking = ranking_by_evidence(inv)
        if not ranking:
            return {"decision": "accept", "hypothesis_id": None, "ranking": [], "confidence": 0.2, "reason": "no hypotheses"}
        top = ranking[0]
        margin = evidence_margin(inv, top)
        conf = round(min(0.95, 0.45 + 0.12 * max(0.0, margin)), 2)
        if temperature > 0 and len(ranking) > 1:
            flip = 0.35 if margin <= 1 else 0.1 if margin <= 2 else 0.03
            if random.Random(f"{len(inv.evidence)}-{ctx.get('sample')}").random() < flip:
                top = ranking[1]
        s, c = support(inv, top)
        rnd, last = ctx.get("round", 1), ctx.get("round", 1) >= ctx.get("max_rounds", 1)
        unresolved = open_ledger(inv, top)
        resolutions = [{"evidence_id": l.evidence_id, "explanation": "outweighed by the other usable evidence"}
                       for l in unresolved if s - c >= 2]
        decision = "accept" if (margin >= 1 or last or rnd >= 2) else "more_evidence"
        return {"decision": decision, "hypothesis_id": top, "ranking": ranking, "confidence": conf,
                "ledger_resolutions": resolutions,
                "requests": [] if decision == "accept" else [
                    {"specialist": "metrics", "ask": f"Check cause-specific metrics for {', '.join(ranking[:2])}."},
                    {"specialist": "logs", "ask": f"Look for error patterns that separate {', '.join(ranking[:2])}."}],
                "reason": f"{top} has {s} usable supporting items and {c} verified contradictions (margin {margin:g})."}
