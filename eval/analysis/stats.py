"""Statistics used by calibration and the results report. NumPy only.

risk_coverage   error rate among the answered cases as the confidence threshold falls
aurc            area under that curve (lower is better); compared against random and oracle
ece, brier      calibration of a probability-like score
fit_logistic    L2-regularised logistic regression (the combined confidence signal)
mcnemar         exact paired test on discordant pairs
bootstrap_ci    confidence interval that resamples scenario families, not single scenarios
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np


def risk_coverage(conf, correct) -> tuple[np.ndarray, np.ndarray]:
    conf, correct = np.asarray(conf, float), np.asarray(correct, bool)
    order = np.argsort(-conf, kind="stable")
    err = (~correct[order]).astype(float)
    k = np.arange(1, len(err) + 1)
    return k / len(err), np.cumsum(err) / k


def aurc(conf, correct) -> float:
    _, risk = risk_coverage(conf, correct)
    return float(risk.mean()) if len(risk) else float("nan")


def oracle_aurc(correct) -> float:
    correct = np.asarray(correct, bool)
    return aurc(correct.astype(float), correct)


def selective_accuracy(conf, correct, coverage: float = 0.8) -> float:
    cov, risk = risk_coverage(conf, correct)
    i = max(0, int(math.ceil(coverage * len(cov))) - 1)
    return float(1 - risk[i]) if len(risk) else float("nan")


def threshold_for_risk(conf, correct, target_risk: float) -> tuple[float, float, float]:
    """Lowest threshold whose answered set has risk <= target. Returns (threshold, coverage, risk)."""
    conf, correct = np.asarray(conf, float), np.asarray(correct, bool)
    best = (float(conf.max()) if len(conf) else 1.0, 0.0, 0.0)
    for t in sorted(set(conf.tolist())):
        ans = conf >= t
        if not ans.any():
            continue
        risk = float((~correct[ans]).mean())
        if risk <= target_risk and ans.mean() > best[1]:
            best = (t, float(ans.mean()), risk)
    return best


def ece(prob, correct, bins: int = 5) -> float:
    prob, correct = np.asarray(prob, float), np.asarray(correct, float)
    edges = np.linspace(0, 1, bins + 1)
    total = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (prob >= lo) & ((prob < hi) if hi < 1 else (prob <= hi))
        if m.any():
            total += m.mean() * abs(prob[m].mean() - correct[m].mean())
    return float(total)


def brier(prob, correct) -> float:
    prob, correct = np.asarray(prob, float), np.asarray(correct, float)
    return float(((prob - correct) ** 2).mean())


def reliability(prob, correct, bins: int = 5) -> list[tuple[float, float, int]]:
    prob, correct = np.asarray(prob, float), np.asarray(correct, float)
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (prob >= lo) & ((prob < hi) if hi < 1 else (prob <= hi))
        if m.any():
            out.append((float(prob[m].mean()), float(correct[m].mean()), int(m.sum())))
    return out


def fit_logistic(X, y, l2: float = 1.0, iters: int = 3000, lr: float = 0.1) -> tuple[np.ndarray, float]:
    X, y = np.asarray(X, float), np.asarray(y, float)
    w, b = np.zeros(X.shape[1]), 0.0
    n = len(y)
    for _ in range(iters):
        z = np.clip(X @ w + b, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = p - y
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, float(b)


def mcnemar(a_correct, b_correct) -> dict:
    """Exact two-sided McNemar test on paired outcomes (same scenarios, same run index)."""
    a, b = np.asarray(a_correct, bool), np.asarray(b_correct, bool)
    n01 = int((~a & b).sum())     # a wrong, b right
    n10 = int((a & ~b).sum())     # a right, b wrong
    n = n01 + n10
    if n == 0:
        return {"b_only": n01, "a_only": n10, "p": 1.0}
    k = min(n01, n10)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"b_only": n01, "a_only": n10, "p": p}


def bootstrap_ci(values, families, iters: int = 2000, seed: int = 590, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile CI for a mean, resampling whole families (near-duplicate scenarios)."""
    groups = defaultdict(list)
    for v, f in zip(values, families):
        groups[f].append(float(v))
    keys = list(groups)
    if not keys:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(iters):
        pick = rng.integers(0, len(keys), len(keys))
        vals = [x for i in pick for x in groups[keys[i]]]
        means.append(np.mean(vals))
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def bootstrap_diff_ci(a, b, families, iters: int = 2000, seed: int = 590) -> tuple[float, float]:
    """CI for mean(b) - mean(a) on paired values, resampling families."""
    return bootstrap_ci(np.asarray(b, float) - np.asarray(a, float), families, iters, seed)
