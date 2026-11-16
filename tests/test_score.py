from datetime import datetime, timedelta, timezone

from agent.state import ConfidenceSignals, Evidence, Hypothesis, Provenance, Report
from eval.score import score
from scenarios.generator import generate


def test_score_joint_and_evidence():
    sc = next(s for s in generate(590) if s.fault == "bad_config_deploy")
    gt = sc.ground_truth
    h = Hypothesis(id="h1", service=gt.root_cause_service, cause_type=gt.root_cause_type, description="x")
    t = datetime(2026, 11, 10, tzinfo=timezone.utc)
    deploy = next(e for e in gt.expected_evidence if e.startswith("deploy:")).split(":")[1]
    cited = [Evidence(id="e1", agent="changes", hypothesis_id="h1", claimed="supports",
                      summary=f"deploy {deploy} at 14:03", provenance=Provenance(
                          tool="list_deploys", args={}, called_at=t, data_start=t,
                          data_end=t + timedelta(minutes=1), raw_ref="r", raw_hash="h", mode="snapshot"))]
    rep = Report(investigation_id="i", stage="preliminary", root_cause=h, top_hypotheses=[h],
                 confidence=ConfidenceSignals(verbal=0.8), gate_result="pass", evidence_ids=["e1"],
                 open_contradictions=[], proposed_action=None, summary="")
    row = score(rep, sc, cited, cost_usd=0.1, latency_s=60, tokens=1000)
    assert row["joint_correct"] and row["top3_correct"]
    assert row["evidence_precision"] == 1.0 and row["evidence_recall"] == 0.5
