import pytest

from eval.config import ExperimentConfig, check_parity, load_all


def test_all_configs_load_and_share_budget(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test-model")
    cfgs = load_all()
    assert set(cfgs) == {"C0", "C1", "C2", "A1", "A2", "A3", "A4", "A5", "A6"}
    check_parity(cfgs)


def test_each_ablation_differs_from_c2_in_one_place(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m")
    cfgs = load_all()
    flags = ["architecture", "critic", "structural_checks", "semantic_verifier", "runbook_search", "gate"]
    c2 = cfgs["C2"]
    for k in ["A1", "A2", "A4", "A5", "A6"]:
        diff = [f for f in flags if getattr(cfgs[k], f) != getattr(c2, f)]
        assert len(diff) == 1, (k, diff)


def test_semantic_requires_structural():
    with pytest.raises(ValueError):
        ExperimentConfig(id="x", description="", architecture="multi", critic=True,
                         structural_checks=False, semantic_verifier=True, runbook_search=True,
                         gate=True, runs_per_case=1, model="m", temperature=0,
                         caps={"tool_calls": 1, "tokens": 1, "wall_seconds": 1}, seeds=[1])
