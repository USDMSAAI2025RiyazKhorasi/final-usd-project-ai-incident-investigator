from datetime import datetime, timezone

import pytest

from agent.remediation.rules import RULES, load_allowlist, propose_action
from agent.state import CauseType, Hypothesis, Service
from scenarios.generator import generate

NOW = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
PRE = {"deploy_id": "d-7f3", "previous_deploy_id": "d-7f2", "replicas": 1, "alert_firing": True}


def hyp(service, cause):
    return Hypothesis(id="h1", service=service, cause_type=cause, description="test")


@pytest.mark.parametrize("cause,action", [
    (CauseType.config_change, "rollback"),
    (CauseType.code_change, "rollback"),
    (CauseType.resource_exhaustion, "restart"),
])
def test_cause_maps_to_action(cause, action):
    p = propose_action(hyp(Service.payments, cause), ["e1"], PRE, NOW)
    assert p.action == action and p.target == Service.payments


def test_saturated_pool_scales_up():
    p = propose_action(hyp(Service.orders, CauseType.resource_exhaustion), [], {**PRE, "pool_saturated": True}, NOW)
    assert p.action == "scale" and p.params == {"replicas": 2}


def test_dependency_down_restarts_the_dependency():
    p = propose_action(hyp(Service.notifications, CauseType.dependency_unavailable), [], PRE, NOW)
    assert p.action == "restart" and p.target == Service.redis


def test_no_action_when_nothing_to_roll_back_to():
    assert propose_action(hyp(Service.payments, CauseType.config_change), [], {"deploy_id": "d-1"}, NOW) is None


def test_no_action_outside_allowlist():
    assert propose_action(hyp(Service.payments, CauseType.code_change), [], PRE, NOW, allowlist={"restart"}) is None


def test_expiry_and_idempotency():
    a = propose_action(hyp(Service.payments, CauseType.code_change), [], PRE, NOW)
    b = propose_action(hyp(Service.payments, CauseType.code_change), [], PRE, NOW)
    assert (a.expires_at - a.proposed_at).total_seconds() == 900
    assert a.idempotency_key == b.idempotency_key and a.id != b.id


def test_every_rule_is_allowlisted():
    assert set(RULES.values()) <= load_allowlist()


def test_matches_catalogue_fix_for_every_scenario():
    """The rule table must reproduce each scenario's ground-truth fix."""
    for s in generate(590):
        gt = s.ground_truth
        pre = {**PRE, "pool_saturated": s.fault == "pool_exhausted"}
        p = propose_action(hyp(gt.root_cause_service, gt.root_cause_type), [], pre, NOW)
        assert (p.action, p.target) == (gt.correct_fix.action, gt.correct_fix.target), s.fault
