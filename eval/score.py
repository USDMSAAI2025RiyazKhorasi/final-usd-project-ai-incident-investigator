"""Score reports against ground truth (Jira AII-10).

One row per investigation. Aggregates, significance tests and risk-coverage
curves live in eval/analysis (AII-40).

    python -m eval.score --run results/runs/<run_id>
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.state import Evidence, Report
from scenarios.schema import Scenario


def evidence_signature_hits(cited: list[Evidence], expected: list[str]) -> set[str]:
    """Which expected signatures ('log:timeout after', 'deploy:d-7f3') the cited evidence covers."""
    hits = set()
    for sig in expected:
        kind, _, needle = sig.partition(":")
        for ev in cited:
            text = f"{ev.summary} {ev.provenance.args}"
            if needle.lower() in text.lower():
                hits.add(sig)
    return hits


def score(report: Report, scenario: Scenario, cited: list[Evidence], cost_usd: float,
          latency_s: float, tokens: int) -> dict:
    gt = scenario.ground_truth
    rc = report.root_cause
    service_ok = bool(rc and rc.service == gt.root_cause_service)
    type_ok = bool(rc and rc.cause_type == gt.root_cause_type)
    top3_ok = any(h.service == gt.root_cause_service and h.cause_type == gt.root_cause_type
                  for h in report.top_hypotheses[:3])
    hits = evidence_signature_hits(cited, gt.expected_evidence)
    matched_items = [e for e in cited if evidence_signature_hits([e], gt.expected_evidence)]
    fix = report.proposed_action
    return {
        "scenario": scenario.id,
        "family": scenario.family,
        "split": scenario.split,
        "fault": scenario.fault,
        "difficulty": scenario.difficulty,
        "held_out_fault": scenario.held_out_fault,
        "pred_service": rc.service.value if rc else None,
        "pred_type": rc.cause_type.value if rc else None,
        "true_service": gt.root_cause_service.value,
        "true_type": gt.root_cause_type.value,
        "service_correct": service_ok,
        "type_correct": type_ok,
        "joint_correct": service_ok and type_ok,          # primary metric
        "top3_correct": top3_ok,
        "gate_result": report.gate_result,
        "answered": report.gate_result == "pass",
        "conf_verbal": report.confidence.verbal,
        "conf_self_consistency": report.confidence.self_consistency,
        "conf_evidence_margin": report.confidence.evidence_margin,
        "conf_combined": report.confidence.combined,
        "evidence_precision": len(matched_items) / len(cited) if cited else None,
        "evidence_recall": len(hits) / len(gt.expected_evidence) if gt.expected_evidence else None,
        "fix_correct": bool(fix and fix.action == gt.correct_fix.action
                            and fix.target == gt.correct_fix.target),
        "cost_usd": cost_usd,
        "latency_s": latency_s,
        "tokens": tokens,
    }


def score_run(run_dir: Path, snapshots: Path) -> int:
    """Score every finished investigation under run_dir; write scores.jsonl. Returns the count."""
    import yaml
    split = json.loads((run_dir / "run.json").read_text())["split"]
    cfg_id = json.loads((run_dir / "run.json").read_text())["config"]["id"]
    rows = []
    for rep_path in sorted(run_dir.glob("sc-*/r*/report.json")):
        inv = rep_path.parent
        sid, run_no = inv.parent.name, int(inv.name[1:])
        scenario = Scenario(**yaml.safe_load((snapshots / split / sid / "scenario.yaml").read_text()))
        report = Report.model_validate_json(rep_path.read_text())
        evidence = [Evidence(**e) for e in json.loads((inv / "evidence.json").read_text())]
        usage = json.loads((inv / "usage.json").read_text())
        cited = [e for e in evidence if e.id in set(report.evidence_ids)]
        row = score(report, scenario, cited, usage["cost_usd"], usage["latency_s"], usage["tokens"])
        row.update(config=cfg_id, run=run_no, intensity=scenario.intensity, noise=scenario.noise,
                   llm_calls=usage["llm_calls"], tool_calls=usage["tool_calls"], error=usage.get("error"),
                   evidence_items=len(evidence),
                   evidence_rejected=sum(1 for e in evidence if not e.usable),
                   capped=report.gate_result == "capped")
        rows.append(row)
    (run_dir / "scores.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
    return len(rows)


def main(run_dir: Path, snapshots: Path) -> None:
    n = score_run(run_dir, snapshots)
    print(f"scored {n} investigations -> {run_dir / 'scores.jsonl'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--snapshots", type=Path, default=Path("snapshots"))
    a = ap.parse_args()
    main(a.run, a.snapshots)
