"""Fails if any prompt names a fault or a scenario id (bias safeguard, Jira AII-1)."""
import re
import sys
from pathlib import Path

import yaml

FAULTS = yaml.safe_load(Path("scenarios/catalogue.yaml").read_text())["faults"].keys()
PATTERNS = [re.compile(r"sc-\d{3}")] + [re.compile(f.replace("_", "[ _-]?"), re.I) for f in FAULTS]


def main(root: str) -> int:
    bad = [(p, pat.pattern) for p in Path(root).rglob("*") if p.is_file()
           for pat in PATTERNS if pat.search(p.read_text(errors="ignore"))]
    for p, pat in bad:
        print(f"{p}: matches {pat}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "agent/prompts"))
