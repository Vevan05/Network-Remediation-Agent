"""
Simulated network service. One image, run three times (service-a/b/c) via
the SERVICE_NAME env var.

Observation endpoints (what the MCP server reads):
  GET  /ping      liveness only
  GET  /health    {"status": ok|degraded, "latency_ms": ...}
  GET  /metrics   latency, measured packet loss, config checksum flag
  GET  /logs?n=   recent synthetic log lines (reflect injected faults)
  GET  /config    current config

Admin endpoints (used ONLY by the fault injector and the executor,
never exposed to the LLM):
  POST /admin/fault   {"type": HIGH_LATENCY|PACKET_LOSS|CONFIG_DRIFT|COLLATERAL_LATENCY}
  POST /admin/action  {"action": reroute_traffic|reapply_config|scale_service}
  POST /admin/reset

Fault state is persisted to STATE_PATH so it survives `docker restart`.
That models faults living in the network path / on-disk config: a
restart does NOT cure them, only a crash.
"""
import json
import os
import random
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request

NAME = os.environ.get("SERVICE_NAME", "service")
STATE_PATH = Path(os.environ.get("STATE_PATH", "/tmp/netrem_state.json"))
GOOD_CONFIG = {"timeout_ms": 500, "max_connections": 100, "upstream": "primary"}
BASE_LATENCY_MS = 8

app = Flask(__name__)
START = time.time()
LOCK = threading.Lock()
LOGS = deque(maxlen=300)
STATE = {"extra_latency_ms": 0, "loss_pct": 0.0, "config": dict(GOOD_CONFIG)}
COUNTERS = {"requests": 0, "errors": 0}


def log(level: str, msg: str) -> None:
    LOGS.append(f"{time.strftime('%H:%M:%S')} {level} [{NAME}] {msg}")


def _save_locked() -> None:
    try:
        STATE_PATH.write_text(json.dumps(STATE))
    except OSError:
        pass


def _load() -> None:
    if STATE_PATH.exists():
        try:
            STATE.update(json.loads(STATE_PATH.read_text()))
        except (OSError, ValueError):
            pass


def _config_bad(cfg: dict) -> bool:
    return cfg.get("timeout_ms", 0) < 50 or cfg.get("upstream") != "primary"


def _snapshot():
    with LOCK:
        lat = BASE_LATENCY_MS + STATE["extra_latency_ms"] + random.randint(0, 6)
        loss = STATE["loss_pct"]
        bad = _config_bad(STATE["config"])
    return lat, loss, bad


@app.get("/ping")
def ping():
    return "pong"


@app.get("/health")
def health():
    lat, loss, bad = _snapshot()
    time.sleep(min(lat, 1000) / 1000)  # the caller really experiences the latency
    status = "degraded" if (lat > 200 or loss > 5 or bad) else "ok"
    return jsonify(service=NAME, status=status, latency_ms=lat,
                   uptime_s=round(time.time() - START, 1))


@app.get("/metrics")
def metrics():
    lat, loss, bad = _snapshot()
    probes = 50
    lost = sum(random.random() * 100 < loss for _ in range(probes))
    with LOCK:
        COUNTERS["requests"] += probes
        COUNTERS["errors"] += lost + (probes // 5 if bad else 0)
        req, err = COUNTERS["requests"], COUNTERS["errors"]
    return jsonify(service=NAME, latency_ms_p50=lat, latency_ms_p99=int(lat * 1.6),
                   packet_loss_pct=round(lost / probes * 100, 1),
                   error_rate=round(err / max(req, 1), 3),
                   config_checksum_ok=not bad)


@app.get("/logs")
def logs():
    n = int(request.args.get("n", 30))
    return jsonify(service=NAME, lines=list(LOGS)[-n:])


@app.get("/config")
def config():
    with LOCK:
        return jsonify(service=NAME, config=dict(STATE["config"]))


@app.post("/admin/fault")
def admin_fault():
    body = request.get_json(force=True)
    kind = body.get("type")
    with LOCK:
        if kind == "HIGH_LATENCY":
            STATE["extra_latency_ms"] = int(body.get("latency_ms", 600))
        elif kind == "PACKET_LOSS":
            STATE["loss_pct"] = float(body.get("loss_pct", 30))
        elif kind == "CONFIG_DRIFT":
            STATE["config"].update({"timeout_ms": 1, "upstream": "backup-stale"})
        elif kind == "COLLATERAL_LATENCY":
            STATE["extra_latency_ms"] = max(STATE["extra_latency_ms"], 250)
        else:
            return jsonify(error=f"unknown fault {kind}"), 400
        _save_locked()
    log("INFO", f"fault applied: {kind}")  # kept terse; chatter thread emits symptoms
    return jsonify(ok=True)


@app.post("/admin/action")
def admin_action():
    action = request.get_json(force=True).get("action")
    effective = False
    with LOCK:
        if action == "reroute_traffic":
            effective = STATE["extra_latency_ms"] > 0 or STATE["loss_pct"] > 0
            STATE["extra_latency_ms"], STATE["loss_pct"] = 0, 0.0
        elif action == "reapply_config":
            effective = STATE["config"] != GOOD_CONFIG
            STATE["config"] = dict(GOOD_CONFIG)
        elif action == "scale_service":  # partial relief only
            effective = STATE["extra_latency_ms"] > 0 or STATE["loss_pct"] > 0
            STATE["extra_latency_ms"] //= 2
            STATE["loss_pct"] /= 2
        else:
            return jsonify(error=f"unsupported action {action}"), 400
        _save_locked()
    log("INFO", f"action {action} applied (effective={effective})")
    return jsonify(ok=True, effective=effective)


@app.post("/admin/reset")
def admin_reset():
    with LOCK:
        STATE.update({"extra_latency_ms": 0, "loss_pct": 0.0, "config": dict(GOOD_CONFIG)})
        COUNTERS.update({"requests": 0, "errors": 0})
        _save_locked()
    LOGS.clear()
    log("INFO", "state reset")
    return jsonify(ok=True)


def _chatter() -> None:
    """Emit log lines that reflect the current (hidden) fault state."""
    while True:
        time.sleep(2)
        with LOCK:
            extra, loss, cfg = STATE["extra_latency_ms"], STATE["loss_pct"], dict(STATE["config"])
        if _config_bad(cfg):
            log("ERROR", f"config validation failed: timeout_ms={cfg['timeout_ms']} upstream={cfg['upstream']}")
        if extra > 0:
            log("WARN", f"slow responses: p50~{BASE_LATENCY_MS + extra}ms exceeds 100ms SLO")
        if loss > 0:
            log("WARN", f"TCP retransmissions elevated, estimated loss {loss:.0f}%")
        if not (_config_bad(cfg) or extra or loss):
            log("INFO", "heartbeat ok")


_load()

if __name__ == "__main__":
    log("INFO", "service started")
    threading.Thread(target=_chatter, daemon=True).start()
    app.run(host="0.0.0.0", port=8000, threaded=True)
