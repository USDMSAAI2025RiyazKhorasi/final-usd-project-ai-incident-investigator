"""The whole experiment in the order the PRD requires, as one resumable command.

    python -m eval.experiment                 # everything
    python -m eval.experiment --steps dev,calibrate,freeze
    python -m eval.experiment --limit 10      # quick smoke run on 10 scenarios per split

Steps:
  dataset    generate 120 scenarios and their telemetry snapshots (skipped if present)
  dev        C0 and C2 on dev, 3 runs each (tuning and calibration data)
  calibrate  fit the confidence gate on C2-dev and write agent/policy.yaml
  freeze     hash configs, prompts and policy; seal the test snapshots
  test       C0 x3, C2 x3, C1 and A1-A6 x1 on test (refused unless frozen)
  remediate  15 end-to-end remediation runs with approval and verification
  tamper     the 10 approval-tampering cases
  report     results/report-test.md and figures
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = ["dataset", "dev", "calibrate", "freeze", "test", "remediate", "tamper", "report"]
TEST_CONFIGS = ["C0", "C2", "C1", "A1", "A2", "A3", "A4", "A5", "A6"]


def sh(*args: str) -> None:
    print("$", " ".join(args), flush=True)
    subprocess.run([sys.executable, "-m", *args], cwd=ROOT, check=True)


def main(steps: list[str], limit: int | None, workers: int) -> None:
    lim = ["--limit", str(limit)] if limit else []
    w = ["--workers", str(workers)]
    if "dataset" in steps and not (ROOT / "snapshots" / "test").exists():
        sh("scenarios.generator", "--out", "scenarios/generated", "--seed", "590")
        sh("recorder.simulate", "--scenarios", "scenarios/generated", "--out", "snapshots")
    if "dev" in steps:
        for c in ("C0", "C2"):
            sh("eval.run", "--config", f"eval/configs/{c}.yaml", "--split", "dev", *w, *lim)
    if "calibrate" in steps:
        sh("eval.calibrate", "--run", "results/runs/C2-dev")
    if "freeze" in steps:
        sh("eval.freeze")
    if "test" in steps:
        for c in TEST_CONFIGS:
            sh("eval.run", "--config", f"eval/configs/{c}.yaml", "--split", "test", *w, *lim)
    if "remediate" in steps:
        sh("eval.remediate", "--config", "eval/configs/C2.yaml", "--split", "test", "--limit", str(min(15, limit or 15)))
    if "tamper" in steps:
        sh("eval.tampering")
    if "report" in steps:
        sh("eval.analysis.results", "--split", "test")
        print(f"\nreport: {ROOT / 'results' / 'report-test.md'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default=",".join(STEPS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    main([s.strip() for s in a.steps.split(",")], a.limit, a.workers)
