"""
Threshold sweep: the trade-off curve for the paper.

Two modes:

  --mode replay  (default)  Re-route an existing benchmark at candidate thresholds
                             WITHOUT touching Docker or the LLM. Each recorded
                             diagnosis is re-routed, the operator is re-simulated,
                             and the escalation path is replayed end to end
                             (approve -> the plan runs; reject -> the operator
                             fixes it by hand, which can fail).
                             The recorded full-auto row is the ground truth for
                             "what happens when the plan runs autonomously".

  --mode live                Re-execute the same plans in the testbed at each
                             threshold. Slower, but it measures instead of
                             simulating - use it to validate the replay.

  python -m eval.sweep results/runs_benchmark_v2.jsonl
  python -m eval.sweep results/runs_benchmark_v2.jsonl --mode live --thresholds 0.2 0.3 0.4 0.5 0.9

The old analytic sweep modelled "unsafe iff the plan is wrong". That is wrong for
this testbed: a wrong plan is usually just INEFFECTIVE (e.g. restarting a service
that has packet loss). Only a misfired reroute or a gratuitous restart of a shared
dependency causes collateral damage. Conflating the two is what made the old
unsafe-rate curve meaningless.
"""
import argparse
import asyncio
import json
import uuid
import zlib
from pathlib import Path

import pandas as pd

from remediation.config import (ADAPTIVE_REVIEW_LOAD, ADAPTIVE_THRESHOLD, MANUAL_FIX_PENALTY_S,
                                RECOVERY_TIMEOUT_S, RESULTS_DIR, RISK_VERSION)
from remediation.operator_model import SimulatedOperator
from remediation.risk import score_plan

# Evidence is a deterministic function of the injected fault in this testbed, so a
# plan recorded before the `plan` field was added can be rebuilt faithfully.
_EVIDENCE_BY_FAULT = {
    "CRASH":         {"status": "down", "latency_ms": 8, "latency_ms_p50": 8, "packet_loss_pct": 0.0, "config_checksum_ok": True},
    "HIGH_LATENCY":  {"status": "degraded", "latency_ms": 608, "latency_ms_p50": 608, "packet_loss_pct": 0.0, "config_checksum_ok": True},
    "PACKET_LOSS":   {"status": "degraded", "latency_ms": 8, "latency_ms_p50": 8, "packet_loss_pct": 30.0, "config_checksum_ok": True},
    "CONFIG_DRIFT":  {"status": "degraded", "latency_ms": 8, "latency_ms_p50": 8, "packet_loss_pct": 0.0, "config_checksum_ok": False},
}


def reconstruct_plan(row) -> dict:
    """Rebuild the full plan (incl. the evidence the agent saw) from a recorded row."""
    if isinstance(row.get("plan"), dict) and row["plan"]:
        p = dict(row["plan"])
        p.setdefault("evidence", _EVIDENCE_BY_FAULT.get(row["fault"], {}))
        return p
    return {
        "service": row["plan_service"],
        "suspected_fault": row["suspected_fault"],
        "action": row["plan_action"],
        "confidence": row["confidence_raw"],
        "evidence": _EVIDENCE_BY_FAULT.get(row["fault"], {}),
        "rationale": "(reconstructed from recorded row)",
    }


def load_groups(path: str) -> list[dict]:
    """One entry per diagnosis (run_id group), with the auto-execute outcome attached."""
    df = pd.read_json(path, lines=True)
    if "error" in df:
        df = df[df["error"].isna()]
    groups: dict[str, list] = {}
    for _, r in df.iterrows():
        groups.setdefault(r["run_id"], []).append(r)
    out = []
    for rid, rows in groups.items():
        auto = next((r for r in rows if r["mode"] == "full-auto"), rows[0])
        lat = float(auto.get("decision_latency_s") or 0.0)
        mttr = auto.get("mttr_s")
        exec_s = (float(mttr) - lat) if mttr is not None and not pd.isna(mttr) else RECOVERY_TIMEOUT_S
        out.append({
            "run_id": rid,
            "scenario_id": auto["scenario_id"],
            "fault": auto["fault"],
            "target": auto["target"],
            "plan": reconstruct_plan(auto),
            "latency_s": lat,
            "mcp_calls": auto.get("mcp_calls"),
            # what happens when the plan runs with no human in the loop
            "auto_recovered": bool(auto["recovered"]),
            "auto_unsafe": bool(auto["unsafe_action"]),
            "auto_effective": bool(auto.get("action_effective", auto["recovered"])),
            "exec_s": exec_s,
            # stable across processes (python's str hash is salted per process)
            "seed": zlib.crc32(rid.encode()) % (2 ** 31),
        })
    return out


def _aggregate(results: list[dict]) -> dict:
    n = len(results)
    recovered = [r for r in results if r["recovered"]]
    return {
        "n": n,
        "escalated": sum(1 for r in results if r["escalated"]),
        "human_rate": round(sum(1 for r in results if r["escalated"]) / n, 3) if n else 0.0,
        "success_rate": round(len(recovered) / n, 3) if n else 0.0,
        "unsafe_rate": round(sum(1 for r in results if r["unsafe"]) / n, 3) if n else 0.0,
        "failures": n - len(recovered),
        "mttr_s": round(sum(r["mttr_s"] for r in recovered) / len(recovered), 2) if recovered else None,
        "decision_latency_s": round(sum(r["decision_latency_s"] for r in results) / n, 2) if n else 0.0,
        "mcp_calls": round(sum(r["mcp_calls"] or 0 for r in results) / n, 2) if n else 0.0,
    }


def replay(groups: list[dict], thresholds: list[float], version: str = "v1",
           review_load: float = ADAPTIVE_REVIEW_LOAD) -> pd.DataFrame:
    """Re-route every recorded diagnosis at each threshold and replay the outcome."""
    rows = []
    for t in thresholds:
        results = []
        for g in groups:
            risk = score_plan(g["plan"], version=version)["score"]
            escalated = risk >= t
            if not escalated:
                results.append({
                    "escalated": False, "recovered": g["auto_recovered"], "unsafe": g["auto_unsafe"],
                    "mttr_s": g["latency_s"] + g["exec_s"], "decision_latency_s": g["latency_s"],
                    "mcp_calls": g["mcp_calls"]})
                continue
            op = SimulatedOperator(seed=g["seed"])
            verdict, delay = op.review(g["plan"], g["fault"], review_load=review_load)
            if verdict == "approve":
                results.append({
                    "escalated": True, "recovered": g["auto_recovered"], "unsafe": g["auto_unsafe"],
                    "mttr_s": g["latency_s"] + delay + g["exec_s"], "decision_latency_s": g["latency_s"],
                    "mcp_calls": g["mcp_calls"]})
            else:
                ok = op.attempt_manual_fix()
                results.append({
                    "escalated": True, "recovered": ok, "unsafe": False,
                    "mttr_s": g["latency_s"] + delay + g["exec_s"] + MANUAL_FIX_PENALTY_S,
                    "decision_latency_s": g["latency_s"], "mcp_calls": g["mcp_calls"]})
        row = {"risk_version": version, "threshold": round(float(t), 3)}
        row.update(_aggregate(results))
        rows.append(row)
    return pd.DataFrame(rows)


def replay_fixed(groups: list[dict], mode: str, version: str = "v1",
                 review_load: float = 0.0) -> dict:
    """
    Replay a FIXED policy (full-auto or full-human) at a given operator review load.
    Used to separate the effect of the routing policy from the effect of the
    operator's alert fatigue, which the v1 code never applied.
    """
    results = []
    for g in groups:
        if mode == "full-auto":
            results.append({"escalated": False, "recovered": g["auto_recovered"], "unsafe": g["auto_unsafe"],
                            "mttr_s": g["latency_s"] + g["exec_s"], "decision_latency_s": g["latency_s"],
                            "mcp_calls": g["mcp_calls"]})
            continue
        op = SimulatedOperator(seed=g["seed"])
        verdict, delay = op.review(g["plan"], g["fault"], review_load=review_load)
        if verdict == "approve":
            results.append({"escalated": True, "recovered": g["auto_recovered"], "unsafe": g["auto_unsafe"],
                            "mttr_s": g["latency_s"] + delay + g["exec_s"], "decision_latency_s": g["latency_s"],
                            "mcp_calls": g["mcp_calls"]})
        else:
            ok = op.attempt_manual_fix()
            results.append({"escalated": True, "recovered": ok, "unsafe": False,
                            "mttr_s": g["latency_s"] + delay + g["exec_s"] + MANUAL_FIX_PENALTY_S,
                            "decision_latency_s": g["latency_s"], "mcp_calls": g["mcp_calls"]})
    out = _aggregate(results)
    out["mode"] = mode
    out["review_load"] = review_load
    return out


async def live_sweep(groups: list[dict], thresholds: list[float], version: str = "v1",
                     risk_version: str | None = None) -> pd.DataFrame:
    """Re-execute the recorded plans in the testbed at every threshold (measures, does not simulate)."""
    from remediation import faults
    from remediation.agent import make_agent
    from remediation.config import INJECT_SETTLE_S
    from remediation.orchestrator import run_incident

    risk_version = risk_version or version
    rows = []
    async with make_agent("heuristic") as agent:  # the plan is precomputed; the agent is never asked
        for t in thresholds:
            results = []
            for g in groups:
                op = SimulatedOperator(seed=g["seed"])
                pre = {"plan": g["plan"], "latency_s": g["latency_s"], "mcp_calls": g["mcp_calls"]}
                r = await run_incident(agent, "adaptive", g["fault"], g["target"],
                                       scenario_id=g["scenario_id"], operator=op, threshold=t,
                                       precomputed=pre, run_id=uuid.uuid4().hex[:8],
                                       risk_version=risk_version)
                results.append({
                    "escalated": bool(r.get("human_intervention")),
                    "recovered": bool(r.get("recovered")),
                    "unsafe": bool(r.get("unsafe_action")),
                    "mttr_s": r.get("mttr_s") or 0.0,
                    "decision_latency_s": r.get("decision_latency_s") or 0.0,
                    "mcp_calls": r.get("mcp_calls") or 0})
            row = {"risk_version": version, "threshold": round(float(t), 3)}
            row.update(_aggregate(results))
            rows.append(row)
            print(f"  threshold {t}: {row}", flush=True)
    return pd.DataFrame(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--mode", choices=["replay", "live"], default="replay")
    p.add_argument("--thresholds", type=float, nargs="+",
                   default=[0.15, 0.30, 0.40, 0.50, 0.90])
    p.add_argument("--versions", nargs="+", default=["v1", "v2"])
    p.add_argument("--risk-version", choices=["v1", "v2"], default=None,
                   help="formula used by the LIVE sweep (default: the version being swept)")
    p.add_argument("--out", default="")
    a = p.parse_args()

    groups = load_groups(a.path)
    print(f"loaded {len(groups)} diagnoses from {a.path}\n")

    frames = []
    for v in a.versions:
        if a.mode == "replay":
            df = replay(groups, a.thresholds, version=v)
        else:
            df = asyncio.run(live_sweep(groups, a.thresholds, version=v,
                                        risk_version=a.risk_version or v))
        frames.append(df)

    out = pd.concat(frames, ignore_index=True)
    cols = ["risk_version", "threshold", "n", "escalated", "human_rate", "success_rate",
            "unsafe_rate", "failures", "mttr_s", "decision_latency_s", "mcp_calls"]
    out = out[cols]
    print(out.to_string(index=False))

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(a.out, index=False)
        print(f"\n-> {a.out}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for v, df in zip(a.versions, frames):
        axes[0].plot(df["threshold"], df["success_rate"], marker="o", label=f"risk {v}")
        axes[1].plot(df["human_rate"], df["unsafe_rate"], marker="o", label=f"risk {v}")
    axes[0].axhline(0.75, ls="--", c="gray", lw=1)
    axes[0].set_xlabel("risk threshold")
    axes[0].set_ylabel("success rate")
    axes[0].set_title("Success vs threshold")
    axes[0].legend()
    axes[1].set_xlabel("human intervention rate")
    axes[1].set_ylabel("unsafe-action rate")
    axes[1].set_title("Safety vs human load")
    axes[1].legend()
    fig.tight_layout()
    (RESULTS_DIR / "figs").mkdir(parents=True, exist_ok=True)
    fig.savefig(RESULTS_DIR / "figs" / "threshold_tradeoff.png", dpi=200)
    print(f"-> {RESULTS_DIR / 'figs' / 'threshold_tradeoff.png'}")


if __name__ == "__main__":
    main()
