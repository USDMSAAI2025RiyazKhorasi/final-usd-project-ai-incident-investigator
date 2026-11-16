import json

import pytest

from agent.investigate import investigate
from agent.llm import OpenAICompatLLM, Usage, parse_json
from eval.config import load_all
from tools.backend import SnapshotBackend

CONFIGS = load_all()


@pytest.fixture(scope="module")
def snaps(snapshot_dir):
    return sorted(snapshot_dir.glob("*/sc-*"))


@pytest.mark.parametrize("cid", sorted(CONFIGS))
def test_every_config_produces_a_valid_report(cid, snaps, tmp_path):
    for snap in snaps[:3]:
        res = investigate(SnapshotBackend(snap), CONFIGS[cid], tmp_path / f"{cid}-{snap.name}")
        r = res.report
        assert r.gate_result in ("pass", "needs_review", "capped")
        assert (tmp_path / f"{cid}-{snap.name}" / "report.md").exists()
        if r.proposed_action is not None:
            assert r.gate_result == "pass" and r.proposed_action.status == "awaiting_approval"
        assert res.usage["tool_calls"] <= CONFIGS[cid].caps.tool_calls


def test_ablation_flags_change_behaviour(snaps, tmp_path):
    snap = snaps[0]
    run = lambda cid: investigate(SnapshotBackend(snap), CONFIGS[cid], tmp_path / cid)
    c2, a3, a4, a6, c0 = run("C2"), run("A3"), run("A4"), run("A6"), run("C0")
    roles = lambda r: set(r.usage["by_role"])
    assert "verifier" in roles(c2) and "critic" in roles(c2)
    assert "verifier" not in roles(run("A2"))
    assert all(e.structural_reason == "unchecked" for e in a3.evidence)
    assert "critic" not in roles(run("A1"))
    tools_used = [e["tool"] for e in a4.trace if e["kind"] == "tool"]
    assert "search_runbooks" not in tools_used
    assert {e.agent for e in a6.evidence} <= {"investigator"}
    assert roles(c0) == {"single"}
    assert run("A5").report.gate_result in ("pass", "capped")


def test_token_budget_caps_the_run(snaps, tmp_path):
    cfg = CONFIGS["C2"].model_copy(update={"caps": CONFIGS["C2"].caps.model_copy(update={"tokens": 3000})})
    res = investigate(SnapshotBackend(snaps[0]), cfg, tmp_path / "cap")
    assert res.report.gate_result == "capped" and res.report.proposed_action is None


def test_parse_json_tolerates_prose_and_fences():
    assert parse_json('ok: ```json\n{"a": [1, {"b": "}"}]}\n```') == {"a": [1, {"b": "}"}]}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_openai_compatible_request_shape(monkeypatch):
    seen = {}

    def fake_post(url, headers, body, **kw):
        seen.update(url=url, body=body)
        return {"choices": [{"message": {"content": '{"final": {}}'}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    monkeypatch.setattr("agent.llm._post", fake_post)
    u = Usage()
    llm = OpenAICompatLLM("m", "k", "http://x/v1", u)
    assert llm.complete("single", "sys", [{"role": "user", "content": "hi"}]) == '{"final": {}}'
    assert seen["url"] == "http://x/v1/chat/completions" and seen["body"]["messages"][0]["role"] == "system"
    assert u.tokens == 10 and u.llm_calls == 1
