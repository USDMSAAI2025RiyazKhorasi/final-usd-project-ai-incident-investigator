"""Human-readable reports: preliminary (at diagnosis) and final (after remediation)."""
from __future__ import annotations


def _ev_lines(result, ids: set[str] | None = None) -> list[str]:
    out = []
    for e in result.evidence:
        if ids is not None and e.id not in ids:
            continue
        status = "usable" if e.usable else (e.structural_reason if e.structural != "valid"
                                            else f"verifier: {e.verdict}" if e.verdict not in ("SUPPORTS", "CONTRADICTS") or not e.usable
                                            else "rejected")
        out.append(f"- **{e.id}** ({e.agent}, {e.claimed} {e.hypothesis_id}, {status}): {e.summary} "
                   f"_[{e.provenance.tool} {e.call_id or ''}]_")
    return out


def render_markdown(result, outcome: dict | None = None) -> str:
    r = result.report
    stage = "Final incident report" if outcome else "Preliminary investigation report"
    lines = [f"# {stage}: {r.investigation_id}", ""]
    if r.root_cause:
        h = r.root_cause
        lines += [f"**Root cause:** {h.cause_type.value.replace('_', ' ')} in **{h.service.value}**. {h.description}"]
    else:
        lines += ["**Root cause:** not established."]
    c = r.confidence
    fmt = lambda x: "n/a" if x is None else f"{x:.2f}"
    lines += ["", f"**Gate:** {r.gate_result}  |  confidence: stated {fmt(c.verbal)}, agreement {fmt(c.self_consistency)}, "
                  f"evidence margin {fmt(c.evidence_margin)}, calibrated {fmt(c.combined)}", ""]
    if r.top_hypotheses:
        lines += ["## Hypotheses (ranked)"] + [f"{i + 1}. {h.cause_type.value} in {h.service.value} ({h.id}): {h.description}"
                                               for i, h in enumerate(r.top_hypotheses)] + [""]
    lines += ["## Evidence"] + (_ev_lines(result) or ["(none)"]) + [""]
    if r.open_contradictions:
        lines += ["## Open contradictions"] + [f"- {l.evidence_id} against {l.hypothesis_id}" for l in r.open_contradictions] + [""]
    a = r.proposed_action
    if a:
        lines += ["## Proposed action", f"{a.action} **{a.target.value}** {a.params or ''} - status {a.status}, "
                  f"expires {a.expires_at:%H:%M:%S} UTC, needs role `{a.required_role}`", ""]
    elif r.gate_result != "pass":
        lines += ["## Proposed action", "None: confidence below the gate threshold. Needs human review.", ""]
    lines += ["## Summary", r.summary or "", ""]
    if outcome:
        lines += ["## Outcome"] + [f"- {k}: {v}" for k, v in outcome.items()] + [""]
    u = result.usage
    lines += [f"_Cost: {u['tokens']} tokens, {u['llm_calls']} model calls, {u['tool_calls']} tool calls, "
              f"US${u['cost_usd']:.4f}, {u['latency_s']:.1f}s_"]
    return "\n".join(lines)
