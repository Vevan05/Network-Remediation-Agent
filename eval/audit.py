"""
Escalation audit: did the risk router escalate the RIGHT plans?

For the adaptive policy this prints
  - what was escalated (and what was NOT)
  - how many escalated runs were failures vs successes
  - the risk-score distribution of failed vs successful plans, and how much they
    overlap (with the AUC of the risk score as a failure predictor)
  - the counterfactual: which plans SHOULD have been escalated

  python -m eval.audit results/runs_final_benchmark.jsonl
"""
import argparse

import pandas as pd

from remediation.config import ADAPTIVE_THRESHOLD
from remediation.risk import score_plan


def auc_score(failed: pd.Series, ok: pd.Series) -> float:
    """P(risk(failed) > risk(succeeded)) - 1.0 perfect, 0.5 random, 0.0 anti-correlated."""
    pairs = [(f, o) for f in failed for o in ok]
    wins = sum(1 for f, o in pairs if f > o) + 0.5 * sum(1 for f, o in pairs if f == o)
    return wins / len(pairs)


def audit(path: str, threshold: float = ADAPTIVE_THRESHOLD) -> None:
    df = pd.read_json(path, lines=True)
    if "error" in df and df["error"].notna().any():
        print(f"NOTE: {int(df['error'].notna().sum())} rows have errors and are excluded\n")
        df = df[df["error"].isna()]

    ad = df[df["mode"] == "adaptive"]
    auto = df[df["mode"] == "full-auto"]
    print("=" * 100)
    print(f"ESCALATION AUDIT  ({path})   threshold={threshold}")
    print("=" * 100)

    # ---------- what the router escalated ----------
    esc = ad[ad["routing"] == "require_approval"]
    print(f"\n1. WHAT THE ROUTER ESCALATED  ({len(esc)} of {len(ad)} adaptive runs "
          f"= {len(esc)/max(len(ad),1):.1%} human load)")
    if len(esc):
        cols = ["scenario_id", "rep", "fault", "target", "suspected_fault", "plan_action",
                "plan_correct", "confidence_used", "risk_score", "verdict", "recovered"]
        print(esc[[c for c in cols if c in esc.columns]].to_string(index=False))
        n_fail = int((~esc["recovered"].astype(bool)).sum())
        print(f"\n   escalated runs that FAILED : {n_fail}/{len(esc)}")
        print(f"   escalated runs that SUCCEEDED: {len(esc)-n_fail}/{len(esc)}")
        print(f"   escalated plans that were CORRECT: {int(esc['plan_correct'].astype(bool).sum())}/{len(esc)}")

    not_esc = ad[ad["routing"] == "auto_execute"]
    if len(not_esc):
        n_fail = int((~not_esc["recovered"].astype(bool)).sum())
        print(f"\n   auto-executed runs that FAILED: {n_fail}/{len(not_esc)}   "
              f"(auto-executed plans that were WRONG: {int((~not_esc['plan_correct'].astype(bool)).sum())}/{len(not_esc)})")

    # ---------- risk score separation ----------
    print(f"\n2. RISK SCORE OF FAILED vs SUCCEEDED RUNS")
    failed = auto[~auto["recovered"].astype(bool)]["risk_score"]
    ok = auto[auto["recovered"].astype(bool)]["risk_score"]
    for name, s in (("failed", failed), ("succeeded", ok)):
        if len(s):
            print(f"   {name:10s} n={len(s):3d}  min={s.min():.4f}  max={s.max():.4f}  mean={s.mean():.4f}")
    if len(failed) and len(ok):
        lo = max(failed.min(), ok.min())
        hi = min(failed.max(), ok.max())
        inside_f = int(failed.between(ok.min(), ok.max()).sum())
        inside_o = int(ok.between(failed.min(), failed.max()).sum())
        auc = auc_score(failed, ok)
        print(f"\n   overlap region: [{lo:.4f}, {hi:.4f}]  ->  {'OVERLAP (the formula cannot separate them)' if lo <= hi else 'SEPARABLE'}")
        print(f"   failed plans inside the successful range:   {inside_f}/{len(failed)}")
        print(f"   successful plans inside the failed range:    {inside_o}/{len(ok)}")
        print(f"   AUC(risk -> failure) = {auc:.3f}   (1.0 perfect, 0.5 random, <0.5 anti-correlated)")
        print(f"   failed plans ABOVE the threshold (should have been escalated): "
              f"{int((failed >= threshold).sum())}/{len(failed)}")
        print(f"   correct plans BELOW the threshold (needlessly escalated):      "
              f"{int((ok >= threshold).sum())}/{len(ok)}")

    # ---------- counterfactual: which plans should have been escalated ----------
    print(f"\n3. COUNTERFACTUAL: WHICH PLANS SHOULD HAVE BEEN ESCALATED?")
    from eval.sweep import reconstruct_plan
    per_scen = auto.drop_duplicates("scenario_id")[[
        "scenario_id", "fault", "target", "suspected_fault", "plan_action", "plan_correct",
        "confidence_used", "risk_score"]].copy()
    # Recompute both formulas from the plan so the audit works on files that were
    # recorded before the run rows carried both scores.
    plans = [reconstruct_plan(r) for _, r in auto.drop_duplicates("scenario_id").iterrows()]
    per_scen["v1"] = [score_plan(p, version="v1")["score"] for p in plans]
    per_scen["v2"] = [score_plan(p, version="v2")["score"] for p in plans]
    per_scen["should_esc"] = ~per_scen["plan_correct"].astype(bool)
    per_scen["v1_esc"] = per_scen["v1"] >= threshold
    per_scen["v2_esc"] = per_scen["v2"] >= threshold
    print(per_scen[["scenario_id", "fault", "plan_action", "plan_correct", "v1", "v2",
                    "v1_esc", "v2_esc"]].to_string(index=False))
    for v in ("v1", "v2"):
        wrong = per_scen[per_scen[f"{v}_esc"] != per_scen["should_esc"]]
        print(f"\n   {v}: routing decision correct for {len(per_scen)-len(wrong)}/{len(per_scen)} scenarios; "
              f"{len(wrong)} misrouted -> {list(wrong['scenario_id'])}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--threshold", type=float, default=ADAPTIVE_THRESHOLD)
    a = p.parse_args()
    audit(a.path, a.threshold)
