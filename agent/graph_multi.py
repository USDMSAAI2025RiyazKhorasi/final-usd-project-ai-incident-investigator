"""Multi-agent graph: supervisor, three specialists, evidence validation, critic, gate.

Configuration flags (eval/configs/*.yaml) switch parts off for the ablations:
  critic=false            A1  the supervisor concludes once, no challenge loop
  semantic_verifier=false A2  structural checks only
  structural_checks=false A3  no validation at all
  runbook_search=false    A4  the runbook tool is removed for every agent
  gate=false              A5  every case passes the gate
  architecture=multi_merged A6  one investigator with all tools replaces the three specialists

Implemented as a plain, explicit state machine rather than a graph library, so
each step is easy to read, test and switch off.
"""
from __future__ import annotations

from agent.common import (Investigation, Result, add_hypothesis, board, evidence_margin, finish, ledger_text,
                          make_evidence, open_ledger, prompt, ranking_by_evidence, react, result_of, tools_for,
                          validate)
from agent.llm import BudgetExceeded, parse_json
from agent.state import ConfidenceSignals
from tools.toolkit import ALL_TOOLS

SPECIALISTS = {
    "logs": {"speciality": "logs analyst", "steps": 5,
             "focus": "You read application logs: error and warning patterns, when they started, and which service they come from.",
             "tools": ["search_logs", "get_service_graph", "search_runbooks"]},
    "metrics": {"speciality": "metrics analyst", "steps": 5,
                "focus": "You read time-series metrics: which signals departed from normal, on which service, and in what order.",
                "tools": ["scan_anomalies", "query_metrics", "get_service_graph", "search_runbooks"]},
    "changes": {"speciality": "changes analyst", "steps": 4,
                "focus": "You read the release history and code diffs, and judge whether a release could explain the incident.",
                "tools": ["list_deploys", "get_commit_diff", "get_service_graph", "search_runbooks"]},
    "investigator": {"speciality": "investigator", "steps": 12,
                     "focus": "You gather logs, metrics and release evidence yourself.",
                     "tools": [t for t in ALL_TOOLS if t != "get_alert"]},
}
DEFAULT_ASK = "Find evidence for and against each hypothesis, especially the first."


def _hyp_text(inv: Investigation) -> str:
    return "\n".join(f"{h.id}: {h.cause_type.value} in {h.service.value}. {h.description}" for h in inv.hypotheses)


def _run_specialist(inv: Investigation, name: str, instruction: str, steps: int | None = None) -> None:
    spec = SPECIALISTS[name]
    permitted = tools_for(inv.cfg, spec["tools"])
    n = steps or spec["steps"]
    system = prompt("specialist", speciality=spec["speciality"], focus=spec["focus"], hypotheses=_hyp_text(inv),
                    instruction=instruction or DEFAULT_ASK, tools=inv.toolkit.specs(permitted), max_steps=n)
    final, _ = react(inv, f"specialist:{name}", name, system, "Begin.", permitted, n,
                     context={"speciality": name, "hypotheses": list(inv.hypotheses), "instruction": instruction,
                              "inv": inv})
    nh = final.get("new_hypothesis")
    if isinstance(nh, dict) and len(inv.hypotheses) < 5:
        h = add_hypothesis(inv, nh, name)
        if h:
            inv.trace.add("hypothesis", id=h.id, by=name, service=h.service.value, cause=h.cause_type.value)
    validate(inv, make_evidence(inv, final.get("evidence") or [], name))


def _decide(inv: Investigation, role: str, rnd: int, max_rounds: int, temperature: float = 0.0, sample: int = 0) -> dict:
    a = inv.alert
    alert = f"{a['labels']['alertname']} on {a['labels']['service']}: {a['annotations']['summary']}"
    if role == "critic":
        system = prompt("critic", alert=alert, board=board(inv), ledger=ledger_text(inv), round=rnd,
                        max_rounds=max_rounds)
    else:
        system = prompt("conclude", alert=alert, board=board(inv))
    text = inv.llm.complete(role, system, [{"role": "user", "content": "Decide now."}], temperature=temperature,
                            max_tokens=900, context={"inv": inv, "round": rnd, "max_rounds": max_rounds,
                                                     "temperature": temperature, "sample": sample})
    try:
        obj = parse_json(text)
        obj = obj if isinstance(obj, dict) else {}
    except ValueError:
        obj = {}
    ids = {h.id for h in inv.hypotheses}
    if obj.get("hypothesis_id") not in ids:
        obj["hypothesis_id"] = (ranking_by_evidence(inv) or [None])[0]
    rank = [i for i in obj.get("ranking") or [] if i in ids]
    obj["ranking"] = rank + [i for i in ranking_by_evidence(inv) if i not in rank]
    if obj["ranking"] and obj["ranking"][0] != obj["hypothesis_id"]:
        obj["ranking"].remove(obj["hypothesis_id"])
        obj["ranking"].insert(0, obj["hypothesis_id"])
    try:
        obj["confidence"] = min(1.0, max(0.0, float(obj.get("confidence"))))
    except (TypeError, ValueError):
        obj["confidence"] = None
    if sample == 0:
        inv.trace.add("decision", role=role, round=rnd, decision=obj.get("decision", "accept"),
                      hypothesis=obj["hypothesis_id"], confidence=obj["confidence"], reason=str(obj.get("reason", ""))[:500])
    return obj


def run_multi(inv: Investigation) -> Result:
    cfg, pol = inv.cfg, inv.policy
    try:
        # 1. the framework gathers the supervisor's context (counted against the same tool budget)
        alert = inv.tool("get_alert", agent="supervisor")
        graph = inv.tool("get_service_graph", agent="supervisor")
        scan = inv.tool("scan_anomalies", agent="supervisor")
        text = inv.llm.complete("supervisor", prompt("supervisor"), [{"role": "user", "content":
                                f"Alert: {alert['summary']}\n\nService map:\n{graph['summary']}\n\n"
                                f"Anomaly scan:\n{scan['summary']}"}], max_tokens=900,
                                context={"inv": inv, "calls": [alert["call_id"], graph["call_id"], scan["call_id"]]})
        try:
            plan = parse_json(text)
            plan = plan if isinstance(plan, dict) else {}
        except ValueError:
            plan = {}
        for d in (plan.get("hypotheses") or [])[:4]:
            if isinstance(d, dict):
                add_hypothesis(inv, d, "supervisor")
        inv.trace.add("plan", hypotheses=[h.model_dump(mode="json") for h in inv.hypotheses],
                      assignments=plan.get("assignments"))
        if not inv.hypotheses:
            rep = finish(inv, None, [], ConfidenceSignals(), note="The supervisor produced no valid hypothesis.")
            return result_of(inv, rep)

        if cfg.architecture == "multi_merged":
            assignments = {"investigator": DEFAULT_ASK}
        else:
            given = plan.get("assignments") if isinstance(plan.get("assignments"), dict) else {}
            assignments = {k: str(v) for k, v in given.items() if k in ("logs", "metrics", "changes") and v}
            assignments = assignments or {k: DEFAULT_ASK for k in ("logs", "metrics", "changes")}

        # 2. evidence rounds, challenged by the critic
        max_rounds = pol["caps"]["critic_rounds"] if cfg.critic else 1
        verdict: dict = {}
        for rnd in range(1, max_rounds + 1):
            for name, ask in assignments.items():
                _run_specialist(inv, name, ask, steps=None if rnd == 1 else 3)
            if not cfg.critic:
                break
            verdict = _decide(inv, "critic", rnd, max_rounds)
            ev_ids = {l.evidence_id for l in open_ledger(inv)}
            for r in verdict.get("ledger_resolutions") or []:
                if isinstance(r, dict) and r.get("evidence_id") in ev_ids and r.get("explanation"):
                    for l in inv.ledger:
                        if l.evidence_id == r["evidence_id"]:
                            l.resolved_by_critic = str(r["explanation"])[:300]
            last = rnd == max_rounds
            unresolved = open_ledger(inv, verdict["hypothesis_id"])
            if verdict.get("decision", "accept") == "accept" and (not unresolved or last):
                break
            if last:
                break
            asks = {}
            for r in verdict.get("requests") or []:
                if isinstance(r, dict):
                    if cfg.architecture == "multi_merged":
                        who = "investigator"
                    else:
                        who = r.get("specialist") if r.get("specialist") in ("logs", "metrics", "changes") else None
                    if who:
                        asks[who] = str(r.get("ask") or DEFAULT_ASK)
            if unresolved and not asks:
                text = "Explain or refute these contradictions: " + "; ".join(l.evidence_id for l in unresolved)
                asks = ({"investigator": text} if cfg.architecture == "multi_merged"
                        else {"logs": text, "metrics": text})
            assignments = asks or assignments

        # 3. decision and confidence signals
        role = "critic" if cfg.critic else "conclude"
        if not cfg.critic:
            verdict = _decide(inv, "conclude", 1, 1)
        hid = verdict["hypothesis_id"]
        root = next((h for h in inv.hypotheses if h.id == hid), None)
        signals = ConfidenceSignals(verbal=verdict.get("confidence"))
        if root is not None:
            signals.evidence_margin = evidence_margin(inv, root.id)
            agree = 1
            for k in range(pol["critic"]["samples_for_consistency"] - 1):
                s = _decide(inv, role, max_rounds, max_rounds, temperature=pol["critic"]["sample_temperature"],
                            sample=k + 1)
                agree += s["hypothesis_id"] == hid
            signals.self_consistency = agree / pol["critic"]["samples_for_consistency"]
        rep = finish(inv, root, verdict["ranking"], signals, note=str(verdict.get("reason", "")))
        return result_of(inv, rep)
    except BudgetExceeded as e:
        rank = ranking_by_evidence(inv)
        rep = finish(inv, None, rank, ConfidenceSignals(), capped=True, note=str(e))
        return result_of(inv, rep, error=str(e))
