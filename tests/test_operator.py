import pytest

from remediation.operator_model import SimulatedOperator, is_dangerous


def plan(action="restart_service", service="service-b"):
    return {"service": service, "suspected_fault": "CRASH", "action": action, "confidence": 0.9}


def test_correct_safe_plan_is_usually_approved():
    op = SimulatedOperator(seed=0)
    verdicts = [op.review(plan("restart_service", "service-b"), "CRASH")[0] for _ in range(400)]
    assert verdicts.count("approve") / len(verdicts) > 0.85


def test_wrong_plan_is_mostly_rejected():
    op = SimulatedOperator(seed=0)
    verdicts = [op.review(plan("scale_service", "service-b"), "CRASH")[0] for _ in range(400)]
    assert verdicts.count("reject") / len(verdicts) > 0.80


def test_review_load_causes_alert_fatigue():
    """A saturated operator catches fewer wrong plans and rejects more good ones."""
    tired = SimulatedOperator(seed=0)
    fresh = SimulatedOperator(seed=0)
    wrong_tired = [tired.review(plan("scale_service"), "CRASH", review_load=1.0)[0] for _ in range(600)]
    wrong_fresh = [fresh.review(plan("scale_service"), "CRASH", review_load=0.0)[0] for _ in range(600)]
    good_tired = [tired.review(plan("restart_service"), "CRASH", review_load=1.0)[0] for _ in range(600)]
    good_fresh = [fresh.review(plan("restart_service"), "CRASH", review_load=0.0)[0] for _ in range(600)]
    assert wrong_tired.count("reject") < wrong_fresh.count("reject")   # catches fewer
    assert good_tired.count("reject") > good_fresh.count("reject")     # false-rejects more


def test_restart_of_a_shared_dependency_counts_as_dangerous():
    assert is_dangerous(plan("restart_service", "service-a")) is True
    assert is_dangerous(plan("restart_service", "service-b")) is False
    assert is_dangerous(plan("reroute_traffic", "service-b")) is True
    assert is_dangerous(plan("scale_service", "service-b")) is True


def test_review_latency_is_scaled_by_the_median():
    op = SimulatedOperator(seed=0, latency_median=45)
    delays = [op.review(plan(), "CRASH")[1] for _ in range(200)]
    assert all(36 <= d <= 54 for d in delays)


def test_manual_fix_can_fail():
    op = SimulatedOperator(seed=0, manual_success_rate=0.0)
    assert op.attempt_manual_fix() is False
    op2 = SimulatedOperator(seed=0, manual_success_rate=1.0)
    assert op2.attempt_manual_fix() is True
