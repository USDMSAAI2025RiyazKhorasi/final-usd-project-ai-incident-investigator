"""Run one investigation on one snapshot (or a live alert) and save everything it produced.

    python -m agent.investigate --snapshot snapshots/dev/sc-031 --config eval/configs/C2.yaml

Writes to the output folder: report.json, report.md, evidence.json, trace.jsonl,
usage.json and raw/ (every raw tool result, hashed).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent import gate
from agent.common import Investigation, Result
from agent.graph_multi import run_multi
from agent.graph_single import run_single
from agent.llm import LLM, Usage, from_env
from agent.report import render_markdown
from tools.backend import SnapshotBackend
from tools.base import RawStore
from tools.toolkit import ALL_TOOLS, Toolkit


def investigate(backend, cfg, out_dir: Path, policy: dict | None = None, llm: LLM | None = None,
                investigation_id: str | None = None) -> Result:
    policy = policy or gate.load_policy()
    usage = Usage(max_tokens=cfg.caps.tokens, max_tool_calls=cfg.caps.tool_calls, max_seconds=cfg.caps.wall_seconds)
    if llm is None:
        llm = from_env(usage)
    else:
        llm = llm.with_usage(usage)
    usage.price_in = usage.price_in or getattr(llm.usage, "price_in", 0)
    allowed = set(ALL_TOOLS) - (set() if cfg.runbook_search else {"search_runbooks"})
    toolkit = Toolkit(backend, RawStore(out_dir / "raw"), allowed=allowed)
    inv = Investigation(investigation_id or out_dir.name, cfg, policy, llm, toolkit, usage)
    result = run_single(inv) if cfg.architecture == "single" else run_multi(inv)
    save(result, out_dir, cfg, llm)
    return result


def save(result: Result, out: Path, cfg, llm: LLM) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(result.report.model_dump_json(indent=2))
    (out / "evidence.json").write_text(json.dumps([e.model_dump(mode="json") for e in result.evidence], indent=1))
    (out / "hypotheses.json").write_text(json.dumps([h.model_dump(mode="json") for h in result.hypotheses], indent=1))
    (out / "ledger.json").write_text(json.dumps([l.model_dump(mode="json") for l in result.ledger], indent=1))
    (out / "trace.jsonl").write_text("".join(json.dumps(e, default=str) + "\n" for e in result.trace))
    (out / "usage.json").write_text(json.dumps({**result.usage, "config": cfg.id, "provider": llm.name,
                                                "model": llm.model, "error": result.error}, indent=1))
    (out / "report.md").write_text(render_markdown(result))


if __name__ == "__main__":
    from eval.config import load as load_config
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--config", default="eval/configs/C2.yaml")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    out = Path(a.out or f"results/adhoc/{Path(a.snapshot).name}-{cfg.id}")
    r = investigate(SnapshotBackend(a.snapshot), cfg, out)
    print((out / "report.md").read_text())
