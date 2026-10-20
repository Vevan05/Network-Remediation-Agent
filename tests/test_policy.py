import pytest

from remediation.policy import Routing, route


def test_full_modes_ignore_risk():
    assert route("full-auto", 0.99) is Routing.AUTO
    assert route("full-human", 0.0) is Routing.APPROVAL


def test_adaptive_threshold_boundary():
    assert route("adaptive", 0.39, threshold=0.4) is Routing.AUTO
    assert route("adaptive", 0.40, threshold=0.4) is Routing.APPROVAL


def test_unknown_mode():
    with pytest.raises(ValueError):
        route("yolo", 0.1)
