"""Generate scenarios with grouped, leak-free splits (Jira AII-5).

A family = fault x target x noise template. Members of a family differ only in
intensity and onset timing, so a family is assigned to exactly one split.
Held-out faults go only to test. Seen families are assigned per fault so every
seen fault appears in train, dev and test.

    python -m scenarios.generator --out scenarios/generated --seed 590
"""
from __future__ import annotations

import argparse
import random
from collections import defaultdict
from pathlib import Path

import yaml

from scenarios.schema import Fix, GroundTruth, Scenario

CATALOGUE = Path(__file__).with_name("catalogue.yaml")
TARGET_COUNTS = {"train": 30, "dev": 30, "test": 60}
HOST_ALERTS = {"HighMemory", "ServiceUnhealthy"}


def load_catalogue(path: Path = CATALOGUE) -> dict:
    return yaml.safe_load(path.read_text())


def build_families(cat: dict) -> list[dict]:
    fams = []
    for fault, spec in cat["faults"].items():
        for target in spec["targets"]:
            for noise in cat["noise"]:
                if noise == "downstream_alert" and (
                        not cat["callers"].get(target)
                        or spec["alert"] in HOST_ALERTS):   # memory / disk alerts cannot fire on a caller
                    continue
                fams.append({"fault": fault, "target": target, "noise": noise,
                             "held_out": bool(spec.get("held_out"))})
    return fams


def assign_splits(fams: list[dict], rng: random.Random) -> dict[str, list[dict]]:
    """Held-out faults -> test. For each seen fault: 1 family to train, 1 to dev, rest to test."""
    out = {"train": [], "dev": [], "test": []}
    by_fault = defaultdict(list)
    for f in fams:
        (out["test"].append(f) if f["held_out"] else by_fault[f["fault"]].append(f))
    # Rotate noise templates across faults so train and dev get the same difficulty mix.
    cycle = ["none", "background_errors", "unrelated_deploy", "downstream_alert", "concurrent_anomaly"]
    offset = rng.randrange(len(cycle))
    for i, fault in enumerate(sorted(by_fault)):
        group = by_fault[fault][:]
        rng.shuffle(group)                       # random target choice for multi-target faults
        picked = []
        for shift in (0, 1):
            for j in range(len(cycle)):
                noise = cycle[(i + offset + shift + j) % len(cycle)]
                fam = next((f for f in group if f["noise"] == noise and f not in picked), None)
                if fam:
                    picked.append(fam)
                    break
        out["train"].append(picked[0])
        out["dev"].append(picked[1])
        out["test"].extend(f for f in group if f not in picked)
    return out


def allocate(fams: list[dict], total: int) -> list[int]:
    """Scenarios per family: equal share per fault first, then spread over that fault's families."""
    faults = sorted({f["fault"] for f in fams})
    per_fault = {f: total // len(faults) for f in faults}
    for f in faults[: total - sum(per_fault.values())]:
        per_fault[f] += 1
    counts = [0] * len(fams)
    for fault in faults:
        idx = [i for i, f in enumerate(fams) if f["fault"] == fault]
        for j in range(per_fault[fault]):
            counts[idx[j % len(idx)]] += 1
    return counts


def difficulty(cat: dict, fault: str, noise: str) -> str:
    if cat["faults"][fault].get("always_hard"):
        return "hard"
    return cat["noise"][noise]["difficulty"]


def alert_service(cat: dict, fault: str, target: str, noise: str) -> str:
    spec = cat["faults"][fault]
    if spec.get("alert_on"):
        return spec["alert_on"]
    if noise == "downstream_alert":
        return cat["callers"][target]
    return target


def generate(seed: int = 590, cat: dict | None = None) -> list[Scenario]:
    cat = cat or load_catalogue()
    rng = random.Random(seed)
    splits = assign_splits(build_families(cat), rng)
    scenarios: list[Scenario] = []
    n = 0
    for split in ("train", "dev", "test"):          # test generated last, from the same seed
        fams = splits[split]
        for fam, k in zip(fams, allocate(fams, TARGET_COUNTS[split])):
            spec = cat["faults"][fam["fault"]]
            for _ in range(k):
                n += 1
                sid = f"sc-{n:03d}"
                deploy_id = f"d-{rng.randrange(16**3):03x}"
                fill = lambda s: s.format(service=fam["target"], deploy_id=deploy_id)
                scenarios.append(Scenario(
                    id=sid,
                    family=f'{fam["fault"]}|{fam["target"]}|{fam["noise"]}',
                    split=split,
                    fault=fam["fault"],
                    target=fam["target"],
                    noise=fam["noise"],
                    difficulty=difficulty(cat, fam["fault"], fam["noise"]),
                    intensity=rng.choice(cat["intensity"]),
                    onset_offset_s=rng.randint(60, 180),
                    alert_name=spec["alert"],
                    alert_service=alert_service(cat, fam["fault"], fam["target"], fam["noise"]),
                    held_out_fault=fam["held_out"],
                    seed=rng.randrange(2**31),
                    ground_truth=GroundTruth(
                        root_cause_service=fam["target"],
                        root_cause_type=spec["cause_type"],
                        expected_evidence=[fill(e) for e in spec["expected_evidence"]],
                        correct_fix=Fix(action=spec["correct_fix"]["action"],
                                        target=fill(spec["correct_fix"]["target"])),
                        relevant_runbooks=[f'runbook:{fam["target"]}-{fam["fault"]}'],
                    ),
                ))
    return scenarios


def write(scenarios: list[Scenario], out: Path) -> None:
    for s in scenarios:
        d = out / s.split
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{s.id}.yaml").write_text(yaml.safe_dump(s.model_dump(mode="json"), sort_keys=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("scenarios/generated"))
    ap.add_argument("--seed", type=int, default=590)
    a = ap.parse_args()
    sc = generate(a.seed)
    write(sc, a.out)
    by = defaultdict(int)
    for s in sc:
        by[s.split] += 1
    print(f"wrote {len(sc)} scenarios: {dict(by)}")
