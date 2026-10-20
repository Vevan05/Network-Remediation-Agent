"""
Autonomy policy: same agent, same risk scorer, three routing rules.

  full-auto   always execute automatically (no human gate)
  full-human  every plan needs human approval
  adaptive    risk < threshold -> auto, otherwise human approval   <- the contribution
"""
from enum import Enum

from .config import ADAPTIVE_THRESHOLD

MODES = ("full-auto", "full-human", "adaptive")


class Routing(str, Enum):
    AUTO = "auto_execute"
    APPROVAL = "require_approval"


def route(mode: str, risk_score: float, threshold: float = ADAPTIVE_THRESHOLD) -> Routing:
    if mode == "full-auto":
        return Routing.AUTO
    if mode == "full-human":
        return Routing.APPROVAL
    if mode == "adaptive":
        return Routing.AUTO if risk_score < threshold else Routing.APPROVAL
    raise ValueError(f"unknown mode {mode}")
