# Snapshot format (frozen at Gate 1)

`snapshots/<scenario_id>/`

| File | Contents |
|---|---|
| `scenario.yaml` | Copy of the scenario, including ground truth. **Harness and scorer only; tools must never read it** |
| `fault_window.json` | When the fault started. **Harness and scorer only** |
| `meta.json` | `source`: `recorded` or `simulated`, plus step size |
| `window.json` | `{"baseline_start", "alert_at", "end"}` as ISO timestamps |
| `alert.json` | The Alertmanager webhook payload that fired |
| `metrics/<metric>.json` | Prometheus range-query results for every metric in the target app, 5 s step |
| `logs/<service>.jsonl` | Loki log lines for the window, one JSON object per line |
| `deploys.json` | Deploy log entries in the window (real and noise deploys) |
| `commits/<sha>.json` | Diff summaries for deploys in the window |
| `service_graph.json` | Who calls whom |
| `SHA256SUMS` | Hash of every file; test-split hashes are committed at Gate 2 |

The snapshot backend in `tools/snapshot_backend.py` answers every tool call from these files only.

Snapshots can come from `recorder/record.py` (real Docker run) or `recorder/simulate.py` (programmatic simulator). Both write this exact format, so agents and tools work on either unchanged.
