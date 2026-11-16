"""Shared machinery for both architectures: prompts, the reason-act loop, evidence,
validation, the contradiction ledger, signals, the gate and the report.

Both graphs use exactly the same tools, validation code, gate and report builder,
so differences between configurations come from the architecture flags only.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import gate
from agent.llm import LLM, BudgetExceeded, Usage, parse_json
from agent.remediation.rules import propose_action
from agent.state import (CauseType, ConfidenceSignals, Evidence, Hypothesis, LedgerEntry, Provenance, Report,
                         Service)
from agent.validation import precedence, semantic, structural
from tools.toolkit import ALL_TOOLS, Toolkit

PROMPTS = Path(__file__).with_name("prompts")
COMMON = (PROMPTS / "_common.md").read_text().strip()
SERVICES = [s.value for s in Service]
CAUSES = [c.value for c in CauseType]


def prompt(name: str, **kw) -> str:
    return (PROMPTS / f"{name}.md").read_text().format(common=COMMON, **kw)


# ------------------------------------------------------------------ trace
@dataclass
class Trace:
    events: list[dict] = field(default_factory=list)
    t0: float = field(default_factory=time.time)

    def add(self, kind: str, **data) -> None:
        self.events.append({"t": round(time.time() - self.t0, 3), "kind": kind, **data})


# ------------------------------------------------------------------ investigation context
@dataclass
class Investigation:
    investigation_id: str
    cfg: object                     # eval.config.ExperimentConfig
    policy: dict
    llm: LLM
    toolkit: Toolkit
    usage: Usage
    trace: Trace = field(default_factory=Trace)
    evidence: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    _symptom: float | None = None
    _symptom_done: bool = False

    @property
    def backend(self):
        return self.toolkit.backend

    @property
    def alert(self) -> dict:
        return self.backend.alert()["alerts"][0]

    @property
    def alert_service(self) -> str:
        return self.alert["labels"]["service"]

    def symptom_onset(self) -> float | None:
        if not self._symptom_done:
            self._symptom = precedence.symptom_onset(self.backend, self.alert_service)
            self._symptom_done = True
        return self._symptom

    def tool(self, name: str, args: dict | None = None, agent: str = "framework") -> dict:
        self.usage.add_tool()
        self.usage.check()
        res = self.toolkit.call(name, args or {}, agent=agent)
        self.trace.add("tool", agent=agent, tool=name, args=args or {}, call_id=res["call_id"],
                       ok=res["ok"], summary=res["summary"])
        return res


def tools_for(cfg, names: list[str]) -> set[str]:
    allowed = set(names)
    if not cfg.runbook_search:
        allowed.discard("search_runbooks")
    return allowed


# ------------------------------------------------------------------ reason-act loop
def _add_user(messages: list[dict], text: str) -> None:
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n\n" + text
    else:
        messages.append({"role": "user", "content": text})


def react(inv: Investigation, role: str, agent: str, system: str, task: str, permitted: set[str],
          max_steps: int, context: dict | None = None) -> tuple[dict, list[dict]]:
    """Tool-using loop. Returns (final object, messages). Raises BudgetExceeded when caps are hit."""
    messages: list[dict] = [{"role": "user", "content": task}]
    steps, bad, forced = 0, 0, False
    while True:
        if steps >= max_steps and not forced:
            _add_user(messages, "You have no tool calls left. Reply now with your final JSON answer.")
            forced = True
        ctx = {**(context or {}), "role": role, "agent": agent, "toolkit": inv.toolkit,
               "phase": "final" if forced else "step", "messages": messages, "max_steps": max_steps,
               "permitted": permitted, "steps": steps}
        text = inv.llm.complete(role, system, messages, temperature=0.0, max_tokens=1500, context=ctx)
        messages.append({"role": "assistant", "content": text})
        inv.trace.add("llm", agent=agent, role=role, text=text[:4000])
        try:
            obj = parse_json(text)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except ValueError:
            bad += 1
            if bad > 2:
                return {}, messages
            _add_user(messages, "Reply with a single JSON object only, as described.")
            continue
        if "final" in obj:
            return obj["final"] if isinstance(obj["final"], dict) else {}, messages
        if forced:
            return {}, messages
        tool = obj.get("tool")
        if inv.usage.tool_calls >= inv.usage.max_tool_calls:      # shared tool budget used up
            _add_user(messages, "The investigation's tool budget is used up. Reply now with your final JSON answer.")
            forced = True
            continue
        if tool not in ALL_TOOLS:
            _add_user(messages, f"Unknown tool {tool!r}. Use one of: {', '.join(sorted(permitted))}.")
            steps += 1
            continue
        inv.usage.add_tool()
        inv.usage.check()
        res = inv.toolkit.call(tool, obj.get("args") or {}, agent=agent, permitted=permitted)
        inv.trace.add("tool", agent=agent, tool=tool, args=obj.get("args") or {}, call_id=res["call_id"],
                      ok=res["ok"], summary=res["summary"])
        _add_user(messages, f"Observation {res['call_id'] or '(not run)'}: {res['summary']}")
        steps += 1


# ------------------------------------------------------------------ hypotheses and evidence
def to_hypothesis(d: dict, hid: str, created_by: str = "supervisor") -> Hypothesis | None:
    try:
        svc = str(d.get("service", "")).strip().lower()
        cause = str(d.get("cause_type", "")).strip().lower()
        if svc not in SERVICES or cause not in CAUSES:
            return None
        return Hypothesis(id=hid, service=svc, cause_type=cause,
                          description=str(d.get("description", ""))[:300], created_by=created_by)
    except (TypeError, ValueError):
        return None


def add_hypothesis(inv: Investigation, d: dict, created_by: str) -> Hypothesis | None:
    for h in inv.hypotheses:
        if h.service.value == str(d.get("service", "")).lower() and h.cause_type.value == str(d.get("cause_type", "")).lower():
            return h
    h = to_hypothesis(d, f"h{len(inv.hypotheses) + 1}", created_by)
    if h:
        inv.hypotheses.append(h)
    return h


def make_evidence(inv: Investigation, items: list, agent: str, hyp_map: dict[str, str] | None = None) -> list[Evidence]:
    """Turn cited items into Evidence with provenance from the tool call they name."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        hid = str(it.get("hypothesis_id") or it.get("hypothesis") or "")
        hid = (hyp_map or {}).get(hid, hid)
        claimed = "contradicts" if str(it.get("claimed", "")).lower().startswith("contra") else "supports"
        call = inv.toolkit.get(str(it.get("call_id", "")))
        if call is not None:
            prov = call.provenance
        else:   # cited a call that does not exist: provenance stays incomplete and fails structural checks
            now = datetime.now(timezone.utc)
            prov = Provenance(tool="", args={}, called_at=now, data_start=now, data_end=now, raw_ref="",
                              raw_hash="", mode=inv.backend.mode, ok=False)
        ev = Evidence(id=f"e{len(inv.evidence) + len(out) + 1}", agent=agent, hypothesis_id=hid, claimed=claimed,
                      summary=str(it.get("summary", ""))[:500], provenance=prov,
                      call_id=call.call_id if call else None)
        out.append(ev)
    return out


def raw_of(ev: Evidence) -> dict:
    try:
        return json.loads(Path(ev.provenance.raw_ref).read_text())
    except (OSError, ValueError):
        return {}


def validate(inv: Investigation, items: list[Evidence]) -> list[Evidence]:
    """Structural checks, then semantic verification and temporal precedence, as the config allows."""
    cfg, out = inv.cfg, []
    hyp = {h.id: h for h in inv.hypotheses}
    margin = timedelta(seconds=inv.policy["evidence"]["window_margin_seconds"])
    window = (datetime.fromtimestamp(inv.backend.window_start, tz=timezone.utc),
              datetime.fromtimestamp(inv.backend.now, tz=timezone.utc))
    for ev in items:
        if cfg.structural_checks:
            call = inv.toolkit.get(ev.call_id) if ev.call_id else None
            ev = structural.check(ev, hypothesis_ids=set(hyp), alert_window=window, margin=margin,
                                  load_raw=lambda ref: Path(ref).read_text(),
                                  rerun_hash=(lambda e, c=call: inv.toolkit.rerun_hash(c)) if call else None,
                                  require_grounded_values=inv.policy["evidence"]["require_grounded_values"])
        else:   # A3: no validation at all
            ev = ev.model_copy(update={"structural": "valid", "structural_reason": "unchecked"})
        if ev.structural == "valid" and ev.hypothesis_id not in hyp:
            ev = ev.model_copy(update={"structural": "invalid", "structural_reason": "unknown hypothesis"})
        if ev.structural == "valid":
            assumed = "SUPPORTS" if ev.claimed == "supports" else "CONTRADICTS"
            if cfg.semantic_verifier:
                label, reason = semantic.verify(inv.llm, prompt, hyp[ev.hypothesis_id], ev, raw_of(ev))
                pre = None
                if ev.claimed == "supports" and label == "SUPPORTS":
                    call = inv.toolkit.get(ev.call_id) if ev.call_id else None
                    pre = precedence.precedes(call, raw_of(ev), ev.summary, inv.symptom_onset())
                ev = ev.model_copy(update={"verdict": label, "verdict_reason": reason, "precedes_symptom": pre})
            else:
                ev = ev.model_copy(update={"verdict": assumed, "verdict_reason": "not verified"})
        inv.trace.add("evidence", id=ev.id, agent=ev.agent, hypothesis=ev.hypothesis_id, claimed=ev.claimed,
                      summary=ev.summary, structural=ev.structural, structural_reason=ev.structural_reason,
                      verdict=ev.verdict, verdict_reason=ev.verdict_reason, precedes=ev.precedes_symptom,
                      usable=ev.usable, call_id=ev.call_id)
        out.append(ev)
        if ev.structural == "valid" and ev.verdict == "CONTRADICTS":
            inv.ledger.append(LedgerEntry(hypothesis_id=ev.hypothesis_id, evidence_id=ev.id))
    inv.evidence.extend(out)
    return out


def support(inv: Investigation, hid: str) -> tuple[int, int]:
    sup = sum(1 for e in inv.evidence if e.hypothesis_id == hid and e.usable and e.claimed == "supports")
    con = sum(1 for l in inv.ledger if l.hypothesis_id == hid)
    return sup, con


def ranking_by_evidence(inv: Investigation) -> list[str]:
    def key(h):
        s, c = support(inv, h.id)
        return (-(s - c), inv.hypotheses.index(h))
    return [h.id for h in sorted(inv.hypotheses, key=key)]


def evidence_margin(inv: Investigation, top: str) -> float:
    s, c = support(inv, top)
    others = [support(inv, h.id) for h in inv.hypotheses if h.id != top]
    best_other = max((a - b for a, b in others), default=0)
    return float((s - c) - best_other)


def board(inv: Investigation) -> str:
    """The critic's view: hypotheses with usable evidence, contradictions and rejected items."""
    lines = []
    for h in inv.hypotheses:
        s, c = support(inv, h.id)
        lines.append(f"{h.id}: {h.cause_type.value} in {h.service.value}. {h.description} "
                     f"[usable support {s}, verified contradictions {c}]")
        for e in inv.evidence:
            if e.hypothesis_id != h.id:
                continue
            if e.usable or (e.structural == "valid" and e.verdict == "CONTRADICTS"):
                tag = "SUPPORTS" if e.usable and e.claimed == "supports" else "CONTRADICTS"
                lines.append(f"   {e.id} [{tag}, {e.agent}] {e.summary}")
            else:
                why = e.structural_reason if e.structural != "valid" else (
                    f"verifier said {e.verdict}" if e.precedes_symptom is not False else "starts after the symptom")
                lines.append(f"   {e.id} [rejected: {why}] {e.summary}")
    return "\n".join(lines) or "(no hypotheses)"


def open_ledger(inv: Investigation, hid: str | None = None) -> list[LedgerEntry]:
    return [l for l in inv.ledger if l.resolved_by_critic is None and (hid is None or l.hypothesis_id == hid)]


def ledger_text(inv: Investigation) -> str:
    rows = open_ledger(inv)
    if not rows:
        return "(none)"
    ev = {e.id: e for e in inv.evidence}
    return "\n".join(f"{l.evidence_id} against {l.hypothesis_id}: {ev[l.evidence_id].summary}" for l in rows)


# ------------------------------------------------------------------ report
def preconditions(inv: Investigation, service: str) -> dict:
    b = inv.backend
    deploys = [d for d in b.deploys(b.now - 30 * 86400, b.now) if d["service"] == service]
    pool = [v for _, v in b.metric("db_pool_in_use", {"service": service}, b.now - 120, b.now)]
    return {"deploy_id": deploys[-1]["id"] if deploys else None,
            "previous_deploy_id": deploys[-2]["id"] if len(deploys) > 1 else None,
            "replicas": 1, "health": "degraded", "alert_firing": True,
            "pool_saturated": bool(pool and max(pool) >= 19)}


def finish(inv: Investigation, root: Hypothesis | None, ranking: list[str], signals: ConfidenceSignals,
           capped: bool = False, note: str = "") -> Report:
    signals.combined = gate.combined(signals, inv.policy["gate"].get("calibration"))
    if capped or root is None:
        result = "capped" if capped else "needs_review"
    else:
        result = gate.decide(signals, inv.policy, enabled=inv.cfg.gate)
    hyp = {h.id: h for h in inv.hypotheses}
    top = [hyp[i] for i in ranking if i in hyp][:3]
    action = None
    if result == "pass" and root is not None:
        ev_ids = [e.id for e in inv.evidence if e.hypothesis_id == root.id and e.usable]
        action = propose_action(root, ev_ids, preconditions(inv, root.service.value), datetime.now(timezone.utc),
                                inv.policy["actions"]["approval_ttl_seconds"], set(inv.policy["actions"]["allowlist"]))
        if action is not None:
            action = action.model_copy(update={"status": "awaiting_approval"})
    usable = [e.id for e in inv.evidence if e.usable and root is not None and e.hypothesis_id == root.id]
    summary = note or (f"Root cause: {root.cause_type.value} in {root.service.value}. {root.description}"
                       if root else "No root cause could be established.")
    rep = Report(investigation_id=inv.investigation_id, stage="preliminary", root_cause=root, top_hypotheses=top,
                 confidence=signals, gate_result=result, evidence_ids=usable,
                 open_contradictions=open_ledger(inv, root.id if root else None), proposed_action=action,
                 summary=summary[:1500],
                 timeline=[{"t": e["t"], "kind": e["kind"], "agent": e.get("agent", ""),
                            "what": e.get("tool") or e.get("id") or e.get("role", "")} for e in inv.trace.events
                           if e["kind"] in ("tool", "evidence", "decision")])
    inv.trace.add("decision", gate=result, root=root.id if root else None, signals=signals.model_dump())
    return rep


@dataclass
class Result:
    report: Report
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    ledger: list[LedgerEntry]
    trace: list[dict]
    usage: dict
    error: str | None = None


def result_of(inv: Investigation, report: Report, error: str | None = None) -> Result:
    return Result(report, inv.evidence, inv.hypotheses, inv.ledger, inv.trace.events, inv.usage.as_dict(), error)
