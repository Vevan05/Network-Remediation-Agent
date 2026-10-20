"""
Tests for the collateral-damage definition and the shared-dependency topology.

These are pure unit tests: they do NOT need Docker.
"""
import pytest

from remediation.config import DEPENDENTS
from remediation.faults import check_collateral_damage


def svc(status="ok", latency=8):
    return {"status": status, "latency_ms": latency}


def test_no_collateral_when_nothing_changes():
    base = {"service-a": svc(), "service-b": svc(), "service-c": svc()}
    assert check_collateral_damage(base, base, "service-a") is False


def test_status_degradation_of_a_non_target_is_collateral():
    base = {"service-a": svc(), "service-b": svc(), "service-c": svc()}
    after = {**base, "service-b": svc(status="degraded", latency=260)}
    assert check_collateral_damage(base, after, "service-a") is True


def test_the_target_service_is_never_blamed():
    base = {"service-a": svc(status="degraded", latency=600), "service-b": svc(), "service-c": svc()}
    after = {"service-a": svc(status="ok", latency=8), "service-b": svc(), "service-c": svc()}
    assert check_collateral_damage(base, after, "service-a") is False


def test_latency_jitter_is_not_collateral():
    """An 8ms baseline that jitters to 14ms is noise, not damage."""
    base = {"service-a": svc(), "service-b": svc(latency=8), "service-c": svc(latency=8)}
    after = {"service-a": svc(), "service-b": svc(latency=14), "service-c": svc(latency=9)}
    assert check_collateral_damage(base, after, "service-a") is False


def test_real_latency_spike_is_collateral():
    base = {"service-a": svc(), "service-b": svc(latency=8), "service-c": svc(latency=8)}
    after = {"service-a": svc(), "service-b": svc(latency=260), "service-c": svc(latency=8)}
    assert check_collateral_damage(base, after, "service-a") is True


def test_pre_existing_degradation_is_not_blamed_on_the_action():
    """A neighbour that was already degraded before the action stays degraded - not collateral."""
    base = {"service-a": svc(), "service-b": svc(status="degraded", latency=300), "service-c": svc()}
    after = {"service-a": svc(), "service-b": svc(status="degraded", latency=300), "service-c": svc()}
    assert check_collateral_damage(base, after, "service-a") is False


def test_service_a_is_the_shared_dependency():
    assert "service-a" in DEPENDENTS
    assert set(DEPENDENTS["service-a"]) == {"service-b", "service-c"}
    # leaves have no dependents, so restarting them is cheap
    assert "service-b" not in DEPENDENTS and "service-c" not in DEPENDENTS
