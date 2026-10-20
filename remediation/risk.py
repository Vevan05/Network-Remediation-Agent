"""
Risk scoring: the paper's core mechanism.

  v1:  risk = w_c(1-confidence) + w_i*impact + w_b*blast_radius + w_r(1-reversibility)
  v2:  risk = w_c(1-confidence) + w_i*impact + w_b*blast_radius + w_r(1-reversibility)
                           + w_x(1-rubric_consistency)

Every term is in [0,1], weights sum to 1, so risk is in [0,1] (higher = riskier).
It is an explicit rubric, not a learned model: defensible at this scope, and the
weights/threshold become an ablation you can report.

Why v2 exists (see README "Findings"):

  v1 scores how DANGEROUS an action is on a service, not whether the action is
  VALID for the fault. Both of its inputs - blast radius and impact - are static
  per (action, service), so two plans for the same service and action get the same
  score no matter how wrong one of them is. Measured on the LLM benchmark, v1's
  score was ANTI-correlated with failure (AUC = 0.32): the router escalated the
  correct plans and auto-executed every wrong one.

  v2 adds two orthogonal signals, both computed from the agent's OWN output:

  1. rubric_consistency - does the proposed action match the published runbook
     (config.REMEDIATION_RUBRIC) for the fault the agent CLAIMED? A plan that
     says "HIGH_LATENCY -> restart_service" contradicts the runbook, and the
     agent's confidence/evidence cannot detect that (it only ever validated the
     claimed fault, never the remedy). NOTE: this compares the plan to a runbook,
     never to the evaluation oracle (config.FIX_MATRIX) - see the guard test in
     tests/test_risk.py.
  2. context-aware blast radius - restarting a shared dependency (service-a)
     that was not even down knocks out its dependents, so it is far more
     destructive than the static 0.2 for restart_service.

Confidence is NOT taken at face value from the LLM. It is blended with an
evidence-agreement score (do the observed metrics actually match the claimed
fault signature?) to reduce over-confidence - see calibrated_confidence().
"""
from .config import ACTIONS, DEPENDENTS, REMEDIATION_RUBRIC

# --- v1 (original formula, kept for the ablation) ---
DEFAULT_WEIGHTS = {"confidence": 0.25, "impact": 0.25, "blast_radius": 0.25, "reversibility": 0.25}

# --- v2 (current) ---
# rubric_consistency dominates because an action that contradicts the runbook is
# almost certainly wrong; the remaining terms order the plans that survive it.
V2_WEIGHTS = {"confidence": 0.20, "impact": 0.15, "blast_radius": 0.20,
              "reversibility": 0.10, "rubric_consistency": 0.35}

RISK_WEIGHTS = {"v1": DEFAULT_WEIGHTS, "v2": V2_WEIGHTS}

# Static criticality of each service (stand-in for business impact). Vary them so impact matters.
SERVICE_CRITICALITY = {"service-a": 0.9, "service-b": 0.6, "service-c": 0.3}

ACTION_BLAST_RADIUS = {"restart_service": 0.2, "reapply_config": 0.3, "scale_service": 0.5, "reroute_traffic": 0.8}
ACTION_REVERSIBILITY = {"restart_service": 0.9, "reapply_config": 0.7, "scale_service": 0.8, "reroute_traffic": 0.4}

# Blast radius of a gratuitous restart of a shared dependency: it takes every
# dependent down with it (config.DEPENDENTS).
SHARED_DEPENDENCY_BLAST = 0.75

# Does the observed evidence for the target service match the claimed fault?
SIGNATURES = {
    "CRASH": lambda ev: ev.get("status") == "down",
    "CONFIG_DRIFT": lambda ev: ev.get("config_checksum_ok") is False,
    "PACKET_LOSS": lambda ev: ev.get("packet_loss_pct", 0) >= 5,
    "HIGH_LATENCY": lambda ev: ev.get("latency_ms_p50", ev.get("latency_ms", 0)) > 150
                               and ev.get("packet_loss_pct", 0) < 5,
}


def evidence_agreement(fault: str, evidence: dict) -> float:
    sig = SIGNATURES.get(fault)
    return 1.0 if (sig and evidence and sig(evidence)) else 0.0


def rubric_consistency(plan: dict) -> float:
    """
    1.0 if the proposed action is the runbook's remedy for the fault the agent CLAIMED.

    This reads only the plan. It never sees the injected fault, so a plan that names
    the wrong fault but proposes the right action still scores 1.0 - it is a
    consistency check against the published runbook, not the evaluation oracle.
    """
    expected = REMEDIATION_RUBRIC.get(plan.get("suspected_fault", ""))
    if not expected:
        return 0.0
    return 1.0 if plan.get("action") in expected else 0.0


def effective_blast_radius(plan: dict) -> float:
    """
    Blast radius, corrected for context.

    A restart of a shared dependency that was NOT down is gratuitous: it knocks
    out every service that depends on it. A restart of a service that IS down is
    the remedy for a crash and stays cheap.
    """
    base = ACTION_BLAST_RADIUS.get(plan.get("action"), 0.5)
    if plan.get("action") == "restart_service" and plan.get("service") in DEPENDENTS:
        status = (plan.get("evidence") or {}).get("status")
        if status != "down":  # the service was up: restarting it is gratuitous
            base = max(base, SHARED_DEPENDENCY_BLAST)
    return base


def calibrated_confidence(plan: dict, w_self: float = 0.5) -> float:
    self_report = min(max(float(plan.get("confidence", 0.5)), 0.0), 1.0)
    agree = evidence_agreement(plan.get("suspected_fault", ""), plan.get("evidence", {}))
    return w_self * self_report + (1 - w_self) * agree


def score_plan(plan: dict, weights: dict | None = None, calibrate: bool = True,
               version: str = "v2", context_aware_blast: bool | None = None) -> dict:
    """
    Score one plan. `version` selects the formula ("v1" | "v2").
    `context_aware_blast` defaults to on for v2 and off for v1.
    """
    if weights is None:
        w = RISK_WEIGHTS[version]
    else:
        w = weights
    assert abs(sum(w.values()) - 1.0) < 1e-6, "weights must sum to 1"
    if context_aware_blast is None:
        context_aware_blast = version == "v2"

    conf = calibrated_confidence(plan) if calibrate else float(plan.get("confidence", 0.5))
    impact = SERVICE_CRITICALITY.get(plan.get("service"), 0.5)
    blast = effective_blast_radius(plan) if context_aware_blast else ACTION_BLAST_RADIUS.get(plan.get("action"), 0.5)
    rev = ACTION_REVERSIBILITY.get(plan.get("action"), 0.5)

    breakdown = {
        "confidence": round(w["confidence"] * (1 - conf), 4),
        "impact": round(w["impact"] * impact, 4),
        "blast_radius": round(w["blast_radius"] * blast, 4),
        "reversibility": round(w["reversibility"] * (1 - rev), 4),
    }
    if "rubric_consistency" in w:
        breakdown["rubric_consistency"] = round(w["rubric_consistency"] * (1 - rubric_consistency(plan)), 4)

    return {"score": round(sum(breakdown.values()), 4),
            "confidence_used": round(conf, 3),
            "rubric_consistency": rubric_consistency(plan),
            "version": version,
            "max_term": round(max(w.values()), 4),
            "breakdown": breakdown}


def score_both(plan: dict) -> dict:
    """Score one plan under both formulas - used so every run row carries an ablation pair."""
    return {"v1": score_plan(plan, version="v1"), "v2": score_plan(plan, version="v2")}


assert set(ACTION_BLAST_RADIUS) == set(ACTION_REVERSIBILITY) == set(ACTIONS)
assert set(REMEDIATION_RUBRIC) == set(SIGNATURES)
