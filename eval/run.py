"""Run one configuration on one split, then score it (Jira AII-10, AII-32).

    python -m eval.run --config eval/configs/C2.yaml --split dev
    python -m eval.run --config eval/configs/C0.yaml --split test --workers 4

Output: results/runs/<config>-<split>/<scenario>/r<run>/ (report, evidence, trace, usage, raw)
and results/runs/<config>-<split>/scores.jsonl. Finished investigations are skipped,
so an interrupted run resumes where it stopped.

The test split is refused unless `python -m eval.freeze --check` passes.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agent.gate import load_policy
from agent.investigate import investigate
from agent.llm import from_env
from eval import freeze
from eval.config import load as load_config
from eval.score import score_run
from tools.backend import SnapshotBackend

ROOT = Path(__file__).resolve().parents[1]


def run(config: str, split: str, snapshots: Path, out_root: Path, workers: int = 1, limit: int | None = None,
        runs: int | None = None, allow_unfrozen: bool = False) -> Path:
    cfg = load_config(config)
    if split == "test" and not allow_unfrozen:
        problems = freeze.check(snapshots)
        if problems:
            sys.exit("refusing to run on test:\n  " + "\n  ".join(problems))
    snaps = sorted((snapshots / split).glob("sc-*"))[:limit]
    n_runs = runs or cfg.runs_per_case
    out = out_root / f"{cfg.id}-{split}"
    out.mkdir(parents=True, exist_ok=True)
    policy = load_policy()
    llm = from_env()
    (out / "run.json").write_text(json.dumps({"config": cfg.model_dump(), "split": split, "runs": n_runs,
                                              "snapshots": str(snapshots), "provider": llm.name,
                                              "model": llm.model, "policy_gate": policy["gate"],
                                              "frozen": not freeze.check(snapshots)}, indent=1, default=str))
    jobs = [(s, r) for s in snaps for r in range(1, n_runs + 1)
            if not (out / s.name / f"r{r}" / "report.json").exists()]

    def one(job):
        snap, r = job
        dest = out / snap.name / f"r{r}"
        try:
            res = investigate(SnapshotBackend(snap), cfg, dest, policy=policy, llm=llm,
                              investigation_id=f"{cfg.id}-{snap.name}-r{r}")
            return f"{snap.name} r{r}: {res.report.gate_result} " + (
                f"{res.report.root_cause.cause_type.value}@{res.report.root_cause.service.value}" if res.report.root_cause else "-")
        except Exception as e:                                   # keep going; record the failure
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "error.txt").write_text(traceback.format_exc())
            return f"{snap.name} r{r}: ERROR {e}"

    print(f"{cfg.id} on {split}: {len(jobs)} investigations to run ({len(snaps)} scenarios x {n_runs} runs, "
          f"provider {llm.name})")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, msg in enumerate(as_completed([pool.submit(one, j) for j in jobs]), 1):
            print(f"[{i}/{len(jobs)}] {msg.result()}", flush=True)
    n = score_run(out, snapshots)
    print(f"scored {n} investigations -> {out / 'scores.jsonl'}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--split", choices=["train", "dev", "test"], required=True)
    ap.add_argument("--snapshots", type=Path, default=ROOT / "snapshots")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "runs")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--runs", type=int, default=None, help="override runs_per_case")
    ap.add_argument("--allow-unfrozen", action="store_true", help="debugging only; marks results as unfrozen")
    a = ap.parse_args()
    run(a.config, a.split, a.snapshots, a.out, a.workers, a.limit, a.runs, a.allow_unfrozen)
