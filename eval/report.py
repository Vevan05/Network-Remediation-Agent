"""
One command to regenerate every number in README.md.

  python -m eval.report results/runs_benchmark_v2.jsonl

Prints, in order:
  1. the results table (with decision latency and MCP tool calls)
  2. the escalation audit (what the router escalated, and what it should have)
  3. the risk-score separation (failed vs succeeded, overlap, AUC)
  4. the threshold sweep (v1 and v2)
  5. the rubric-weight sensitivity sweep
"""
import argparse
import subprocess
import sys
from pathlib import Path

from eval.audit import audit
from eval.metrics import aggregate, load, risk_audit, to_markdown, to_markdown_pretty
from eval.sensitivity import main as sensitivity_main
from eval.sweep import load_groups, replay, replay_fixed

THRESHOLDS = [0.15, 0.30, 0.40, 0.50, 0.90]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path", nargs="?", default="results/runs_benchmark_v2.jsonl")
    p.add_argument("--thresholds", type=float, nargs="+", default=THRESHOLDS)
    a = p.parse_args()

    df = load(a.path)

    print("=" * 100)
    print("1. RESULTS")
    print("=" * 100)
    print(to_markdown_pretty(aggregate(df)))
    print(risk_audit(df))

    print()
    print("=" * 100)
    print("2. ESCALATION AUDIT")
    print("=" * 100)
    audit(a.path)

    print()
    print("=" * 100)
    print("3. THRESHOLD SWEEP (replay of the recorded benchmark)")
    print("=" * 100)
    groups = load_groups(a.path)
    for v in ("v1", "v2"):
        s = replay(groups, a.thresholds, version=v)
        cols = ["risk_version", "threshold", "n", "escalated", "human_rate", "success_rate",
                "unsafe_rate", "failures", "mttr_s", "decision_latency_s", "mcp_calls"]
        print(f"\n--- risk formula {v} ---")
        print(s[cols].to_string(index=False))

    print()
    print("=" * 100)
    print("3b. OPERATOR REVIEW-LOAD SENSITIVITY")
    print("     (the alert-fatigue model was never applied before v2: review_load was always 0)")
    print("=" * 100)
    print(f"{'policy':<12} {'review_load':>11} {'success':>8} {'unsafe':>8} {'mttr_s':>8} {'human_rate':>11}")
    for mode, loads in (("full-auto", [0.0]), ("full-human", [0.0, 0.25, 0.5, 1.0])):
        for load_level in loads:
            r = replay_fixed(groups, mode, version="v1", review_load=load_level)
            print(f"{mode:<12} {load_level:>11.2f} {r['success_rate']:>8.3f} {r['unsafe_rate']:>8.3f} "
                  f"{str(r['mttr_s']):>8} {r['human_rate']:>11.3f}")

    print()
    print("=" * 100)
    print("4. RUBRIC WEIGHT SENSITIVITY")
    print("=" * 100)
    sys.argv = ["sensitivity", a.path]
    sensitivity_main()


if __name__ == "__main__":
    main()
