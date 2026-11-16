"""Freeze configs, prompts and policy, and seal the test snapshots (Gate 2 / freeze-v1).

    python -m eval.freeze            # writes results/freeze.json and results/test_hashes.txt
    python -m eval.freeze --check    # exit 1 if anything changed since freezing

eval/run.py refuses the test split unless --check passes, so nobody can tune on test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ["eval/configs/*.yaml", "agent/policy.yaml", "agent/prompts/*.md", "agent/users.yaml",
          "scenarios/catalogue.yaml"]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def current(root: Path = ROOT) -> dict:
    files = {}
    for pat in FROZEN:
        for p in sorted(root.glob(pat)):
            files[str(p.relative_to(root))] = _sha(p)
    return files


def test_hashes(snapshots: Path) -> dict:
    return {p.parent.name: _sha(p) for p in sorted((snapshots / "test").glob("sc-*/SHA256SUMS"))}


def freeze(snapshots: Path, out: Path = ROOT / "results") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "freeze.json").write_text(json.dumps({"frozen_at": datetime.now(timezone.utc).isoformat(),
                                                 "files": current()}, indent=1))
    (out / "test_hashes.txt").write_text("".join(f"{h}  {sid}\n" for sid, h in test_hashes(snapshots).items()))


def check(snapshots: Path, out: Path = ROOT / "results") -> list[str]:
    problems = []
    f = out / "freeze.json"
    if not f.exists():
        return ["not frozen: run `python -m eval.freeze` after calibrating on dev"]
    frozen = json.loads(f.read_text())["files"]
    now = current()
    for k in sorted(set(frozen) | set(now)):
        if frozen.get(k) != now.get(k):
            problems.append(f"changed since freeze: {k}")
    sealed = dict(l.split()[::-1] for l in (out / "test_hashes.txt").read_text().splitlines() if l.strip())
    for sid, h in test_hashes(snapshots).items():
        if sealed.get(sid) != h:
            problems.append(f"test snapshot changed: {sid}")
    return problems


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=Path, default=ROOT / "snapshots")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        probs = check(a.snapshots)
        print("\n".join(probs) or "frozen state intact")
        sys.exit(1 if probs else 0)
    freeze(a.snapshots)
    print(f"frozen {len(current())} files and sealed {len(test_hashes(a.snapshots))} test snapshots")
