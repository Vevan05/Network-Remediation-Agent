"""
The simulated human operator.

METHODOLOGY.md 1.A defines the verdict by comparing the proposed action against
the ground truth for the injected fault:
  - correct & safe      -> approve with prob 1 - beta
  - correct & dangerous  -> approve with prob 1 - alpha_unsafe
  - incorrect            -> approve with prob 1 - alpha_incorrect

The operator is stochastic on purpose (no "perfect oracle"), and its accuracy
degrades with review load (alert fatigue): with load L in [0,1],
  alpha_adj = alpha_base * (1 - 0.2 L)      beta_adj = beta_base + 0.1 L
so a lightly loaded operator (adaptive: only a fraction of incidents reaches a
human) is MORE accurate than one drowning in the full-human queue.
"""
import random

from .config import DEPENDENTS, FIX_MATRIX, HUMAN_REVIEW_DELAY_S

# Actions that are correct for a fault but touch more than the target.
DANGEROUS_ACTIONS = {"scale_service", "reroute_traffic"}


def is_dangerous(plan: dict) -> bool:
    """
    "Correct but dangerous": the action is a legitimate remedy, but it has a blast
    radius beyond the target. Restarting a shared dependency counts: it recovers
    the target but knocks out everything that depends on it.
    """
    if plan["action"] in DANGEROUS_ACTIONS:
        return True
    return plan["action"] == "restart_service" and plan.get("service") in DEPENDENTS


class SimulatedOperator:
    """
    High-fidelity human model that operates based on observed outcomes.

    Attributes:
        alpha_incorrect: P(reject | plan is wrong)
        alpha_unsafe: P(reject | plan is correct but dangerous)
        beta: P(reject | plan is correct and safe) - False Rejection Rate
        manual_success_rate: P(recover | manual fix attempt)
        latency_median: Median review time (s)
    """

    def __init__(self,
                 alpha_incorrect=0.90,
                 alpha_unsafe=0.70,
                 beta=0.05,
                 manual_success_rate=0.95,
                 latency_median=HUMAN_REVIEW_DELAY_S,
                 seed=1):
        self.alpha_incorrect = alpha_incorrect
        self.alpha_unsafe = alpha_unsafe
        self.beta = beta
        self.manual_success_rate = manual_success_rate
        self.latency_median = latency_median
        self.rng = random.Random(seed)

    def review(self, plan: dict, fault: str, review_load: float = 0.0) -> tuple[str, float]:
        """
        Returns (verdict, simulated_delay).
        review_load: Normalized load (0.0 to 1.0). High load increases false rejections
                     and decreases catch rates (alert fatigue).
        """
        # Calculate load-adjusted accuracy
        # As load increases, catch rate drops and false rejections rise
        adj_alpha_inc = self.alpha_incorrect * (1.0 - 0.2 * review_load)
        adj_alpha_uns = self.alpha_unsafe * (1.0 - 0.2 * review_load)
        adj_beta = self.beta + (0.1 * review_load)

        correct = plan["action"] in FIX_MATRIX[fault]
        dangerous = is_dangerous(plan)

        if correct and not dangerous:
            # Fully safe and correct: mostly approved, some false rejections
            verdict = "reject" if self.rng.random() < adj_beta else "approve"
        elif correct and dangerous:
            # Correct but dangerous: high chance of rejection
            verdict = "reject" if self.rng.random() < adj_alpha_uns else "approve"
        else:
            # Incorrect: high chance of rejection
            verdict = "reject" if self.rng.random() < adj_alpha_inc else "approve"

        # Latency modeled as a simple variation around the median
        delay = self.latency_median * self.rng.uniform(0.8, 1.2)
        return verdict, delay

    def attempt_manual_fix(self) -> bool:
        """Did the operator's by-hand fix actually restore the service?"""
        return self.rng.random() < self.manual_success_rate
