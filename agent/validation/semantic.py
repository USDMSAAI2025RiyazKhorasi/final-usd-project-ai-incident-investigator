"""Semantic verifier: one LLM call per structurally valid evidence item.

The verifier sees only the hypothesis and an excerpt of the raw tool result, never
the specialist's summary or reasoning, so it cannot simply agree with how the
specialist framed the evidence.
"""
from __future__ import annotations

import json

from agent.llm import parse_json

LABELS = {"SUPPORTS", "CONTRADICTS", "IRRELEVANT", "INSUFFICIENT"}


def excerpt(raw: dict, limit: int = 1800) -> str:
    """Compact view of a raw tool result: stats and patterns first, long arrays trimmed."""
    view = dict(raw)
    if "points" in view:
        pts = view["points"]
        view["points"] = pts[:3] + [["...", f"{len(pts) - 6} more"]] + pts[-3:] if len(pts) > 8 else pts
    if "lines" in view:
        view["lines"] = view["lines"][:5]
    text = json.dumps(view, default=str)
    return text if len(text) <= limit else text[:limit] + " ...(truncated)"


def verify(llm, prompt_fn, hypothesis, evidence, raw: dict) -> tuple[str, str]:
    system = prompt_fn("verifier", cause_type=hypothesis.cause_type.value, service=hypothesis.service.value,
                       description=hypothesis.description, tool=evidence.provenance.tool,
                       args=json.dumps(evidence.provenance.args), excerpt=excerpt(raw))
    text = llm.complete("verifier", system, [{"role": "user", "content": "Classify the evidence."}],
                        max_tokens=200, context={"hypothesis": hypothesis, "evidence": evidence, "raw": raw})
    try:
        obj = parse_json(text)
        label = str(obj.get("label", "")).upper()
        return (label if label in LABELS else "INSUFFICIENT"), str(obj.get("reason", ""))[:300]
    except ValueError:
        return "INSUFFICIENT", "verifier reply was not valid JSON"
