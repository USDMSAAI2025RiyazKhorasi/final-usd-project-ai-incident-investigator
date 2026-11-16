import json

import numpy as np
import pytest

from eval.analysis.stats import aurc, bootstrap_ci, ece, fit_logistic, mcnemar, oracle_aurc, threshold_for_risk


def test_aurc_orders_and_bounds():
    correct = [1, 1, 1, 0, 0]
    good = [0.9, 0.8, 0.7, 0.2, 0.1]
    bad = [0.1, 0.2, 0.3, 0.8, 0.9]
    assert aurc(good, correct) == pytest.approx(oracle_aurc(correct))
    assert aurc(bad, correct) > aurc(good, correct)


def test_mcnemar_exact():
    r = mcnemar([1, 1, 0, 0, 0, 0, 0, 0], [1, 1, 1, 1, 1, 1, 1, 1])
    assert r["b_only"] == 6 and r["a_only"] == 0 and r["p"] == pytest.approx(2 / 2 ** 6)


def test_threshold_meets_target_risk():
    t, cov, risk = threshold_for_risk([0.9, 0.8, 0.7, 0.6, 0.5], [1, 1, 1, 0, 0], 0.15)
    assert t == 0.7 and cov == 0.6 and risk == 0.0


def test_logistic_separates():
    X = np.array([[0.9], [0.8], [0.2], [0.1]] * 10)
    y = np.array([1, 1, 0, 0] * 10)
    w, b = fit_logistic(X, y, l2=0.1, iters=5000, lr=0.5)
    assert w[0] > 0


def test_bootstrap_resamples_families():
    lo, hi = bootstrap_ci([1, 1, 0, 0], ["a", "a", "b", "b"], iters=500)
    assert lo == 0.0 and hi == 1.0


def test_ece_perfectly_calibrated():
    assert ece([1.0, 1.0, 0.0, 0.0], [1, 1, 0, 0]) == 0.0


def test_calibrate_writes_gate(tmp_path):
    from eval.calibrate import calibrate
    rng = np.random.default_rng(0)
    rows = []
    for i in range(60):
        ok = i % 3 != 0
        rows.append({"joint_correct": ok, "capped": False, "conf_verbal": float(np.clip(0.7 + (0.2 if ok else -0.2) + rng.normal(0, 0.1), 0, 1)),
                     "conf_self_consistency": 1.0 if ok else 2 / 3, "conf_evidence_margin": 3.0 if ok else 0.0})
    (tmp_path / "scores.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    pol = tmp_path / "policy.yaml"
    from agent.gate import POLICY
    pol.write_text(POLICY.read_text())
    res = calibrate(tmp_path, policy_path=pol)
    assert res["calibration"] and res["dev_risk"] <= 0.15
    import yaml
    assert yaml.safe_load(pol.read_text())["gate"]["signal"] == res["signal"]
