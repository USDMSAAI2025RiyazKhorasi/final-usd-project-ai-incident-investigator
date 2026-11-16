PY ?= python3
.PHONY: install dataset test lint-prompts experiment smoke dev calibrate freeze test-runs remediate tamper report eda api ui up down record clean-results

install:            ## create .venv and install everything
	$(PY) -m venv .venv && .venv/bin/pip install -r requirements.txt
	@echo "now run: source .venv/bin/activate"

dataset:            ## 120 scenarios + simulated telemetry snapshots (deterministic)
	$(PY) -m scenarios.generator --out scenarios/generated --seed 590
	$(PY) -m recorder.simulate --scenarios scenarios/generated --out snapshots

test:
	$(PY) -m pytest -q

lint-prompts:
	$(PY) -m eval.lint_prompts agent/prompts

smoke:              ## whole pipeline on 5 scenarios per split
	$(PY) -m eval.experiment --limit 5

experiment:         ## the whole experiment (dev -> calibrate -> freeze -> test -> remediate -> report)
	$(PY) -m eval.experiment

dev:
	$(PY) -m eval.experiment --steps dev
calibrate:
	$(PY) -m eval.calibrate --run results/runs/C2-dev
freeze:
	$(PY) -m eval.freeze
test-runs:
	$(PY) -m eval.experiment --steps test
remediate:
	$(PY) -m eval.remediate --split test --limit 15
tamper:
	$(PY) -m eval.tampering
report:
	$(PY) -m eval.analysis.results --split test
eda:
	$(PY) -m eval.analysis.eda --out results/eda

api:                ## backend on :8080
	uvicorn backend.app:app --port 8080
ui:                 ## dashboard on :8501
	streamlit run ui/dashboard.py

up:                 ## live Docker stack
	mkdir -p results/live && docker compose -f infra/docker-compose.yml up -d --build
down:
	docker compose -f infra/docker-compose.yml down -v
record:             ## record real incidents for the dev split from the live stack
	$(PY) -m recorder.record --split dev --out snapshots-live

clean-results:
	rm -rf results/runs results/remediation results/report-*.md results/fig_*.png results/table-*.csv results/freeze.json results/test_hashes.txt
