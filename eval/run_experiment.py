"""
Experiment runner (paired design).

For every (scenario, repeat):
  1. reset + inject the fault, let the agent diagnose ONCE
  2. for each policy mode: reset + inject again, replay that SAME plan through route/execute/verify

Because all three modes see the identical plan, differences between them are caused by the
policy alone, not LLM randomness - and the LLM is called 1x instead of 3x.

Rows are appended to results/runs_<tag>.jsonl as they finish, so a crash loses nothing.

  python -m eval.run_experiment --agent heuristic --error-rate 0.25 --repeats 5 --tag pilot
  python -m eval.run_experiment --agent llm --repeats 5 --tag main
"""
import argparse
import asyncio
import json
import time
import uuid
import zlib
from pathlib import Path

import yaml

from remediation import faults
from remediation.agent import make_agent
from remediation.config import ADAPTIVE_THRESHOLD, INJECT_SETTLE_S, RESULTS_DIR, RISK_VERSION
from remediation.orchestrator import SimulatedOperator, run_incident
from remediation.policy import MODES

SCENARIOS = Path(__file__).with_name("scenarios.yaml")


async def main(a):
    scenarios = yaml.safe_load(SCENARIOS.read_text())["scenarios"]
    if a.limit:
        scenarios = scenarios[: a.limit]
    out = RESULTS_DIR / f"runs_{a.tag}.jsonl"
    RESULTS_DIR.mkdir(exist_ok=True)
    total, done = a.repeats * len(scenarios), 0

    # Resume logic: read existing rows to see what's already done
    existing = set()
    if out.exists():
        with out.open("r") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    # A run is uniquely identified by scenario_id + repeat
                    existing.add((r["scenario_id"], r["rep"]))
                except (json.JSONDecodeError, KeyError):
                    pass

    async with make_agent(a.agent, error_rate=a.error_rate, seed=a.seed) as agent:
        for rep in range(a.repeats):
            for sc in scenarios:
                if (sc["id"], rep) in existing:
                    continue
                done += 1
                print(f"[{done}/{total}] {sc['id']} {sc['fault']}@{sc['target']} rep={rep}", flush=True)
                await asyncio.to_thread(faults.reset_all)
                await asyncio.to_thread(faults.inject, sc["fault"], sc["target"])
                await asyncio.sleep(INJECT_SETTLE_S)
                diag = await agent.diagnose()          # one diagnosis, shared by all modes
                group = uuid.uuid4().hex[:8]
                # One operator seed per DIAGNOSIS, not per run. The operator's RNG is
                # consumed in the same order by every policy (review, then the manual
                # fix), so the same plan gets the same verdict under every policy and
                # the only thing that differs between policies is the routing.
                # (Sharing one operator across the whole run let full-human and
                # adaptive draw from different positions of the same stream, which
                # quietly broke the paired design.)
                group_seed = zlib.crc32(group.encode()) % (2 ** 31)
                for mode in MODES:
                    row = await run_incident(
                        agent, mode, sc["fault"], sc["target"], scenario_id=sc["id"],
                        operator=SimulatedOperator(seed=group_seed),
                        threshold=a.threshold, precomputed=diag, run_id=group,
                        risk_version=a.risk_version)
                    row.update(rep=rep, tag=a.tag, agent=a.agent, threshold=a.threshold, ts=time.time())
                    with out.open("a") as f:
                        f.write(json.dumps(row) + "\n")
    print(f"done -> {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--agent", choices=["heuristic", "llm"], default="heuristic")
    p.add_argument("--error-rate", type=float, default=0.25, help="heuristic agent only")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--threshold", type=float, default=ADAPTIVE_THRESHOLD)
    p.add_argument("--risk-version", choices=["v1", "v2"], default=RISK_VERSION)
    p.add_argument("--limit", type=int, default=0, help="only the first N scenarios (smoke test)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--tag", default="pilot")
    asyncio.run(main(p.parse_args()))
