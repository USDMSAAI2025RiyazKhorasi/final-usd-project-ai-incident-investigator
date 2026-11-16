from eval.tampering import blocked, cases


def test_all_tampering_cases_blocked(snapshot_dir):
    snap = sorted(snapshot_dir.glob("*/sc-*"))[0]
    rows = cases(snap)
    assert len(rows) == 10
    assert all(blocked(r) for _, r in rows), rows


def test_correct_action_resolves_and_wrong_action_does_not(snapshot_dir):
    from datetime import datetime, timezone

    import yaml

    from agent.gate import load_policy
    from agent.remediation.approval import ApprovalService
    from agent.remediation.environment import SimulatedEnvironment
    from agent.state import ActionProposal

    snap = sorted(snapshot_dir.glob("*/sc-*"))[0]
    fix = yaml.safe_load((snap / "scenario.yaml").read_text())["ground_truth"]["correct_fix"]
    users = {"alice": "approver"}
    now = datetime.now(timezone.utc)
    for action, target, expect in [(fix["action"], fix["target"], "resolved"), ("restart", "gateway", "not_effective")]:
        if (action, target) == (fix["action"], fix["target"]) and expect == "not_effective":
            continue
        env = SimulatedEnvironment(snap)
        st = env.state(target)
        params = {"to_deploy": st["previous_deploy_id"]} if action == "rollback" else {}
        p = ActionProposal(id=f"a-{target}", idempotency_key=f"k-{target}", action=action, target=target, params=params,
                           evidence_ids=[], preconditions={"deploy_id": st["deploy_id"], "replicas": st["replicas"]},
                           proposed_at=now, expires_at=now.replace(year=now.year + 1))
        svc = ApprovalService(load_policy(), users)
        svc.submit(p)
        svc.approve(p.id, "alice", now)
        assert svc.execute(p.id, env, now).status == expect
