import os
from pathlib import Path

import pytest

from recorder.simulate import Sim
from scenarios.generator import generate

os.environ.setdefault("LLM_PROVIDER", "offline")


@pytest.fixture(scope="session")
def scenarios():
    return generate(590)


@pytest.fixture(scope="session")
def snapshot_dir(tmp_path_factory, scenarios):
    """A handful of simulated snapshots covering every noise template, written once per session."""
    root = tmp_path_factory.mktemp("snapshots")
    picked, seen = [], set()
    for sc in scenarios:
        key = (sc.noise, sc.split)
        if key not in seen or sc.fault in ("memory_leak", "expired_credential") and sc.fault not in {p.fault for p in picked}:
            seen.add(key)
            picked.append(sc)
    for sc in picked:
        Sim(sc).run(root / sc.split / sc.id)
    return root
