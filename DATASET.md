# Dataset card: AI Incident Investigator scenarios (v2)

120 labelled microservice incidents. Each has the telemetry an on-call engineer would see (metrics, logs, releases, commits, the alert) and a hidden ground-truth root cause. It is the main dataset for the research question; RCAEval RE2 is the optional external check.

Rebuild it byte for byte with:

```bash
make dataset        # = python -m scenarios.generator --seed 590 && python -m recorder.simulate
```

Snapshots are produced by `recorder/simulate.py` (programmatic simulator) or `recorder/record.py` (real Docker stack). Both write the same format, so agents cannot tell them apart except via `meta.json`.

## Splits

| Split | Scenarios | Families | Easy / medium / hard | Used for |
|---|---|---|---|---|
| train | 30 | 8 | 8 / 10 / 12 | Prompts, runbooks, tool summaries |
| dev | 30 | 8 | 4 / 15 / 11 | Calibration, thresholds, verifier check |
| test | 60 | 41 | 12 / 21 / 27 | Final numbers only (sealed by `eval/freeze.py`) |

- A **family** is fault × target service × noise template. Its members differ only in strength and timing, so a family never crosses splits (tested).
- Every seen fault appears in all three splits. **Disk full** and **expired credential** appear only in test (12 scenarios), as unseen faults.
- Test has exactly 6 scenarios per fault.

## Faults

| Fault | Root-cause service | Category | Alert | Key evidence |
|---|---|---|---|---|
| Memory leak | orders | resource_exhaustion | HighMemory | Memory climbs to the limit; OOMKilled restarts; counters reset |
| Slow query | inventory | database_performance | HighLatency | Database query time rises; slow-query logs |
| Bad config release | payments, orders | config_change | HighErrorRate | Release 5 s before onset; "timeout after 50ms"; config diff |
| Buggy code release | gateway, inventory | code_change | HighErrorRate | Release before onset; tracebacks; code diff |
| Dependency down | notifications | dependency_unavailable | HighErrorRate | Dependency unreachable; "Connection refused" |
| Pool exhausted | orders | resource_exhaustion | HighLatency | Busy connections reach 20; "pool timeout" |
| CPU saturation | inventory, payments | resource_exhaustion | HighLatency | CPU near 100%; event-loop warnings |
| Cascading failure | payments | resource_exhaustion | HighErrorRate **on gateway** | Payments slows first; "upstream timeout" climbs the chain |
| Expired credential (unseen) | payments | credential_failure | HighErrorRate | Credential-rotation release; "401 Unauthorized" |
| Disk full (unseen) | notifications | resource_exhaustion | ServiceUnhealthy | Free disk falls to 0; "No space left on device" |

## Noise and difficulty

| Template | Difficulty | What it adds |
|---|---|---|
| none | easy | Nothing |
| background_errors | medium | Another service throws unrelated errors for the whole window |
| unrelated_deploy | medium | A harmless release on another service 30–150 s before the fault |
| downstream_alert | hard | The alert fires on a caller, not on the faulty service |
| concurrent_anomaly | hard | An unrelated service misbehaves (errors, latency or CPU) from 60 s before to 40 s after the fault |

Realism added in v2, after the EDA showed that a simple rule found the faulty service in 99% of v1 incidents:
- **Propagation delays.** Each call edge has its own delay. In 30% of edges the delay is negative, so the caller looks abnormal first.
- **Partial faults.** These hit only some requests (26 scenarios).
- **Scrape gaps.** Missing metric samples appear in 48 scenarios.
- **Counter resets.** Request and CPU counters reset when a service restarts.
- **Varied wording.** Each fault's log message is phrased in one of three ways; there are 70 distinct non-routine log patterns.
- **Routine noise.** Every service logs ordinary warnings.

With v2, the best simple rule uses only alert-time information: the first service to depart from normal, with ties going to the deepest service. It finds the faulty service in 88% of easy, 89% of medium and 52% of hard incidents (73% overall). Blaming the alerting service scores 77% overall and 44% on hard.

## Files per scenario (`snapshots/<split>/<id>/`)

| File | Contents | Investigator can read |
|---|---|---|
| alert.json | The alert: name, service, time, summary | yes |
| metrics/*.json | 8 metrics for every service, Prometheus range format, 5 s step | yes |
| logs/<service>.jsonl | JSON log lines (79,308 in total) | yes |
| deploys.json, commits/ | Release history and code changes | yes |
| service_graph.json, window.json, meta.json | Call graph; time window; simulated or recorded | yes |
| scenario.yaml, fault_window.json | The answer and simulation details | **no** (blocked in code) |
| SHA256SUMS | Hash of every file; test hashes are sealed at freeze | no |

Tools only return data up to 30 seconds after the alert, as in a real investigation.

## Checks (`tests/test_simulator.py`, `tests/test_tools.py`)

- Every alert's condition holds on the alert's service when it fires, and never before the fault starts.
- Every expected evidence signature is present in the telemetry.
- No fault name, scenario id or enum name appears in anything an agent can see.
- Generation is deterministic (identical hashes).
- Every target service has earlier releases to roll back to.
- The answer files cannot be read through the tools.

## Limitations

- **The telemetry is simulated.** It is cleaner than production even after v2, so results on it are likely an upper bound. Use `recorder/record.py` to replace it with real recordings in the same format.
- **We designed both the faults and the system under test.** The safeguards against this: the catalogue is frozen, prompts are checked for fault names, the test split is sealed, and two fault types are held out.
- **Wide intervals per fault.** With 6 test scenarios per fault, report counts rather than percentages alone.
