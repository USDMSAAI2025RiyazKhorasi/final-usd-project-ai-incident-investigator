"""Exploratory data analysis of the scenario dataset (report section: Dataset and EDA).

    python -m eval.analysis.eda --snapshots snapshots --out results/eda

Uses ground truth (fault start, root cause) because this is analysis, not an agent.
Writes figures (PNG) and stats.json with every number quoted in the report.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml

from tools.backend import ts as parse_ts

SERVICES = ["gateway", "orders", "payments", "inventory", "notifications"]
CALLER = {"orders": "gateway", "payments": "orders", "inventory": "orders", "notifications": "orders"}
SIGNALS = {  # plain-language name: how to derive it
    "Error rate": "err", "Response time (p95)": "p95", "CPU use": "cpu", "Memory": "mem",
    "DB connections in use": "pool", "DB query time": "dbq", "Dependency reachable": "dep", "Free disk": "disk",
}
plt.rcParams.update({"font.size": 9, "axes.spines.top": False, "axes.spines.right": False})
BLUE, ORANGE, GREY, RED = "#2a6fb0", "#e08a2c", "#9aa5b1", "#c0392b"


def ts(s: str) -> float:
    return parse_ts(s)


def load(snap: Path) -> dict:
    sc = yaml.safe_load((snap / "scenario.yaml").read_text())
    w = json.loads((snap / "window.json").read_text())
    fault_at = ts(json.loads((snap / "fault_window.json").read_text())["fault_at"])
    t = np.arange(ts(w["baseline_start"]), ts(w["end"]) + 1, 5.0)
    idx = {int(x): i for i, x in enumerate(t)}
    raw = {}
    for f in (snap / "metrics").glob("*.json"):
        for r in json.loads(f.read_text())["result"]:
            m = r["metric"]
            arr = np.full(len(t), np.nan)
            for tt, v in r["values"]:
                if int(tt) in idx:
                    arr[idx[int(tt)]] = float(v)
            raw[(m["__name__"], m["service"], m.get("status"))] = arr

    def ffill(a: np.ndarray) -> np.ndarray:
        a = a.copy()
        for i in range(1, len(a)):
            if np.isnan(a[i]):
                a[i] = a[i - 1]
        if np.isnan(a[0]):
            first = np.flatnonzero(~np.isnan(a))
            a[: first[0] if len(first) else len(a)] = a[first[0]] if len(first) else 0
        return a

    def rate(c: np.ndarray) -> np.ndarray:
        """Per-interval increase of a counter; gaps filled, resets treated as a fresh start."""
        c = ffill(c)
        d = np.diff(c, prepend=c[0])
        d[d < 0] = c[d < 0]
        return d

    sig: dict[tuple[str, str], np.ndarray] = {}
    for s in SERVICES:
        d_ok, d_bad = rate(raw[("http_requests_total", s, "2xx")]), rate(raw[("http_requests_total", s, "5xx")])
        sig[(s, "err")] = np.where(d_ok + d_bad > 0, d_bad / np.maximum(d_ok + d_bad, 1e-9), 0)
        sig[(s, "rps")] = (d_ok + d_bad) / 5
        sig[(s, "p95")] = ffill(raw[("http_request_duration_seconds", s, None)])
        sig[(s, "cpu")] = rate(raw[("process_cpu_seconds_total", s, None)]) / 5
        sig[(s, "mem")] = ffill(raw[("container_memory_bytes", s, None)])
        for key, name in [("pool", "db_pool_in_use"), ("dbq", "db_query_seconds"), ("dep", "dependency_up"),
                          ("disk", "disk_free_bytes")]:
            if (name, s, None) in raw:
                sig[(s, key)] = ffill(raw[(name, s, None)])
    for k in sig:                                  # first sample of a diffed counter is undefined
        if k[1] in ("err", "rps", "cpu"):
            sig[k][0] = sig[k][1]
    logs = {s: [json.loads(l) for l in (snap / "logs" / f"{s}.jsonl").read_text().splitlines()] for s in SERVICES}
    deploys = json.loads((snap / "deploys.json").read_text())
    return dict(sc=sc, t=t, fault_at=fault_at, alert_at=ts(w["alert_at"]), start=ts(w["baseline_start"]),
                end=ts(w["end"]), sig=sig, logs=logs, deploys=deploys, raw=raw)


def zchange(x: np.ndarray, t: np.ndarray, fault_at: float) -> float:
    pre, post = x[t < fault_at], x[t >= fault_at]
    sd = pre.std() if pre.std() > 1e-9 else max(abs(pre.mean()) * 0.01, 1e-9)
    return float((np.median(post) - pre.mean()) / sd)


def onset(x: np.ndarray, t: np.ndarray, baseline_end: float, z: float = 8, sustain: int = 3) -> float | None:
    """First time after baseline_end that a signal leaves the band set by data before baseline_end."""
    pre = x[t < baseline_end]
    sd = pre.std() if pre.std() > 1e-9 else max(abs(pre.mean()) * 0.01, 1e-9)
    sd = max(sd, abs(pre.mean()) * 0.05)               # ignore departures under 5% of the level
    out = (np.abs(x - pre.mean()) > z * sd) & (t >= baseline_end)
    run = np.convolve(out.astype(int), np.ones(sustain, int), "valid") == sustain   # sustained departure
    hit = np.flatnonzero(run)
    return float(t[hit[0]]) if len(hit) else None


def main(snap_root: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    data = [load(p) for p in sorted(snap_root.glob("*/sc-*"))]
    st: dict = {"n_scenarios": len(data)}

    # ---------------------------------------------------------------- size and quality
    st["series_per_scenario"] = len(data[0]["raw"])
    st["samples_per_series_min"] = int(min(len(d["t"]) for d in data))
    st["samples_per_series_max"] = int(max(len(d["t"]) for d in data))
    st["window_minutes_min"] = round(min((d["end"] - d["start"]) / 60 for d in data), 1)
    st["window_minutes_max"] = round(max((d["end"] - d["start"]) / 60 for d in data), 1)
    st["missing_values"] = int(sum(np.isnan(v).sum() for d in data for v in d["raw"].values()))
    st["scenarios_with_gaps"] = int(sum(any(np.isnan(v).any() for v in d["raw"].values()) for d in data))
    n_logs = [sum(len(v) for v in d["logs"].values()) for d in data]
    st["log_lines_total"] = int(sum(n_logs))
    st["log_lines_per_scenario_median"] = int(np.median(n_logs))
    lv = Counter(l["level"] for d in data for v in d["logs"].values() for l in v)
    st["log_levels"] = dict(lv)
    import re
    templ = Counter(re.sub(r"\d+", "#", re.sub(r"/api\S*|POST \S+", "ROUTE", l["message"]))
                    for d in data for v in d["logs"].values() for l in v if l["level"] != "INFO")
    st["distinct_nonroutine_log_templates"] = len(templ)
    # counter resets: memory drops sharply (restart) while the request counter keeps rising
    resets, restarts = 0, 0
    for d in data:
        for s in SERVICES:
            m = d["sig"][(s, "mem")]
            if (np.diff(m) < -0.3 * m[:-1]).any():
                restarts += 1
                c = d["raw"][("http_requests_total", s, "2xx")]
                c = c[~np.isnan(c)]
                resets += int((np.diff(c) < 0).any())
    st["restarts_seen"] = restarts
    st["counter_resets_after_restart"] = resets
    st["scale_ranges"] = {
        "Memory (MB)": [round(min(d["sig"][(s, "mem")].min() for d in data for s in SERVICES) / 2**20),
                        round(max(d["sig"][(s, "mem")].max() for d in data for s in SERVICES) / 2**20)],
        "Response time p95 (s)": [round(min(d["sig"][(s, "p95")].min() for d in data for s in SERVICES), 3),
                                  round(max(d["sig"][(s, "p95")].max() for d in data for s in SERVICES), 2)],
        "Error rate": [0, round(max(d["sig"][(s, "err")].max() for d in data for s in SERVICES), 2)],
        "Free disk (GB)": [0, round(max(d["sig"][("notifications", "disk")].max() for d in data) / 1e9, 1)],
    }

    # ---------------------------------------------------------------- labels
    sc = [d["sc"] for d in data]
    st["cause_type_counts"] = dict(Counter(x["ground_truth"]["root_cause_type"] for x in sc))
    st["root_service_counts"] = dict(Counter(x["ground_truth"]["root_cause_service"] for x in sc))
    st["alert_counts"] = dict(Counter(x["alert_name"] for x in sc))
    by_split = defaultdict(Counter)
    for x in sc:
        by_split[x["ground_truth"]["root_cause_type"]][x["split"]] += 1
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.2), dpi=180)
    causes = sorted(st["cause_type_counts"], key=lambda k: -st["cause_type_counts"][k])
    left = np.zeros(len(causes))
    for split, col in [("train", BLUE), ("dev", ORANGE), ("test", GREY)]:
        v = np.array([by_split[c][split] for c in causes])
        ax[0].barh([c.replace("_", " ") for c in causes], v, left=left, color=col, label=split)
        left += v
    ax[0].invert_yaxis(); ax[0].legend(frameon=False, fontsize=8); ax[0].set_xlabel("scenarios")
    ax[0].set_title("Root-cause category", loc="left", fontweight="bold")
    svcs = sorted(st["root_service_counts"], key=lambda k: -st["root_service_counts"][k])
    ax[1].barh(svcs, [st["root_service_counts"][s] for s in svcs], color=BLUE)
    ax[1].invert_yaxis(); ax[1].set_xlabel("scenarios")
    ax[1].set_title("Root-cause service", loc="left", fontweight="bold")
    plt.tight_layout(); plt.savefig(out / "fig1_labels.png"); plt.close()

    # ---------------------------------------------------------------- signal vs fault (root service)
    faults = sorted({x["fault"] for x in sc})
    keys = list(SIGNALS.items())
    mat = np.full((len(faults), len(keys)), np.nan)
    for i, f in enumerate(faults):
        rows = [d for d in data if d["sc"]["fault"] == f]
        for j, (_, k) in enumerate(keys):
            vals = [zchange(d["sig"][(d["sc"]["target"], k)], d["t"], d["fault_at"])
                    for d in rows if (d["sc"]["target"], k) in d["sig"]]
            if vals:
                mat[i, j] = np.median(vals)
    st["zchange_matrix"] = {f: {n: (None if np.isnan(mat[i, j]) else round(float(mat[i, j]), 1))
                                for j, (n, _) in enumerate(keys)} for i, f in enumerate(faults)}
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=180)
    show = np.sign(mat) * np.log10(1 + np.abs(mat))
    im = ax.imshow(show, cmap="RdBu_r", vmin=-3, vmax=3, aspect="auto")
    ax.set_xticks(range(len(keys)), [k for k, _ in keys], rotation=30, ha="right")
    ax.set_yticks(range(len(faults)), [f.replace("_", " ") for f in faults])
    for i in range(len(faults)):
        for j in range(len(keys)):
            if not np.isnan(mat[i, j]):
                v = mat[i, j]
                ax.text(j, i, f"{v:.0f}" if abs(v) >= 10 else f"{v:.1f}", ha="center", va="center", fontsize=7,
                        color="white" if abs(show[i, j]) > 1.8 else "black")
            else:
                ax.text(j, i, "n/a", ha="center", va="center", fontsize=7, color=GREY)
    cb = plt.colorbar(im, ax=ax, fraction=0.03); cb.set_label("signed log10(1+|z|)")
    ax.set_title("Change after the fault on the root-cause service (median z-score vs. its own baseline)",
                 loc="left", fontweight="bold", fontsize=9)
    plt.tight_layout(); plt.savefig(out / "fig2_signal_by_fault.png"); plt.close()

    # ---------------------------------------------------------------- simple non-LLM baselines
    res = defaultdict(lambda: Counter())
    for d in data:
        x = d["sc"]; root = x["ground_truth"]["root_cause_service"]; diff = x["difficulty"]
        res[diff]["n"] += 1
        res[diff]["alert"] += x["alert_service"] == root
        # Only what is visible at alert time: baseline = the first 50 s of the window
        # (always before the fault), onset = first departure after that. No knowledge of the fault start.
        first = {}
        for s in SERVICES:
            times = [onset(d["sig"][(s, k)], d["t"], d["start"] + 50) for k in SIGNALS.values() if (s, k) in d["sig"]]
            times = [t for t in times if t is not None and t <= d["alert_at"]]
            if times:
                first[s] = min(times)
        pred = min(first, key=first.get) if first else x["alert_service"]
        res[diff]["earliest"] += pred == root
        depth = {"gateway": 0, "orders": 1, "payments": 2, "inventory": 2, "notifications": 2}
        if first:
            m = min(first.values())
            tied = [s for s, t in first.items() if t == m]
            res[diff]["ties"] += len(tied) > 1
            pred2 = max(tied, key=lambda s: depth[s])
        else:
            pred2 = x["alert_service"]
        res[diff]["earliest_downstream"] += pred2 == root
        res[diff]["deploy_near"] += any(abs(ts(dp["at"]) - d["fault_at"]) < 180 for dp in d["deploys"])
    st["baselines"] = {k: {m: round(v[m] / v["n"], 2) for m in ("alert", "earliest", "earliest_downstream", "ties", "deploy_near")} | {"n": v["n"]}
                       for k, v in res.items()}
    tot = Counter()
    for v in res.values():
        tot.update(v)
    st["baselines"]["all"] = {m: round(tot[m] / tot["n"], 2) for m in ("alert", "earliest", "earliest_downstream", "ties", "deploy_near")} | {"n": tot["n"]}

    # deploy near onset vs cause type: is "a deploy happened" informative?
    dep_tab = defaultdict(Counter)
    for d in data:
        near = any(abs(ts(dp["at"]) - d["fault_at"]) < 180 for dp in d["deploys"])
        cat = "deploy-caused" if d["sc"]["ground_truth"]["root_cause_type"] in (
            "config_change", "code_change", "credential_failure") else "not deploy-caused"
        dep_tab[cat]["deploy within 3 min" if near else "no deploy"] += 1
    st["deploy_vs_cause"] = {k: dict(v) for k, v in dep_tab.items()}

    fig, ax = plt.subplots(figsize=(8, 3.4), dpi=180)
    diffs = ["easy", "medium", "hard", "all"]
    w = 0.27
    for off, m, col, lab in [(-w, "alert", GREY, "Blame the alerting service"),
                             (0, "earliest", ORANGE, "First service to look abnormal (ties broken arbitrarily)"),
                             (w, "earliest_downstream", BLUE, "First to look abnormal, ties go to the most downstream")]:
        vals = [st["baselines"][k][m] * 100 for k in diffs]
        bars = ax.bar(np.arange(4) + off, vals, w, color=col, label=lab)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v + 1.5, f"{v:.0f}%", ha="center", fontsize=7)
    ax.set_xticks(range(4), [f"{k}\n(n={st['baselines'][k]['n']})" for k in diffs])
    ax.set_ylabel("root-cause service correct (%)"); ax.set_ylim(0, 150); ax.set_yticks([0, 25, 50, 75, 100])
    ax.legend(frameon=False, fontsize=7.5, loc="upper left", ncol=1)
    ax.set_title("Two simple rules for finding the faulty service", loc="left", fontweight="bold")
    plt.tight_layout(); plt.savefig(out / "fig3_simple_rules.png"); plt.close()

    # ---------------------------------------------------------------- correlations
    # (a) between signals: scenario-level change on the root-cause service
    names = ["Error rate", "Response time (p95)", "CPU use", "Memory", "Requests per second"]
    ks = ["err", "p95", "cpu", "mem", "rps"]
    X = np.array([[zchange(d["sig"][(d["sc"]["target"], k)], d["t"], d["fault_at"]) for k in ks] for d in data])
    Xr = np.argsort(np.argsort(X, axis=0), axis=0)          # Spearman: rank then Pearson
    C1 = np.corrcoef(Xr.T)
    # (b) within a scenario over time: caller vs callee error rate and latency
    pair_corr = defaultdict(list)
    for d in data:
        tgt = d["sc"]["target"]
        if tgt in CALLER:
            c = CALLER[tgt]
            for k in ("err", "p95"):
                a, b = d["sig"][(tgt, k)], d["sig"][(c, k)]
                if a.std() > 0 and b.std() > 0:
                    pair_corr[k].append(np.corrcoef(a, b)[0, 1])
        # unrelated pair: two services that are neither target nor its caller
        others = [s for s in SERVICES if s not in (tgt, CALLER.get(tgt))]
        a, b = d["sig"][(others[0], "err")], d["sig"][(others[1], "err")]
        if a.std() > 0 and b.std() > 0:
            pair_corr["unrelated_err"].append(np.corrcoef(a, b)[0, 1])
    st["pair_corr_median"] = {k: round(float(np.median(v)), 2) for k, v in pair_corr.items()}
    # (c) error rate vs latency over time within root service
    within = [np.corrcoef(d["sig"][(d["sc"]["target"], "err")], d["sig"][(d["sc"]["target"], "p95")])[0, 1]
              for d in data if d["sig"][(d["sc"]["target"], "err")].std() > 0]
    st["err_vs_latency_within_median"] = round(float(np.nanmedian(within)), 2)
    st["signal_corr"] = {names[i]: {names[j]: round(float(C1[i, j]), 2) for j in range(len(names))} for i in range(len(names))}

    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8), dpi=180, gridspec_kw={"width_ratios": [1.1, 1]})
    im = ax[0].imshow(C1, cmap="RdBu_r", vmin=-1, vmax=1)
    ax[0].set_xticks(range(5), names, rotation=30, ha="right"); ax[0].set_yticks(range(5), names)
    for i in range(5):
        for j in range(5):
            ax[0].text(j, i, f"{C1[i, j]:.2f}", ha="center", va="center", fontsize=7.5,
                       color="white" if abs(C1[i, j]) > 0.6 else "black")
    ax[0].set_title("Between signals (Spearman, n=120)", loc="left", fontweight="bold")
    plt.colorbar(im, ax=ax[0], fraction=0.04)
    labels = ["Error rate:\nfaulty service vs caller", "Response time:\nfaulty service vs caller",
              "Error rate:\ntwo unrelated services"]
    vals = [pair_corr["err"], pair_corr["p95"], pair_corr["unrelated_err"]]
    bp = ax[1].boxplot(vals, vert=False, widths=0.5, patch_artist=True)
    for patch, col in zip(bp["boxes"], [BLUE, BLUE, GREY]):
        patch.set_facecolor(col); patch.set_alpha(0.5)
    ax[1].set_yticks([1, 2, 3], labels); ax[1].set_xlabel("correlation over time within a scenario (Pearson)")
    ax[1].axvline(0, color="#ccc", lw=0.8); ax[1].invert_yaxis()
    ax[1].set_title("Between services", loc="left", fontweight="bold")
    plt.tight_layout(); plt.savefig(out / "fig4_correlations.png"); plt.close()

    # ---------------------------------------------------------------- example scenario (hard: downstream alert)
    ex = next(d for d in data if d["sc"]["noise"] == "downstream_alert" and d["sc"]["fault"] == "bad_config_deploy") \
        if any(d["sc"]["noise"] == "downstream_alert" and d["sc"]["fault"] == "bad_config_deploy" for d in data) \
        else next(d for d in data if d["sc"]["noise"] == "downstream_alert")
    x = ex["sc"]; tgt = x["target"]; c = CALLER[tgt]
    fig, ax = plt.subplots(2, 1, figsize=(8, 4.4), dpi=180, sharex=True)
    tm = (ex["t"] - ex["start"]) / 60
    for s, col in [(tgt, RED), (c, BLUE)]:
        ax[0].plot(tm, ex["sig"][(s, "err")] * 100, color=col, lw=1.2, label=f"{s} ({'root cause' if s == tgt else 'alerting caller'})")
        ax[1].plot(tm, ex["sig"][(s, "p95")], color=col, lw=1.2)
    for a in ax:
        a.axvline((ex["fault_at"] - ex["start"]) / 60, color="k", ls="--", lw=0.8)
        a.axvline((ex["alert_at"] - ex["start"]) / 60, color=ORANGE, lw=1)
    ax[0].set_ylabel("errors (%)"); ax[1].set_ylabel("p95 response (s)"); ax[1].set_xlabel("minutes from window start")
    ax[0].legend(frameon=False, fontsize=8)
    ax[0].set_title(f"Example {x['id']}: {x['fault'].replace('_', ' ')}, alert fires on the caller "
                    f"(dashed = fault starts, orange = alert)", loc="left", fontweight="bold", fontsize=9)
    st["example_id"] = x["id"]
    plt.tight_layout(); plt.savefig(out / "fig5_example.png"); plt.close()

    (out / "stats.json").write_text(json.dumps(st, indent=2, default=str))
    print(json.dumps(st, indent=1, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=Path, default=Path("snapshots"))
    ap.add_argument("--out", type=Path, default=Path("results/eda"))
    a = ap.parse_args()
    main(a.snapshots, a.out)
