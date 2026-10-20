"""
MCP server (stdio). Exposes OBSERVATION tools plus propose_remediation.

There is intentionally NO execute tool here. The LLM can only propose; the
policy layer / human decides, and executor.py (not reachable via MCP) acts.

Test it interactively:
    npx @modelcontextprotocol/inspector python -m remediation.mcp_server
"""
import requests

try:  # mcp >= 2.0 renamed FastMCP -> MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .config import ACTIONS, FAULT_TYPES, SERVICES

mcp = _Server("network-remediation")


def _get(service: str, path: str, **params) -> dict:
    if service not in SERVICES:
        return {"service": service, "error": f"unknown service; choose from {list(SERVICES)}"}
    try:
        r = requests.get(SERVICES[service] + path, params=params, timeout=3)
        return r.json()
    except Exception as e:
        # A service that does not answer is reported as down - this is the CRASH signal.
        return {"service": service, "status": "down", "error": type(e).__name__}


@mcp.tool()
def list_services() -> dict:
    """List the monitored services."""
    return {"services": list(SERVICES)}


@mcp.tool()
def get_health(service: str) -> dict:
    """Health status of a service: status (ok/degraded/down) and probe latency."""
    return _get(service, "/health")


@mcp.tool()
def get_metrics(service: str) -> dict:
    """Metrics: p50/p99 latency, measured packet loss %, error rate, config checksum flag."""
    return _get(service, "/metrics")


@mcp.tool()
def get_logs(service: str, n_lines: int = 30) -> dict:
    """Most recent log lines from a service."""
    return _get(service, "/logs", n=n_lines)


@mcp.tool()
def get_config(service: str) -> dict:
    """Current running configuration of a service."""
    return _get(service, "/config")


@mcp.tool()
def propose_remediation(service: str, suspected_fault: str, action: str, rationale: str, confidence: float) -> dict:
    """
    Propose ONE remediation. This does NOT execute anything; a separate policy
    layer decides whether it runs automatically or needs human approval.
    suspected_fault: CRASH | HIGH_LATENCY | PACKET_LOSS | CONFIG_DRIFT
    action: restart_service | reapply_config | reroute_traffic | scale_service
    confidence: your honest probability (0-1) that the diagnosis and action are correct.
    """
    if service not in SERVICES:
        return {"status": "rejected", "error": f"unknown service {service}"}
    if suspected_fault not in FAULT_TYPES:
        return {"status": "rejected", "error": f"suspected_fault must be one of {FAULT_TYPES}"}
    if action not in ACTIONS:
        return {"status": "rejected", "error": f"action must be one of {ACTIONS}"}
    plan = {"service": service, "suspected_fault": suspected_fault, "action": action,
            "rationale": rationale, "confidence": min(max(float(confidence), 0.0), 1.0)}
    return {"status": "proposed", "plan": plan}


if __name__ == "__main__":
    mcp.run()  # stdio transport
