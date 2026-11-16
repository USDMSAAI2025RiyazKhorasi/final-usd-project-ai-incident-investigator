"""Fit the confidence gate on the dev split only (Jira AII-29).

    python -m eval.calibrate --run results/runs/C2-dev

1. Fits the combined signal: L2 logistic regression over (verbal, self-consistency,
   tanh(evidence margin / 2)) predicting whether the root cause was right.
2. Computes each signal's AURC on dev and picks the lowest.
3. Picks the lowest threshold whose dev risk is at or below gate.target_risk.
4. Writes signal, threshold and coefficients into agent/policy.yaml.

Run eval/freeze.py afterwards; thresholds must not change once test is opened.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import yaml

from agent.gate import POLICY
from eval.analysis.stats import aurc, fit_logistic, threshold_for_risk

SIGNALS = ["verbal", "self_consistency", "evidence_margin", "combined"]


def feats(r: dict) -> list[float]:
    v = 0.5 if r["conf_verbal"] is None else r["conf_verbal"]
    sc = v if r["conf_self_consistency"] is None else r["conf_self_consistency"]
    m = 0.0 if r["conf_evidence_margin"] is None else math.tanh(r["conf_evidence_margin"] / 2)
    return [v, sc, m]


def calibrate(run_dir: Path, policy_path: Path = POLICY, write: bool = True) -> dict:
    rows = [json.loads(l) for l in (run_dir / "scores.jsonl").read_text().splitlines()]
    rows = [r for r in rows if not r["capped"]]
    y = [bool(r["joint_correct"]) for r in rows]
    X = [feats(r) for r in rows]
    policy = yaml.safe_load(policy_path.read_text())
    target = policy["gate"]["target_risk"]
    result = {"n": len(rows), "accuracy": sum(y) / len(y) if y else None, "aurc": {}}
    if len(set(y)) > 1:
        w, b = fit_logistic(X, y, l2=1.0)
        cal = {"features": ["verbal", "self_consistency", "tanh(evidence_margin/2)"],
               "coef": [round(float(c), 4) for c in w], "intercept": round(b, 4), "fitted_on": str(run_dir),
               "n": len(rows)}
        comb = [1 / (1 + math.exp(-(b + sum(c * x for c, x in zip(w, f))))) for f in X]
    else:
        cal, comb = None, None          # one class only: nothing to learn from
    scores = {"verbal": [f[0] for f in X], "self_consistency": [f[1] for f in X],
              "evidence_margin": [f[2] for f in X]}
    if comb:
        scores["combined"] = comb
    for name, s in scores.items():
        result["aurc"][name] = round(aurc(s, y), 4)
    best = min(result["aurc"], key=lambda k: (result["aurc"][k], k != "combined"))
    t, cov, risk = threshold_for_risk(scores[best], y, target)
    result.update(signal=best, threshold=round(t, 4), dev_coverage=round(cov, 3), dev_risk=round(risk, 3),
                  calibration=cal)
    if write:
        policy["gate"].update(signal=best, threshold=round(float(t), 4), calibration=cal)
        header = "# Guardrail settings. Gate values were written by eval/calibrate.py from the dev split.\n"
        policy_path.write_text(header + yaml.safe_dump(policy, sort_keys=False))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, default=Path("results/runs/C2-dev"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(json.dumps(calibrate(a.run, write=not a.dry_run), indent=1))
