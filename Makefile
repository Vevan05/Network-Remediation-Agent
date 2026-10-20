# Use the project virtualenv if it exists, otherwise the system python.
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

.PHONY: up down reset api ui demo pilot test bench unsafe audit sweep sweep-live report figs

up:
	docker compose up -d --build

down:
	docker compose down

reset:
	$(PY) -c "from remediation import faults; faults.reset_all(); print('testbed reset')"

api:
	$(PY) -m uvicorn remediation.api:app --port 8000 --reload

ui:
	cd ui && npm install && npm run dev

demo:
	$(PY) -m remediation.orchestrator --mode adaptive --fault HIGH_LATENCY --target service-b

# Fast, no-LLM smoke run of the whole pipeline.
pilot:
	$(PY) -m eval.run_experiment --agent heuristic --error-rate 0.25 --repeats 3 --tag pilot
	$(PY) -m eval.metrics results/runs_pilot.jsonl

# The main benchmark: the LLM agent, 12 scenarios x 3 repeats, paired across policies.
bench:
	$(PY) -m eval.run_experiment --agent llm --repeats 3 --tag benchmark_v2 --risk-version v2
	$(PY) -m eval.metrics results/runs_benchmark_v2.jsonl

# The stress test that makes unsafe actions possible: a deliberately fallible agent
# plus a shared dependency, so collateral damage is reachable.
unsafe:
	$(PY) -m eval.run_experiment --agent heuristic --error-rate 0.30 --repeats 10 --tag unsafe_heuristic --risk-version v2
	$(PY) -m eval.run_experiment --agent heuristic --error-rate 0.50 --repeats 5 --tag unsafe_heuristic_50 --risk-version v2
	$(PY) -m eval.metrics results/runs_unsafe_heuristic.jsonl

# Did the router escalate the right plans?
audit:
	$(PY) -m eval.audit results/runs_benchmark_v2.jsonl

# Threshold sweep (offline replay of a recorded benchmark; no Docker, no LLM).
sweep:
	$(PY) -m eval.sweep results/runs_benchmark_v2.jsonl --mode replay --thresholds 0.15 0.30 0.40 0.50 0.90 --versions v1 v2

# Threshold sweep that re-executes the plans in the testbed instead of simulating.
sweep-live:
	$(PY) -m eval.sweep results/runs_benchmark_v2.jsonl --mode live --thresholds 0.15 0.30 0.40 0.50 0.90 --versions v1 v2

# Every number in README.md, in one go.
report:
	$(PY) -m eval.report results/runs_benchmark_v2.jsonl

figs:
	$(PY) -m eval.metrics results/runs_benchmark_v2.jsonl

test:
	$(PY) -m pytest -q
