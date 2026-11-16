"""Single-agent baseline (C0; C1 adds evidence validation and the gate).

One reason-act agent with every read-only tool, the same model, snapshot and caps
as the multi-agent graph. Its cited evidence goes through exactly the same
validation code as the multi-agent system when the config turns validation on.
"""
from __future__ import annotations

from agent.common import (Investigation, Result, add_hypothesis, evidence_margin, finish, make_evidence, prompt,
                          react, result_of, tools_for, validate)
from agent.llm import BudgetExceeded, parse_json
from agent.state import ConfidenceSignals
from tools.toolkit import ALL_TOOLS


def _key(d: dict) -> tuple[str, str]:
    return (str(d.get("service", "")).lower(), str(d.get("cause_type", "")).lower())


def run_single(inv: Investigation) -> Result:
    cfg = inv.cfg
    permitted = tools_for(cfg, ALL_TOOLS)
    max_steps = cfg.caps.tool_calls
    system = prompt("single", tools=inv.toolkit.specs(permitted), max_steps=max_steps)
    a = inv.alert
    task = (f"Alert {a['labels']['alertname']} fired on {a['labels']['service']}: {a['annotations']['summary']}. "
            "Investigate and report the root cause.")
    try:
        final, messages = react(inv, "single", "single", system, task, permitted, max_steps)
        root = add_hypothesis(inv, final.get("root_cause") or {}, "single")
        hmap = {"root": root.id if root else "", "h1": root.id if root else ""}
        for i, alt in enumerate((final.get("alternatives") or [])[:2], start=1):
            h = add_hypothesis(inv, alt, "single") if isinstance(alt, dict) else None
            if h:
                hmap[f"alt{i}"] = h.id
        validate(inv, make_evidence(inv, final.get("evidence") or [], "single", hmap))

        try:
            verbal = min(1.0, max(0.0, float(final.get("confidence"))))
        except (TypeError, ValueError):
            verbal = None
        signals = ConfidenceSignals(verbal=verbal)
        if root is not None:
            signals.evidence_margin = evidence_margin(inv, root.id)
        # C1 only: the gate needs self-consistency, so re-sample the final answer twice
        if cfg.gate and root is not None and len(messages) >= 2:
            agree = 1
            for k in range(2):
                msgs = [dict(m) for m in messages[:-1]]
                msgs[-1]["content"] += "\n\nReply now with your final JSON answer."
                text = inv.llm.complete("single", system, msgs, temperature=inv.policy["critic"]["sample_temperature"],
                                        context={"role": "single", "agent": "single", "toolkit": inv.toolkit,
                                                 "phase": "final", "messages": msgs, "sample": k + 1,
                                                 "temperature": 0.7, "permitted": permitted, "steps": max_steps})
                try:
                    alt_final = parse_json(text).get("final", {})
                    agree += _key(alt_final.get("root_cause") or {}) == (root.service.value, root.cause_type.value)
                except (ValueError, AttributeError):
                    pass
            signals.self_consistency = agree / 3
        ranking = [h.id for h in inv.hypotheses]
        rep = finish(inv, root, ranking, signals, note=str(final.get("summary", ""))[:1500])
        return result_of(inv, rep)
    except BudgetExceeded as e:
        rep = finish(inv, None, [h.id for h in inv.hypotheses], ConfidenceSignals(), capped=True, note=str(e))
        return result_of(inv, rep, error=str(e))
