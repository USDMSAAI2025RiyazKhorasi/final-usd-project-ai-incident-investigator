from datetime import datetime, timedelta, timezone

from agent.state import Evidence, Provenance
from agent.validation.structural import check, sha256

T0 = datetime(2026, 11, 10, 14, 0, tzinfo=timezone.utc)
WINDOW = (T0, T0 + timedelta(minutes=10))
RAW = '{"pattern": "timeout after 50ms", "count": 412, "first_seen": "14:03"}'


def ev(summary="412 lines say 'timeout after 50ms'", raw_hash=None, start=T0, hyp="h1", ok=True):
    return Evidence(id="e1", agent="logs", hypothesis_id=hyp, claimed="supports", summary=summary,
                    provenance=Provenance(tool="search_logs", args={"service": "payments"}, called_at=T0,
                                          data_start=start, data_end=start + timedelta(minutes=5),
                                          raw_ref="mem", raw_hash=raw_hash or sha256(RAW),
                                          mode="snapshot", ok=ok))


def run(e):
    return check(e, hypothesis_ids={"h1", "h2"}, alert_window=WINDOW, margin=timedelta(minutes=5),
                 load_raw=lambda ref: RAW)


def test_valid():
    assert run(ev()).structural == "valid"


def test_ungrounded_number_rejected():
    r = run(ev(summary="900 lines say 'timeout after 50ms'"))
    assert r.structural == "invalid" and "900" in r.structural_reason


def test_stale_evidence_rejected():
    assert run(ev(start=T0 - timedelta(hours=2))).structural == "invalid"


def test_hash_mismatch_rejected():
    assert run(ev(raw_hash="0" * 64)).structural == "invalid"


def test_unknown_hypothesis_rejected():
    assert run(ev(hyp="h9")).structural == "invalid"


def test_failed_call_rejected():
    assert run(ev(ok=False)).structural == "invalid"


def test_usable_needs_matching_verdict():
    e = run(ev())
    assert not e.usable
    assert e.model_copy(update={"verdict": "SUPPORTS"}).usable
    assert not e.model_copy(update={"verdict": "IRRELEVANT"}).usable
