import ast
import inspect
import pathlib

import pytest

from remediation import risk
from remediation.risk import (calibrated_confidence, effective_blast_radius, evidence_agreement,
                              rubric_consistency, score_plan)

HEALTHY_LATENCY_EV = {"status": "degraded", "latency_ms_p50": 600, "packet_loss_pct": 0.0}


def plan(**kw):
    base = {"service": "service-b", "suspected_fault": "HIGH_LATENCY", "action": "reroute_traffic",
            "confidence": 0.9, "evidence": HEALTHY_LATENCY_EV}
    base.update(kw)
    return base


def test_score_is_bounded():
    for version in ("v1", "v2"):
        for action in ("restart_service", "reapply_config", "reroute_traffic", "scale_service"):
            for conf in (0.0, 0.5, 1.0):
                s = score_plan(plan(action=action, confidence=conf), version=version)["score"]
                assert 0.0 <= s <= 1.0


def test_lower_confidence_means_higher_risk():
    assert score_plan(plan(confidence=0.3))["score"] > score_plan(plan(confidence=0.9))["score"]


def test_reroute_has_a_bigger_blast_radius_than_restart():
    # v1 scored the action in isolation, so this held for the TOTAL score too.
    assert (score_plan(plan(action="reroute_traffic"), version="v1")["score"]
            > score_plan(plan(action="restart_service"), version="v1")["score"])
    # v2 compares the total score, which also prices in the rubric violation of
    # restarting a service that has latency, so check the blast term itself.
    assert (score_plan(plan(action="reroute_traffic"))["breakdown"]["blast_radius"]
            > score_plan(plan(action="restart_service"))["breakdown"]["blast_radius"])


def test_critical_service_riskier():
    assert score_plan(plan(service="service-a"))["score"] > score_plan(plan(service="service-c"))["score"]


def test_evidence_agreement_and_calibration():
    assert evidence_agreement("HIGH_LATENCY", HEALTHY_LATENCY_EV) == 1.0
    assert evidence_agreement("PACKET_LOSS", HEALTHY_LATENCY_EV) == 0.0
    # An over-confident LLM claiming a fault the metrics do not support gets pulled down.
    assert calibrated_confidence(plan(suspected_fault="PACKET_LOSS", confidence=0.95)) < 0.5


def test_breakdown_sums_to_score():
    for version in ("v1", "v2"):
        r = score_plan(plan(), version=version)
        assert abs(sum(r["breakdown"].values()) - r["score"]) < 1e-3


# --- v2: the rubric-consistency term -----------------------------------------

def test_rubric_consistency_uses_the_agents_own_claim():
    assert rubric_consistency(plan(action="reroute_traffic")) == 1.0
    # right fault, wrong remedy -> inconsistent
    assert rubric_consistency(plan(action="restart_service")) == 0.0
    # unknown fault -> cannot vouch for the remedy
    assert rubric_consistency(plan(suspected_fault="MYSTERY")) == 0.0


def test_rubric_violation_is_riskier_in_v2_but_invisible_in_v1():
    good, bad = plan(action="reroute_traffic"), plan(action="restart_service")
    assert score_plan(bad, version="v2")["score"] > score_plan(good, version="v2")["score"]
    # v1 cannot tell them apart beyond the blast radius: this is the bug the
    # benchmark exposed (risk score was anti-correlated with failure).
    assert score_plan(bad, version="v1")["score"] < score_plan(good, version="v1")["score"]


def test_risk_layer_never_reads_the_evaluation_oracle():
    """
    The rubric term must be a consistency check against the published runbook,
    never against the ground-truth FIX_MATRIX. If risk.py read FIX_MATRIX the
    risk layer would be reading the answer key.
    """
    tree = ast.parse(pathlib.Path(inspect.getfile(risk)).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "FIX_MATRIX" not in imported
    assert "FIX_MATRIX" not in used
    assert "REMEDIATION_RUBRIC" in imported


def test_rubric_matches_the_published_runbook_not_the_oracle():
    from remediation.config import FIX_MATRIX, REMEDIATION_RUBRIC
    assert REMEDIATION_RUBRIC == FIX_MATRIX  # same runbook ...
    assert REMEDIATION_RUBRIC is not FIX_MATRIX  # ... but a separate, documented object


# --- v2: context-aware blast radius -----------------------------------------

def test_gratuitous_restart_of_a_shared_dependency_is_destructive():
    up = plan(service="service-a", action="restart_service", evidence={"status": "degraded"})
    down = plan(service="service-a", action="restart_service", evidence={"status": "down"})
    assert effective_blast_radius(up) > effective_blast_radius(down)
    assert effective_blast_radius(up) >= risk.SHARED_DEPENDENCY_BLAST
    # restarting a leaf service is still cheap
    leaf = plan(service="service-c", action="restart_service", evidence={"status": "degraded"})
    assert effective_blast_radius(leaf) == risk.ACTION_BLAST_RADIUS["restart_service"]


def test_shared_dependency_restart_scores_higher_than_a_leaf_restart():
    shared = plan(service="service-a", action="restart_service", evidence={"status": "degraded"})
    leaf = plan(service="service-c", action="restart_service", evidence={"status": "degraded"})
    assert score_plan(shared)["score"] > score_plan(leaf)["score"]
