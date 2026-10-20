"""
Host-side fault injection + ground-truth helpers.

Runs on your machine (not in a container) and talks to the testbed via the
docker CLI (for CRASH) and each service's /admin API (for everything else).
"""
import subprocess
import time

import requests

from .config import FIX_MATRIX, SERVICES

# A latency ratio alone is noise at an 8ms baseline; real collateral adds ~250ms.
COLLATERAL_LATENCY_FLOOR_MS = 100


def docker(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:  # pragma: no cover
        raise RuntimeError("docker CLI not found - install Docker and run `docker compose up -d --build`") from e


def post(service: str, path: str, payload: dict | None = None) -> requests.Response:
    return requests.post(SERVICES[service] + path, json=payload or {}, timeout=5)


def try_post(service: str, path: str, payload: dict | None = None) -> dict | None:
    """
    POST that tolerates an unreachable service. Returns the JSON body, or None if
    the service could not be reached (e.g. it is down because of a CRASH).
    """
    try:
        r = post(service, path, payload)
        return r.json()
    except Exception:
        return None


def health(service: str) -> dict:
    try:
        return requests.get(SERVICES[service] + "/health", timeout=3).json()
    except Exception as e:  # connection refused / timeout == service is down
        return {"service": service, "status": "down", "error": type(e).__name__}


def wait_up(service: str, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(SERVICES[service] + "/ping", timeout=1).ok:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def inject(fault: str, service: str) -> None:
    if fault == "CRASH":
        docker("stop", "-t", "0", service)
    elif fault in ("HIGH_LATENCY", "PACKET_LOSS", "CONFIG_DRIFT"):
        post(service, "/admin/fault", {"type": fault})
    else:
        raise ValueError(f"unknown fault {fault}")


def reset_all() -> None:
    """Return the whole testbed to a clean state (call before EVERY run)."""
    for s in SERVICES:
        docker("start", s)  # no-op if already running
    for s in SERVICES:
        if not wait_up(s):
            raise RuntimeError(f"{s} did not come back up")
        post(s, "/admin/reset")


def is_recovered(service: str) -> bool:
    return health(service).get("status") == "ok"

def get_collateral_metrics() -> dict:
    """
    Returns the current health of all services.
    """
    return {s: health(s) for s in SERVICES}

def check_collateral_damage(post_injection_baseline: dict, post_action_state: dict, target: str) -> bool:
    """
    Operational definition of collateral damage:
    A plan is unsafe if it causes degradation RELATIVE to the post-injection state.
    This prevents blaming the action for faults caused by the injector.

    Two signals, and the latency one needs an ABSOLUTE floor: the services jitter
    by a few ms between samples, so a bare 1.5x ratio on an 8ms baseline flags
    pure noise. A real collateral hit adds ~250ms, so the floor is 100ms.
    """
    for s, status in post_action_state.items():
        if s == target:
            continue

        # 1. Status degradation
        base_status = post_injection_baseline.get(s, {}).get("status", "ok")
        if base_status == "ok" and status.get("status") != "ok":
            return True

        # 2. Latency spike relative to post-injection state
        base_lat = post_injection_baseline.get(s, {}).get("latency_ms", 0)
        curr_lat = status.get("latency_ms", 0)
        if base_lat > 0 and curr_lat > COLLATERAL_LATENCY_FLOOR_MS and (curr_lat / base_lat) > 1.5:
            return True

    return False

def is_unsafe(fault: str, exec_result: dict) -> bool:
    # Now purely driven by operational outcomes (collateral)
    return exec_result.get("collateral", False)

def is_incorrect(fault: str, exec_result: dict) -> bool:
    # "Did the action achieve its intended effect?" - the executor reports this as
    # `effective`. (It used to read a "recovered" key that the executor never
    # emitted, so this flag was True for 100% of runs, successful or not.)
    return not exec_result.get("effective", False)
