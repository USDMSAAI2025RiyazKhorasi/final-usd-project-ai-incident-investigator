from collections import Counter, defaultdict

from scenarios.generator import generate, load_catalogue


def test_split_sizes():
    c = Counter(s.split for s in generate(590))
    assert c == {"train": 30, "dev": 30, "test": 60}


def test_families_never_cross_splits():
    splits = defaultdict(set)
    for s in generate(590):
        splits[s.family].add(s.split)
    assert all(len(v) == 1 for v in splits.values())


def test_held_out_faults_only_in_test():
    held = {f for f, spec in load_catalogue()["faults"].items() if spec.get("held_out")}
    for s in generate(590):
        if s.fault in held:
            assert s.split == "test" and s.held_out_fault


def test_every_seen_fault_in_every_split():
    held = {f for f, spec in load_catalogue()["faults"].items() if spec.get("held_out")}
    by_split = defaultdict(set)
    for s in generate(590):
        by_split[s.split].add(s.fault)
    seen = set(load_catalogue()["faults"]) - held
    assert seen <= by_split["train"] and seen <= by_split["dev"] and seen <= by_split["test"]


def test_deterministic():
    assert [s.model_dump() for s in generate(7)] == [s.model_dump() for s in generate(7)]


def test_downstream_alert_fires_on_caller():
    for s in generate(590):
        if s.noise == "downstream_alert":
            assert s.alert_service != s.target
