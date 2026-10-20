"""
FastAPI bridge between the orchestrator and the React approval UI.

  GET  /api/plans/pending            plans waiting for a human
  GET  /api/plans/history            decided plans
  POST /api/plans/{id}/decision      {"decision": "approve"|"reject", "edited_action": optional}
  GET  /api/services/health          live health of the testbed
  POST /api/incidents                start an interactive demo incident
  GET  /api/events                   Server-Sent Events, AG-UI-style typed events

NOTE: /api/events implements a *minimal AG-UI-style* event stream (RUN_STARTED, STATE_SNAPSHOT,
CUSTOM, RUN_FINISHED). Before you claim AG-UI conformance in the paper, check the current AG-UI
spec/SDK and swap in the official event encoder (see IMPLEMENTATION_GUIDE.md, Phase 6).

Run:  uvicorn remediation.api:app --port 8000 --reload
"""
import asyncio
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import faults, store
from .agent import make_agent
from .config import ACTIONS, FAULT_TYPES, SERVICES
from .orchestrator import run_incident
from .policy import MODES

app = FastAPI(title="Remediation approval API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
store.init()


class Decision(BaseModel):
    decision: str
    edited_action: str | None = None


class Incident(BaseModel):
    fault: str = "HIGH_LATENCY"
    target: str = "service-b"
    mode: str = "adaptive"


@app.get("/api/plans/pending")
def pending():
    return store.pending()


@app.get("/api/plans/history")
def history():
    return store.history()


@app.post("/api/plans/{plan_id}/decision")
def decide(plan_id: str, d: Decision):
    if d.decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    if d.edited_action and d.edited_action not in ACTIONS:
        raise HTTPException(400, f"edited_action must be one of {ACTIONS}")
    if not store.decide(plan_id, d.decision, d.edited_action):
        raise HTTPException(409, "plan not pending")
    return {"ok": True}


@app.get("/api/services/health")
async def services_health():
    return {s: await asyncio.to_thread(faults.health, s) for s in SERVICES}


@app.post("/api/incidents")
async def start_incident(inc: Incident):
    if inc.fault not in FAULT_TYPES or inc.target not in SERVICES or inc.mode not in MODES:
        raise HTTPException(400, "bad fault/target/mode")

    async def _run():
        async with make_agent(os.getenv("AGENT_KIND", "heuristic")) as agent:
            await run_incident(agent, inc.mode, inc.fault, inc.target, interactive=True)

    asyncio.create_task(_run())
    return {"started": True}


@app.get("/api/events")
async def events(last_id: int = 0):
    async def gen():
        cur = last_id
        while True:
            for e in store.events_since(cur):
                cur = e["id"]
                yield f"id: {cur}\ndata: {json.dumps(e)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream")
