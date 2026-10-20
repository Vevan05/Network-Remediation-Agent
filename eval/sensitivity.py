"""
Weight sensitivity for the v2 risk formula.

The v2 rubric-consistency term is a structural fix, but its WEIGHT is a free
parameter. If the result only holds at one hand-picked weight it is a fit to the
benchmark, not a fix. This sweeps the rubric weight (and, separately, the
confidence weight) and reports, for each setting:

  AUC         - how well the score separates failures from successes
                (1.0 perfect, 0.5 random, <0.5 anti-correlated)
  misrouted   - scenarios whose routing decision is wrong (a wrong plan sent to
                the human, or a correct plan sent to the autopilot)
  caught      - wrong plans that end up above the threshold

  python -m eval.sensitivity results/runs_benchmark_v2.jsonl
"""
import argparse

import pandas as pd

from eval.sweep import load_groups, reconstruct_plan
from remediation.config import ADAPTIVE_THRESHOLD
from remediation.risk import RISK_WEIGHTS, score_plan


def auc_score(failed, ok):
    pairs = [(f, o) for f in failed for o in ok]
    if not pairs:
        return float("nan")
    wins = sum(1 for f, o in pairs if f > o) + 0.5 * sum(1 for f, o in pairs if f == o)
    return wins / len(pairs)


def evaluate(groups, weights, threshold):
    rows = []
    for g in groups:
        score = score_plan(g["plan"], weights=weights)["score"]
        rows.append({"score": score, "correct": g["plan_correct"], "risk": score})
    df = pd.DataFrame(rows)
    failed = df[~df["correct"]]["score"]
    ok = df[df["correct"]]["score"]
    should_esc = ~df["correct"]
    is_esc = df["score"] >= threshold
    return {
        "auc": auc_score(failed, ok),
        "caught": int((should_esc & is_esc).sum()),
        "missed": int((should_esc & ~is_esc).sum()),
        "false_esc": int((~should_esc & is_esc).sum()),
        "misrouted": int((should_esc != is_esc).sum()),
        "human_rate": round(float(is_esc.mean()), 3),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--threshold", type=float, default=ADAPTIVE_THRESHOLD)
    a = p.parse_args()

    groups = load_groups(a.path)
    # ground truth per diagnosis
    from remediation.config import FIX_MATRIX
    for g in groups:
        g["plan_correct"] = g["plan"]["action"] in FIX_MATRIX[g["fault"]]

    base = dict(RISK_WEIGHTS["v2"])
    print(f"Sweeping the rubric weight (threshold={a.threshold}, {len(groups)} diagnoses)\n")
    print(f"{'w_rubric':>9} {'AUC':>7} {'caught':>7} {'missed':>7} {'false_esc':>10} {'misrouted':>10} {'human_rate':>11}")
    rows = []
    for w_rub in (0.00, 0.10, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80):
        others = {k: v for k, v in base.items() if k != "rubric_consistency"}
        rest = sum(others.values())
        # rescale the remaining terms so the weights still sum to 1
        scaled = {k: v * (1.0 - w_rub) / rest for k, v in others.items()}
        scaled["rubric_consistency"] = w_rub
        w = dict(scaled)
        r = evaluate(groups, w, a.threshold)
        rows.append({"w_rubric": w_rub, **r})
        print(f"{w_rub:>9.2f} {r['auc']:>7.3f} {r['caught']:>7} {r['missed']:>7} "
              f"{r['false_esc']:>10} {r['misrouted']:>10} {r['human_rate']:>11.3f}")

    print(f"\nThe v1 formula (no rubric term at all) for comparison:")
    r = evaluate(groups, RISK_WEIGHTS["v1"], a.threshold)
    print(f"{'  v1':>9} {r['auc']:>7.3f} {r['caught']:>7} {r['missed']:>7} "
          f"{r['false_esc']:>10} {r['misrouted']:>10} {r['human_rate']:>11.3f}")
    print("\n'caught' = wrong plans escalated (good). 'false_esc' = correct plans escalated (wasted human time).")

    pd.DataFrame(rows).to_csv("results/risk_weight_sensitivity.csv", index=False)
    print("\n-> results/risk_weight_sensitivity.csv")


if __name__ == "__main__":
    main()
