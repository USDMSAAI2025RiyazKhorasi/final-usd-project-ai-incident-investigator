"""Structural evidence checks (Jira AII-8). Deterministic, no LLM.

Asks: is this evidence item well-formed and traceable? It does NOT ask whether
the evidence supports the hypothesis; that is agent/validation/semantic.py.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from agent.state import Evidence

# numbers like 4.2, 180, 95%, 50ms and anything in quotes
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
_QUOTED = re.compile(r"[\"“']([^\"”']{3,})[\"”']")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def quoted_values(summary: str) -> list[str]:
    """Values a summary claims came from the data: quoted strings and numbers."""
    quotes = _QUOTED.findall(summary)
    rest = _QUOTED.sub(" ", summary)
    return quotes + _NUM.findall(rest)


def check(
    ev: Evidence,
    *,
    hypothesis_ids: set[str],
    alert_window: tuple[datetime, datetime],
    margin: timedelta,
    load_raw: Callable[[str], str] = lambda ref: Path(ref).read_text(),
    rerun_hash: Callable[[Evidence], str] | None = None,
    require_grounded_values: bool = True,
) -> Evidence:
    """Return a copy of `ev` with `structural` set to valid or invalid, plus a reason."""

    def fail(reason: str) -> Evidence:
        return ev.model_copy(update={"structural": "invalid", "structural_reason": reason})

    p = ev.provenance
    if not (p.tool and p.raw_ref and p.raw_hash):
        return fail("provenance incomplete")
    if not p.ok:
        return fail("tool call failed or timed out")
    if ev.hypothesis_id not in hypothesis_ids:
        return fail(f"unknown hypothesis id {ev.hypothesis_id}")

    try:
        raw = load_raw(p.raw_ref)
    except OSError:
        return fail("raw result not found")
    if not raw.strip():
        return fail("empty result")
    if sha256(raw) != p.raw_hash:
        return fail("raw result does not match recorded hash")
    if rerun_hash is not None and rerun_hash(ev) != p.raw_hash:
        return fail("re-run in snapshot mode gives a different result")

    start, end = alert_window
    if p.data_end < start - margin or p.data_start > end:
        return fail("data outside the alert window")

    if require_grounded_values:
        missing = [v for v in quoted_values(ev.summary) if v not in raw]
        if missing:
            return fail(f"values not found in raw result: {missing[:3]}")

    return ev.model_copy(update={"structural": "valid", "structural_reason": None})
