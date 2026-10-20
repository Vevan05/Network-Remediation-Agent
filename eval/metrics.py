"""
Aggregate a runs_<tag>.jsonl into the paper metrics per policy mode.

  1. remediation success rate     mean(recovered)
  2. mean time to recovery        mean(mttr_s) over RECOVERED runs (also report #failures!)
  3. unsafe action rate           mean(unsafe_action)      - collateral damage was MEASURED
  4. incorrect action rate        mean(incorrect_action)   - the action had no effect
  5. human intervention rate      mean(human_intervention)
  6. decision latency             mean(decision_latency_s) - time the agent spent thinking
  7. communication overhead       mean(mcp_calls)          - MCP tool calls per incident

  python -m eval.metrics results/runs_benchmark_v2.jsonl
"""
import math
import sys
from pathlib import Path

import pandas as pd

from remediation.config import RESULTS_DIR

ORDER = ["full-auto", "adaptive", "full-human"]


def load(path: str) -> pd.DataFrame:
    df = pd.read_json(path, lines=True)
    if "error" in df and df["error"].notna().any():
        print(f"WARNING: {int(df['error'].notna().sum())} rows have errors - inspect before trusting results")
    return df[df["error"].isna()] if "error" in df else df


def mcnemar_exact(b: int, c: int) -> float:
    """
    Exact (binomial) McNemar test for paired proportions.
    b = cases where mode A failed and mode B succeeded
    c = cases where mode A succeeded and mode B failed
    H0: the two policies are equally likely to fail.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def paired_comparison(df: pd.DataFrame, column: str = "recovered", label: str = "failure") -> str:
    """
    The design is paired: every diagnosis is replayed under all three policies, so
    the informative comparison is within-diagnosis, not between independent samples.
    McNemar's exact test on the discordant pairs.
    """
    modes = [m for m in ORDER if m in set(df["mode"])]
    n_groups = df["run_id"].nunique()
    lines = [f"", f"Paired comparison on '{label}' ({n_groups} diagnoses replayed under each policy)",
             "  McNemar exact test on the discordant pairs:", ""]
    for i, m1 in enumerate(modes):
        for m2 in modes[i + 1:]:
            a = df[df["mode"] == m1].set_index("run_id")
            b = df[df["mode"] == m2].set_index("run_id")
            common = a.index.intersection(b.index)
            ba = a.loc[common, column].astype(bool)
            bb = b.loc[common, column].astype(bool)
            # "bad" = failure (recovered==False) or unsafe (unsafe_action==True)
            bad_a = ~ba if column == "recovered" else ba
            bad_b = ~bb if column == "recovered" else bb
            only_a = int((bad_a & ~bad_b).sum())   # m1 bad, m2 good
            only_b = int((~bad_a & bad_b).sum())   # m2 bad, m1 good
            p = mcnemar_exact(only_a, only_b)
            verdict = "significant" if p < 0.05 else "not significant"
            lines.append(f"    {m1:>10} vs {m2:<10} n={len(common):3d}  "
                         f"{m1}-only-{label}={only_a:2d}  {m2}-only-{label}={only_b:2d}  "
                         f"p={p:.4f}  ({verdict})")
    return "\n".join(lines)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def ci(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"[{lo:.0%}, {hi:.0%}]"


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("mode")
    out = pd.DataFrame({
        "n": g.size(),
        "success_rate": g["recovered"].mean(),
        "mttr_s": g["mttr_s"].mean(),
        "n_failed": g["recovered"].apply(lambda s: int((~s.astype(bool)).sum())),
        "unsafe_rate": g["unsafe_action"].mean(),
        "incorrect_rate": g["incorrect_action"].mean(),
        "human_rate": g["human_intervention"].mean(),
        "decision_latency_s": g["decision_latency_s"].mean(),
        "mcp_calls": g["mcp_calls"].mean(),
    })
    out = out.reindex([m for m in ORDER if m in out.index]).round(3)
    # 95% Wilson intervals: with n=36 a difference of a few runs is not a result.
    out["success_ci95"] = [ci(int(r["success_rate"] * n), n) for n, r in zip(out["n"], out.to_dict("records"))]
    out["unsafe_ci95"] = [ci(int(round(r["unsafe_rate"] * n)), n) for n, r in zip(out["n"], out.to_dict("records"))]
    return out


def to_markdown(t: pd.DataFrame) -> str:
    cols = ["mode"] + list(t.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for mode, r in t.iterrows():
        cells = [str(int(v)) if col == "n" else str(v) for col, v in r.items()]
        lines.append("| " + " | ".join([mode] + cells) + " |")
    return "\n".join(lines)


def to_markdown_pretty(t: pd.DataFrame) -> str:
    """Same table, human-readable, with the confidence intervals spelled out."""
    order = ["n", "success_rate", "success_ci95", "mttr_s", "n_failed", "unsafe_rate", "unsafe_ci95",
             "incorrect_rate", "human_rate", "decision_latency_s", "mcp_calls"]
    lines = ["| Policy | n | Success | 95% CI | MTTR (s) | Failed | Unsafe | 95% CI | Ineffective | Human | Decision latency (s) | MCP calls |",
             "| :--- | ---: | ---: | :---: | ---: | ---: | ---: | :---: | ---: | ---: | ---: | ---: |"]
    for mode, r in t.iterrows():
        n = int(r["n"])
        lines.append(
            f"| **{mode}** | {n} | {r['success_rate']:.1%} | {r['success_ci95']} | {r['mttr_s']:.2f} | "
            f"{int(r['n_failed'])} | {r['unsafe_rate']:.1%} | {r['unsafe_ci95']} | {r['incorrect_rate']:.1%} | "
            f"{r['human_rate']:.1%} | {r['decision_latency_s']:.2f} | {r['mcp_calls']:.2f} |")
    return "\n".join(lines)


def plots(t: pd.DataFrame, out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    for col in t.columns.drop("n"):
        fig, ax = plt.subplots(figsize=(4, 3))
        ax.bar(t.index, t[col], color=["#9ca3af", "#6366f1", "#d1d5db"][: len(t)])
        ax.set_title(col)
        ax.set_ylabel(col)
        fig.tight_layout()
        fig.savefig(out_dir / f"{col}.png", dpi=200)
        plt.close(fig)


def risk_audit(df: pd.DataFrame) -> str:
    """
    The question the risk layer has to answer: does a HIGH risk score actually mean
    "something is wrong with this plan"? Compare the score distributions of the plans
    that failed against the ones that worked, and report how much they overlap.
    """
    auto = df[df["mode"] == "full-auto"]
    if auto.empty:
        return ""
    failed = auto[~auto["recovered"].astype(bool)]
    ok = auto[auto["recovered"].astype(bool)]
    lines = ["", "Risk score: failed plans vs successful plans (full-auto rows)", ""]
    for name, s in (("failed", failed["risk_score"]), ("succeeded", ok["risk_score"])):
        lines.append(f"  {name:10s} n={len(s):3d}  min={s.min():.4f}  max={s.max():.4f}  mean={s.mean():.4f}")
    if len(failed) and len(ok):
        lo, hi = max(failed["risk_score"].min(), ok["risk_score"].min()), min(failed["risk_score"].max(), ok["risk_score"].max())
        overlap = lo <= hi
        inside_f = int(failed["risk_score"].between(ok["risk_score"].min(), ok["risk_score"].max()).sum())
        inside_o = int(ok["risk_score"].between(failed["risk_score"].min(), failed["risk_score"].max()).sum())
        pairs = [(f, o) for f in failed["risk_score"] for o in ok["risk_score"]]
        auc = sum(1 for f, o in pairs if f > o) + 0.5 * sum(1 for f, o in pairs if f == o)
        auc /= len(pairs)
        lines += [
            "",
            f"  overlap region: [{lo:.4f}, {hi:.4f}]  ->  {'OVERLAP' if overlap else 'SEPARABLE'}",
            f"  failed plans inside the successful range:   {inside_f}/{len(failed)}",
            f"  successful plans inside the failed range:    {inside_o}/{len(ok)}",
            f"  AUC(risk -> failure) = {auc:.3f}   (1.0 perfect, 0.5 random, 0.0 anti-correlated)",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    path = sys.argv[1]
    df = load(path)
    table = aggregate(df)
    print(to_markdown_pretty(table))
    print(risk_audit(df))
    print(paired_comparison(df, "recovered", "failure"))
    print(paired_comparison(df, "unsafe_action", "unsafe"))
    plots(table, RESULTS_DIR / "figs")
    print(f"\nfigures -> {RESULTS_DIR / 'figs'}")
