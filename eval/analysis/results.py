"""Build the results report from scored runs (Jira AII-40, AII-41).

    python -m eval.analysis.results --split test

Reads results/runs/<config>-<split>/scores.jsonl for every config present and writes
results/report-<split>.md, tables (CSV) and figures (PNG, if matplotlib is installed).
Every number in the final report should come from this script.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from eval.analysis.stats import (aurc, bootstrap_ci, bootstrap_diff_ci, brier, ece, mcnemar, oracle_aurc,
                                 reliability, risk_coverage, selective_accuracy)

ROOT = Path(__file__).resolve().parents[2]
ORDER = ["C0", "C0-SC", "C1", "C2", "A1", "A2", "A3", "A4", "A5", "A6"]
NAMES = {"C0": "single agent", "C0-SC": "single agent, majority of 3 runs", "C1": "single agent + guardrails",
         "C2": "full multi-agent system", "A1": "C2 without critic", "A2": "C2 structural checks only",
         "A3": "C2 without validation", "A4": "C2 without runbook search", "A5": "C2 without gate",
         "A6": "C2 with specialists merged"}


def load(split: str, runs_root: Path) -> tuple[dict[str, list[dict]], dict]:
    data, meta = {}, {}
    for f in sorted(runs_root.glob(f"*-{split}/scores.jsonl")):
        cid = f.parent.name.rsplit("-", 1)[0]
        rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        if not rows:
            continue
        data[cid] = rows
        meta[cid] = json.loads((f.parent / "run.json").read_text())
    if "C0" in data and len({r["run"] for r in data["C0"]}) >= 3:
        data["C0-SC"] = majority(data["C0"])
    return data, meta


def majority(rows: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[r["scenario"]].append(r)
    out = []
    for sid, rs in by.items():
        votes = Counter((r["pred_service"], r["pred_type"]) for r in rs)
        (svc, typ), _ = votes.most_common(1)[0]
        base = dict(rs[0])
        base.update(run=1, pred_service=svc, pred_type=typ, service_correct=svc == base["true_service"],
                    type_correct=typ == base["true_type"],
                    joint_correct=(svc, typ) == (base["true_service"], base["true_type"]),
                    cost_usd=sum(r["cost_usd"] for r in rs), tokens=sum(r["tokens"] for r in rs),
                    latency_s=sum(r["latency_s"] for r in rs), answered=True)
        out.append(base)
    return out


def pct(x) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{100 * x:.1f}%"


def mean(rows, key) -> float:
    v = [r[key] for r in rows if r.get(key) is not None]
    return float(np.mean(v)) if v else float("nan")


def main_table(data: dict) -> list[dict]:
    out = []
    for cid in [c for c in ORDER if c in data] + [c for c in data if c not in ORDER]:
        rows = data[cid]
        joint = [float(r["joint_correct"]) for r in rows]
        lo, hi = bootstrap_ci(joint, [r["family"] for r in rows])
        answered = [r for r in rows if r["answered"]]
        correct = sum(joint)
        cost = sum(r["cost_usd"] for r in rows)
        out.append({"config": cid, "description": NAMES.get(cid, cid), "n": len(rows),
                    "runs": len({r["run"] for r in rows}), "joint_acc": np.mean(joint), "ci_low": lo, "ci_high": hi,
                    "service_acc": mean(rows, "service_correct"), "type_acc": mean(rows, "type_correct"),
                    "top3_acc": mean(rows, "top3_correct"), "coverage": len(answered) / len(rows),
                    "answered_acc": mean(answered, "joint_correct") if answered else float("nan"),
                    "evidence_precision": mean(rows, "evidence_precision"), "evidence_recall": mean(rows, "evidence_recall"),
                    "tokens": mean(rows, "tokens"), "latency_s": mean(rows, "latency_s"), "cost_usd": mean(rows, "cost_usd"),
                    "cost_per_correct": cost / correct if correct else float("nan"),
                    "capped": sum(bool(r.get("capped")) for r in rows)})
    return out


def paired(data: dict, a: str, b: str) -> dict | None:
    if a not in data or b not in data:
        return None
    A = {(r["scenario"], r["run"]): r for r in data[a]}
    B = {(r["scenario"], r["run"]): r for r in data[b]}
    keys = sorted(set(A) & set(B))
    if not keys:                       # different run counts: pair run 1 with run 1
        A1 = {r["scenario"]: r for r in data[a] if r["run"] == 1}
        B1 = {r["scenario"]: r for r in data[b] if r["run"] == 1}
        keys = [(s, 1) for s in sorted(set(A1) & set(B1))]
        A, B = {(s, 1): A1[s] for s, _ in keys}, {(s, 1): B1[s] for s, _ in keys}
    ac = [bool(A[k]["joint_correct"]) for k in keys]
    bc = [bool(B[k]["joint_correct"]) for k in keys]
    lo, hi = bootstrap_diff_ci([float(x) for x in ac], [float(x) for x in bc], [A[k]["family"] for k in keys])
    return {"a": a, "b": b, "pairs": len(keys), "acc_a": np.mean(ac), "acc_b": np.mean(bc),
            "diff": np.mean(bc) - np.mean(ac), "ci_low": lo, "ci_high": hi, **mcnemar(ac, bc)}


def signals_table(rows: list[dict]) -> list[dict]:
    y = [bool(r["joint_correct"]) for r in rows if not r.get("capped")]
    rows = [r for r in rows if not r.get("capped")]
    out = []
    for name, key in [("stated confidence", "conf_verbal"), ("self-consistency", "conf_self_consistency"),
                      ("evidence margin", "conf_evidence_margin"), ("calibrated combination", "conf_combined")]:
        s = [r[key] for r in rows]
        if any(v is None for v in s) or not s:
            continue
        s = np.array(s, float)
        prob = s if key != "conf_evidence_margin" else np.tanh(s / 2) / 2 + 0.5
        out.append({"signal": name, "key": key, "aurc": aurc(s, y), "sel_acc_80": selective_accuracy(s, y, 0.8),
                    "ece": ece(prob, y) if key != "conf_evidence_margin" else float("nan"),
                    "brier": brier(prob, y) if key != "conf_evidence_margin" else float("nan")})
    err = 1 - np.mean(y) if y else float("nan")
    out.append({"signal": "random order (reference)", "key": None, "aurc": err, "sel_acc_80": 1 - err,
                "ece": float("nan"), "brier": float("nan")})
    out.append({"signal": "oracle (reference)", "key": None, "aurc": oracle_aurc(y) if y else float("nan"),
                "sel_acc_80": float("nan"), "ece": float("nan"), "brier": float("nan")})
    return out


def breakdown(data: dict, key: str, configs=("C0", "C2")) -> list[dict]:
    out = []
    groups = sorted({r[key] for c in configs if c in data for r in data[c]}, key=str)
    for g in groups:
        row = {key: g}
        for c in configs:
            if c in data:
                rs = [r for r in data[c] if r[key] == g]
                row[c] = f"{sum(r['joint_correct'] for r in rs)}/{len(rs)}"
        out.append(row)
    return out


def md_table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    head = "| " + " | ".join(h for _, h in cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = ""
    for r in rows:
        cells = []
        for k, _ in cols:
            v = r.get(k, "")
            if isinstance(v, float):
                v = "n/a" if np.isnan(v) else (pct(v) if k.endswith(("acc", "coverage", "precision", "recall", "_80"))
                                               or k in ("ci_low", "ci_high", "diff", "acc_a", "acc_b")
                                               else f"{v:.4f}" if k in ("aurc", "ece", "brier", "p", "cost_usd", "cost_per_correct")
                                               else f"{v:,.0f}" if k == "tokens" else f"{v:.2f}")
            cells.append(str(v))
        body += "| " + " | ".join(cells) + " |\n"
    return head + body


def figures(data: dict, out: Path, split: str) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    made = []
    tab = main_table(data)
    fig, ax = plt.subplots(figsize=(7.5, 3.2), dpi=150)
    xs = np.arange(len(tab))
    acc = [t["joint_acc"] * 100 for t in tab]
    err = [[100 * (t["joint_acc"] - t["ci_low"]) for t in tab], [100 * (t["ci_high"] - t["joint_acc"]) for t in tab]]
    ax.bar(xs, acc, color=["#2a6fb0" if t["config"] == "C2" else "#9aa5b1" for t in tab])
    ax.errorbar(xs, acc, yerr=err, fmt="none", ecolor="#333", capsize=3, lw=1)
    ax.set_xticks(xs, [t["config"] for t in tab]); ax.set_ylabel("joint accuracy (%)"); ax.set_ylim(0, 105)
    ax.set_title(f"Root-cause accuracy by configuration ({split}), 95% CI by family", loc="left", fontsize=9)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    plt.tight_layout(); plt.savefig(out / f"fig_accuracy_{split}.png"); plt.close(); made.append(f"fig_accuracy_{split}.png")
    if "C2" in data:
        rows = [r for r in data["C2"] if not r.get("capped")]
        y = [bool(r["joint_correct"]) for r in rows]
        fig, ax = plt.subplots(figsize=(5.5, 3.4), dpi=150)
        for name, key, col in [("stated", "conf_verbal", "#e08a2c"), ("self-consistency", "conf_self_consistency", "#3a9a5b"),
                               ("evidence margin", "conf_evidence_margin", "#7a5fb0"), ("calibrated", "conf_combined", "#2a6fb0")]:
            s = [r[key] for r in rows]
            if s and all(v is not None for v in s):
                cov, risk = risk_coverage(s, y)
                ax.plot(cov * 100, risk * 100, label=f"{name} (AURC {aurc(s, y):.3f})", color=col, lw=1.4)
        ax.axhline((1 - np.mean(y)) * 100 if y else 0, color="#999", ls="--", lw=1, label="random order")
        ax.set_xlabel("coverage: share of incidents answered (%)"); ax.set_ylabel("risk: error rate (%)")
        ax.set_title(f"C2 risk-coverage ({split})", loc="left", fontsize=9); ax.legend(fontsize=7, frameon=False)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        plt.tight_layout(); plt.savefig(out / f"fig_risk_coverage_{split}.png"); plt.close()
        made.append(f"fig_risk_coverage_{split}.png")
    return made


def build(split: str, runs_root: Path = ROOT / "results" / "runs", out: Path = ROOT / "results") -> Path:
    data, meta = load(split, runs_root)
    if not data:
        raise SystemExit(f"no scored runs for split {split} under {runs_root}")
    out.mkdir(parents=True, exist_ok=True)
    providers = sorted({f"{m['provider']} / {m['model'] or '-'}" for m in meta.values()})
    frozen = all(m.get("frozen") for m in meta.values())
    tab = main_table(data)
    lines = [f"# Results: {split} split", ""]
    if any(m["provider"] == "offline" for m in meta.values()):
        lines += ["> **Pipeline check only.** These runs used the offline rule-based stand-in, not a language model. "
                  "They show the harness works end to end; they are not research results.", ""]
    lines += [f"Model: {', '.join(providers)}. Frozen configuration: {'yes' if frozen else 'NO'}. "
              f"Scenarios: {len({r['scenario'] for rs in data.values() for r in rs})}.", "",
              "## RQ1: accuracy by configuration", "",
              md_table(tab, [("config", "Config"), ("description", "What it is"), ("n", "n"), ("joint_acc", "Joint acc"),
                             ("ci_low", "95% CI low"), ("ci_high", "95% CI high"), ("service_acc", "Service"),
                             ("type_acc", "Category"), ("top3_acc", "Top-3"), ("coverage", "Answered"),
                             ("answered_acc", "Acc. when answered")]), ""]
    comps = [paired(data, "C0", "C2"), paired(data, "C0-SC", "C2"), paired(data, "C1", "C2")] + \
            [paired(data, "C2", a) for a in ("A1", "A2", "A3", "A4", "A5", "A6")]
    comps = [c for c in comps if c]
    if comps:
        lines += ["Paired comparisons (same scenarios and run index; McNemar exact test; CI resamples families):", "",
                  md_table(comps, [("a", "A"), ("b", "B"), ("pairs", "Pairs"), ("acc_a", "Acc A"), ("acc_b", "Acc B"),
                                   ("diff", "B - A"), ("ci_low", "CI low"), ("ci_high", "CI high"),
                                   ("a_only", "Only A right"), ("b_only", "Only B right"), ("p", "p")]), ""]
    lines += ["## RQ2: evidence quality", "",
              md_table(tab, [("config", "Config"), ("evidence_precision", "Evidence precision"),
                             ("evidence_recall", "Evidence recall")]), ""]
    if "C2" in data:
        lines += ["## RQ3: confidence signals on C2", "",
                  md_table(signals_table(data["C2"]), [("signal", "Signal"), ("aurc", "AURC (lower better)"),
                                                       ("sel_acc_80", "Accuracy at 80% coverage"), ("ece", "ECE"),
                                                       ("brier", "Brier")]), ""]
        comb = [r for r in data["C2"] if r.get("conf_combined") is not None]
        if comb:
            rel = reliability([r["conf_combined"] for r in comb], [r["joint_correct"] for r in comb])
            lines += ["Reliability of the calibrated signal (mean predicted, observed accuracy, n): " +
                      "; ".join(f"{p:.2f}/{o:.2f}/{n}" for p, o, n in rel), ""]
    lines += ["## Cost", "", md_table(tab, [("config", "Config"), ("tokens", "Tokens/inv."), ("latency_s", "Seconds/inv."),
                                             ("cost_usd", "US$/inv."), ("cost_per_correct", "US$ per correct"),
                                             ("capped", "Hit a cap")]), ""]
    for key, title in [("fault", "By fault"), ("difficulty", "By difficulty"), ("held_out_fault", "Unseen faults")]:
        lines += [f"## {title} (correct / total)", "",
                  md_table(breakdown(data, key), [(key, key)] + [(c, c) for c in ("C0", "C2") if c in data]), ""]
    rem = sorted((ROOT / "results" / "remediation").glob(f"summary-*-{split}.json"))
    if rem:
        s = json.loads(rem[-1].read_text())["summary"]
        lines += ["## Remediation", "", ", ".join(f"{k}: {v}" for k, v in s.items()), ""]
    figs = figures(data, out, split)
    lines += [f"![{f}]({f})" for f in figs]
    lines += ["", "## Limitations", "",
              "- Small sample: with 60 test scenarios, intervals are wide; differences inside the CI are not claimed.",
              "- Simulated telemetry is cleaner than production; results are likely an upper bound.",
              "- One model family per run; results may differ for others."]
    path = out / f"report-{split}.md"
    path.write_text("\n".join(lines) + "\n")
    with (out / f"table-{split}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(tab[0]))
        w.writeheader()
        w.writerows(tab)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test")
    ap.add_argument("--runs", type=Path, default=ROOT / "results" / "runs")
    a = ap.parse_args()
    print(build(a.split, a.runs).read_text())
