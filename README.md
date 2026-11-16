# AI Incident Investigator

An evidence-validated, critic-driven multi-agent system that finds the root cause of microservice incidents, proposes a safe fix behind human approval, and verifies that the fix worked. It is also a controlled experiment built to answer one question:

> Does an evidence-validated, critic-driven multi-agent architecture improve root-cause identification accuracy and decision reliability compared with a single-agent approach in distributed microservice environments?

USD AAI-590 capstone project.

## What is in the box

| Part | What it does | Where |
|---|---|---|
| Dataset | 120 labelled incidents (10 fault types, 5 noise templates), grouped 30/30/60 splits, sealed test set | `scenarios/`, `recorder/simulate.py`, [DATASET.md](DATASET.md) |
| Live system | 5-service shop with real fault switches, Postgres, Redis, Prometheus, Alertmanager, Loki, Grafana | `target_app/`, `infra/` |
| Recorder | Records real incidents from the live stack into the same snapshot format | `recorder/record.py` |
| Tools | 8 read-only tools (alert, service map, anomaly scan, metrics, logs, releases, diffs, runbook search) with hashed provenance | `tools/` |
| Single-agent baseline | One reason-act agent with all tools (C0, C1) | `agent/graph_single.py` |
| Multi-agent system | Supervisor, logs/metrics/changes specialists, evidence validation, critic, confidence gate (C2, A1–A6) | `agent/graph_multi.py` |
| Evidence validation | Structural checks, semantic verifier, temporal precedence, contradiction ledger | `agent/validation/`, `agent/common.py` |
| Confidence gate | Stated confidence, self-consistency, evidence margin, calibrated combination | `agent/gate.py`, `eval/calibrate.py` |
| Action layer | Rule-based fix, approval with roles and expiry, re-validation before acting, verification, final report | `agent/remediation/` |
| Evaluation | Harness, scorer, freeze/seal, paired tests, bootstrap CIs, risk–coverage, per-fault tables | `eval/` |
| Interfaces | Alert webhook and approval API; Streamlit dashboard | `backend/app.py`, `ui/dashboard.py` |

## How an investigation flows

```
alert ─► supervisor (2–4 hypotheses) ─► logs | metrics | changes specialists
      ─► evidence validation: structural checks → semantic verifier → cause-before-symptom check
      ─► critic (accept, or ask for specific evidence; must answer every contradiction) ⟲
      ─► confidence gate ─► preliminary report
      ─► rule-based fix ─► approval (role, 15-min expiry) ─► re-validate state ─► execute ─► verify ─► final report
```

The single-agent baseline uses the same model, tools, snapshots and caps, so differences come from the architecture alone.

## Quick start (no API key, about 1 minute)

```bash
git clone <this repo> && cd final-usd-project-ai-incident-investigator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make dataset          # 120 scenarios + telemetry (about 3 seconds)
make test             # 418 tests
make smoke            # whole pipeline on 5 scenarios per split, offline
```

The default provider is `offline`, a deterministic rule-based stand-in for the language model. It runs every step of the pipeline without a key. **Its numbers are a pipeline check, not research results.** Every report it produces says so at the top.

Look at one investigation:

```bash
python -m agent.investigate --snapshot snapshots/test/sc-073 --config eval/configs/C2.yaml
```

## Running the real experiment

1. **Choose a model.** Copy `.env.example` to `.env` and set:
   - Anthropic: `LLM_PROVIDER=anthropic`, `LLM_MODEL=<model id>`, `ANTHROPIC_API_KEY=...`
   - OpenAI-compatible (OpenAI, or a local model through Ollama, vLLM or LM Studio): `LLM_PROVIDER=openai`, `LLM_MODEL=...`, `LLM_BASE_URL=...` (for example `http://localhost:11434/v1` for Ollama), `LLM_API_KEY=...`
   - Set `LLM_PRICE_IN_PER_MTOK` and `LLM_PRICE_OUT_PER_MTOK` so cost is reported in US$.
2. **Check cost first.** `make smoke` runs 5 scenarios per split. Multiply its cost per investigation by about 960 to estimate the full run.
3. **Run everything.** This takes hours with a real model:
   ```bash
   make experiment       # or step by step:
   make dev              # C0 and C2 on dev, 3 runs each
   make calibrate        # fit the gate on C2-dev only; writes agent/policy.yaml
   make freeze           # hash configs/prompts/policy; seal the test snapshots
   make test-runs        # C0 x3, C2 x3, C1 and A1–A6 on test (refused unless frozen)
   make remediate        # 15 end-to-end fixes with approval and verification
   make tamper           # the 10 approval-tampering cases
   make report           # results/report-test.md, figures, CSV
   ```
   Runs resume where they stopped. Use `--workers 4` (the default in `eval.experiment`) to run investigations in parallel.

## Configurations

| Id | What it is | Answers |
|---|---|---|
| C0 | Single agent, all tools | Baseline |
| C0-SC | Majority vote over C0's 3 runs (computed, no extra calls) | Is the gain just extra compute? |
| C1 | C0 plus validation and gate | Do guardrails help without the agent split? |
| C2 | Full multi-agent system | System under test |
| A1–A6 | C2 without critic / semantic verifier / any validation / runbook search / gate; specialists merged | Contribution of each component |

All share one model, temperature 0, the same tools and caps (25 tool calls, 150k tokens, 600 s per investigation). Change caps in `eval/configs/*.yaml`; `eval/config.py` refuses configs that break parity.

## The live system (optional, needs Docker)

```bash
make up               # target app, Postgres, Redis, load generator, Prometheus, Alertmanager, Loki, Grafana
make api              # backend on :8080 (Alertmanager posts alerts here)
make ui               # dashboard on :8501
```

- **Grafana:** http://localhost:3000
- **Prometheus:** http://localhost:9090
- **Alertmanager:** http://localhost:9093
- **Inject a fault by hand:** `curl -XPOST localhost:8003/admin/faults/slow_query -d '{"enabled":true}' -H 'content-type: application/json'`. Alertmanager then fires and the backend starts an investigation.
- **Record real incidents in the snapshot format:** `make record`. Run the experiment on them by passing `--snapshots snapshots-live` to `eval.run`.

The dashboard also works without Docker. Its **Investigate** page runs investigations on snapshots through the backend, with approve and reject buttons. Its **Runs** page browses any experiment investigation, showing the report, every evidence check and the reasoning trail.

## Safety rules the code enforces

- Agents only have read-only tools. Only `ApprovalService.execute` can change the system.
- An approval is valid for one exact action, by a user with the approver role, who is not the proposer, for 15 minutes.
- Just before acting, the system re-checks the release id, the replica count, whether the target exists, and whether the alert is still firing. Any change blocks execution and reopens the case.
- Idempotency keys stop double execution. Every step is written to an audit log.
- `make tamper` runs ten tampering cases; all ten must be blocked.

## Research hygiene

- Prompts may not name a fault or a scenario id (`make lint-prompts`, enforced in tests).
- Tools cannot read the answer files, and see no data later than 30 seconds after the alert.
- The test split runs only after `eval/freeze.py` has hashed configs, prompts and policy and sealed the test snapshots.
- Confidence intervals resample scenario families, not single scenarios; comparisons are paired (McNemar).

## Repository layout

```
agent/         state schema, prompts, LLM layer, both graphs, validation, gate, remediation, reports
tools/         snapshot and live backends, toolkit, runbook search
scenarios/     fault catalogue, generator, schema
recorder/      simulator (v2) and live recorder
eval/          configs, harness, scorer, calibration, freeze, remediation runs, tampering, analysis
backend/       FastAPI alert webhook and approval API
ui/            Streamlit dashboard
target_app/    the live microservices and load generator
infra/         Docker Compose, Prometheus rules, Alertmanager, Loki, Grafana
knowledge/     runbooks
tests/         418 tests
```

## Known limitations

- The main dataset is simulated. It is realistic in structure, but cleaner than production.
- The offline provider is only for pipeline checks; publish numbers only from a real model.
- The live stack and the FastAPI/Streamlit layers were written to run under Docker and Python 3.10+. Their core logic is tested; the web servers and containers themselves need a first run on your machine.
- With 60 test scenarios, differences under about 10 points may not be distinguishable.
