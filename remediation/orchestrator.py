"""
Orchestrator: runs ONE incident end to end.

  inject fault -> agent diagnoses (MCP) -> risk score -> policy routes
      -> (auto) execute          -> verify recovery
      -> (approval) human/sim-operator approves|rejects|edits -> execute -> verify

Blocking calls (docker, requests, sqlite polling) run via asyncio.to_thread so the
API's event loop (SSE) stays responsive during interactive demos.

CLI:
  python -m remediation.orchestrator --mode adaptive --fault HIGH_LATENCY --target service-b
  python -m remediation.orchestrator --mode adaptive --fault CRASH --target service-a --interactive
"""
import argparse
import asyncio
import json
import time
import uuid

from . import executor, faults, store
from .agent import make_agent
from .config import (ADAPTIVE_REVIEW_LOAD, ADAPTIVE_THRESHOLD, FIX_MATRIX, HUMAN_REVIEW_DELAY_S,
                      INJECT_SETTLE_S, MANUAL_FIX_PENALTY_S, OPERATOR_CATCH_RATE, RECOVERY_TIMEOUT_S,
                      RISK_VERSION)
from .operator_model import SimulatedOperator
from .policy import MODES, Routing, route
from .risk import score_plan


async def _wait_recovered(service: str, timeout_s: float) -> bool:
    end = time.time() + timeout_s
    while time.time() < end:
        if await asyncio.to_thread(faults.is_recovered, service):
            return True
        await asyncio.sleep(1)
    return False


def _review_load(mode: str) -> float:
    """
    How saturated is the human queue for this policy? 1.0 = every incident is
    reviewed by a human (full-human). Under the adaptive policy only a fraction
    reaches a human, so the operator is less fatigued and reviews more accurately
    (METHODOLOGY.md 1.B). Before this was wired up, review_load was always 0 and
    the alert-fatigue model never fired.
    """
    if mode == "full-human":
        return 1.0
    if mode == "adaptive":
        return ADAPTIVE_REVIEW_LOAD
    return 0.0


async def run_incident(agent, mode: str, fault: str, target: str, *, scenario_id: str = "adhoc",
                       operator: SimulatedOperator | None = None, interactive: bool = False,
                       threshold: float = ADAPTIVE_THRESHOLD, precomputed: dict | None = None,
                       run_id: str | None = None, risk_version: str = RISK_VERSION) -> dict:
    """precomputed: reuse one agent diagnosis across modes (paired design, 3x cheaper, no LLM variance)."""
    assert mode in MODES
    row = {"run_id": run_id or uuid.uuid4().hex[:8], "mode": mode, "scenario_id": scenario_id,
           "fault": fault, "target": target, "error": None, "risk_version": risk_version}
    op = operator if operator is not None else SimulatedOperator()
    try:
        await asyncio.to_thread(faults.reset_all)
        await asyncio.to_thread(faults.inject, fault, target)
        await asyncio.sleep(INJECT_SETTLE_S)
        store.add_event("RUN_STARTED", {"mode": mode, "fault": fault, "target": target})

        res = precomputed or await agent.diagnose()
        plan = res["plan"]
        row.update(decision_latency_s=res["latency_s"], mcp_calls=res["mcp_calls"])
        if plan is None:
            row.update(error="no_plan", recovered=False)
            return row

        # Score under BOTH formulas so every row carries an ablation pair.
        risk_v1 = score_plan(plan, version="v1")
        risk_v2 = score_plan(plan, version="v2")
        risk = risk_v1 if risk_version == "v1" else risk_v2
        routing = route(mode, risk["score"], threshold)
        row.update(plan=plan, plan_service=plan["service"], suspected_fault=plan["suspected_fault"],
                   plan_action=plan["action"], plan_correct=plan["action"] in FIX_MATRIX[fault],
                   confidence_raw=plan["confidence"], confidence_used=risk["confidence_used"],
                   rubric_consistency=risk["rubric_consistency"],
                   risk_score=risk["score"], risk_score_v1=risk_v1["score"], risk_score_v2=risk_v2["score"],
                   routing=routing.value)
        store.add_event("STATE_SNAPSHOT", {"plan": plan, "risk": risk, "routing": routing.value})

        human_wait, manual_fix, verdict = 0.0, False, "auto"
        if routing is Routing.APPROVAL:
            if interactive:
                pid = await asyncio.to_thread(store.create_plan, plan, risk, {
                    "decision_latency_s": res["latency_s"], "mcp_calls": res["mcp_calls"],
                    "risk_version": risk_version, "threshold": threshold, "mode": mode,
                })
                store.add_event("CUSTOM", {"name": "approval_requested", "plan_id": pid})
                t_wait = time.time()
                decided = await asyncio.to_thread(store.wait_for_decision, pid)
                human_wait = time.time() - t_wait
                verdict = decided["decision"] if decided else "reject"
                if decided and decided.get("edited_action"):
                    plan["action"] = decided["edited_action"]
            else:
                verdict, human_wait = op.review(plan, fault, review_load=_review_load(mode))

        # Health snapshot taken right before the action: the baseline that any
        # collateral damage is measured against.
        baseline = await asyncio.to_thread(faults.get_collateral_metrics)

        t_exec = time.time()
        if verdict == "reject":
            manual_fix = True  # human rejects the plan and fixes it by hand
            if op.attempt_manual_fix():
                exec_res = await asyncio.to_thread(
                    executor.execute,
                    {"service": target, "action": sorted(FIX_MATRIX[fault])[0]}, baseline)
            else:
                # Simulate a failed manual attempt
                exec_res = {"action": "manual_attempt", "collateral": False, "effective": False}
        else:
            exec_res = await asyncio.to_thread(executor.execute, plan, baseline)
        recovered = await _wait_recovered(target, RECOVERY_TIMEOUT_S)
        exec_verify_s = time.time() - t_exec
        penalty = MANUAL_FIX_PENALTY_S if manual_fix else 0.0

        row.update(human_intervention=routing is Routing.APPROVAL, verdict=verdict, manual_fix=manual_fix,
                   executed_action=exec_res["action"], action_effective=exec_res.get("effective"),
                   collateral=exec_res["collateral"],
                   unsafe_action=faults.is_unsafe(fault, exec_res),
                   incorrect_action=faults.is_incorrect(fault, exec_res),
                   recovered=recovered, human_wait_s=round(human_wait, 2),
                   mttr_s=round(res["latency_s"] + human_wait + exec_verify_s + penalty, 2) if recovered else None)
        store.add_event("RUN_FINISHED", {k: row.get(k) for k in ("recovered", "unsafe_action", "mttr_s")})
    except Exception as e:  # keep the sweep alive; the row records the failure
        row.update(error=f"{type(e).__name__}: {e}", recovered=False)
    return row


async def _cli(a):
    async with make_agent(a.agent, error_rate=a.error_rate) as agent:
        row = await run_incident(agent, a.mode, a.fault, a.target, interactive=a.interactive)
    print(json.dumps(row, indent=2))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=MODES, default="adaptive")
    p.add_argument("--fault", default="HIGH_LATENCY")
    p.add_argument("--target", default="service-b")
    p.add_argument("--agent", choices=["heuristic", "llm"], default="heuristic")
    p.add_argument("--error-rate", type=float, default=0.0)
    p.add_argument("--interactive", action="store_true", help="wait for approval from the UI")
    asyncio.run(_cli(p.parse_args()))


if __name__ == "__main__":
    main()
