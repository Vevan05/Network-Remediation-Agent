"""
Executor: the ONLY code path that mutates the testbed.

It is deliberately NOT an MCP tool. The LLM can only *propose*; the
orchestrator calls execute() after the policy layer (auto) or a human
(approval) says go. This separation is the core safety argument of the paper.

Collateral damage is MEASURED, not assumed: the caller passes the health
snapshot taken just before the action (the post-injection baseline), and after
the action we compare every non-target service against it
(faults.check_collateral_damage). A fault that the injector caused is therefore
never blamed on the remediation.
"""
import time

from . import faults
from .config import ACTIONS, DEPENDENTS, NEIGHBOR


def execute(plan: dict, baseline: dict | None = None) -> dict:
    action, svc = plan["action"], plan["service"]
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    t0 = time.time()
    collateral, collateral_service = False, None
    collateral_services: list[str] = []

    if action == "restart_service":
        was_down = faults.health(svc).get("status") == "down"
        faults.docker("restart", "-t", "0", svc)  # also starts a stopped container
        faults.wait_up(svc)
        effective = was_down
        if not was_down:
            # A gratuitous restart of a SHARED dependency takes its dependents down
            # with it. This is the scenario that makes "restart the wrong thing"
            # genuinely unsafe, and it is what gives the risk layer a job.
            for dep in DEPENDENTS.get(svc, []):
                faults.post(dep, "/admin/fault", {"type": "COLLATERAL_LATENCY"})
                collateral_services.append(dep)
    else:
        body = faults.try_post(svc, "/admin/action", {"action": action})
        if body is None:
            # The service is down (a CRASH misdiagnosed as something else, say).
            # The action cannot be applied: it is simply ineffective, it is not an
            # error, and nothing about the rest of the mesh changes.
            effective = False
            if action == "reroute_traffic":
                # Traffic that was heading for the dead service gets pushed onto
                # the neighbour instead.
                collateral_service = NEIGHBOR[svc]
                faults.post(collateral_service, "/admin/fault", {"type": "COLLATERAL_LATENCY"})
                collateral_services.append(collateral_service)
        else:
            effective = bool(body.get("effective"))
            if action == "reroute_traffic" and not effective:
                # Misfired reroute pushes load onto the neighbour: this is why its blast radius is rated high.
                collateral_service = NEIGHBOR[svc]
                faults.post(collateral_service, "/admin/fault", {"type": "COLLATERAL_LATENCY"})
                collateral_services.append(collateral_service)

    # Measure the damage instead of trusting the intent.
    if baseline is not None:
        after = faults.get_collateral_metrics()
        collateral = faults.check_collateral_damage(baseline, after, svc)
    else:
        collateral = bool(collateral_services)

    return {"action": action, "service": svc, "effective": effective,
            "collateral": collateral, "collateral_service": collateral_service,
            "collateral_services": collateral_services,
            "exec_s": round(time.time() - t0, 3)}
