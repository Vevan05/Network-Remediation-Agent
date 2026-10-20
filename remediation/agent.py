"""
Agents. Both talk to the SAME MCP server over stdio, so MCP call counts and
latency (the "communication overhead" metric) are measured identically.

  LLMAgent        Claude (Anthropic) or Llama (Groq/OpenAI), tools = the MCP server's tools
  HeuristicAgent  rule-based stand-in with a tunable error rate. Use it to build
                  and debug the whole pipeline without spending API credits, and
                  to run controlled "what if the agent is X% wrong" experiments.
"""
import asyncio
import json
import os
import random
import sys
import time
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import ACTIONS, FIX_MATRIX, MODEL, ROOT
from .risk import SIGNATURES

EVIDENCE_KEYS = ("status", "latency_ms", "latency_ms_p50", "packet_loss_pct", "config_checksum_ok", "error_rate")

SYSTEM_PROMPT = """You are an autonomous network operations agent for a small service mesh (service-a, service-b, service-c).

Workflow:
1. Call list_services.
2. Call get_health and get_metrics for every service.
3. For any service that looks unhealthy, call get_logs (and get_config if useful).
4. Diagnose the root cause of the most important fault.
5. Call propose_remediation EXACTLY ONCE.

Remediation Rubric (Follow strictly):
- CRASH -> restart_service
- HIGH_LATENCY -> reroute_traffic
- PACKET_LOSS -> reroute_traffic
- CONFIG_DRIFT -> reapply_config

You can only PROPOSE. A separate policy layer decides whether the action runs automatically or needs human
approval, so report an honest confidence between 0 and 1; do not pad it.

If every service is healthy, reply 'all healthy' and do not propose anything."""


class _Ctx:
    def __init__(self):
        self.calls = 0
        self.trace: list[dict] = []
        self.evidence: dict[str, dict] = {}


class BaseAgent:
    async def __aenter__(self):
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=sys.executable, args=["-m", "remediation.mcp_server"],
                                       cwd=str(ROOT), env=dict(os.environ))
        r, w = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(r, w))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def _call(self, ctx: _Ctx, name: str, args: dict):
        t0 = time.time()
        out = await self.session.call_tool(name, args)
        ctx.calls += 1
        text = "".join(getattr(c, "text", "") for c in out.content)
        try:
            data = json.loads(text)
        except ValueError:
            data = {"raw": text}
        ctx.trace.append({"tool": name, "args": args, "s": round(time.time() - t0, 3)})
        if name in ("get_health", "get_metrics") and isinstance(data, dict) and "service" in data:
            ev = ctx.evidence.setdefault(data["service"], {})
            ev.update({k: v for k, v in data.items() if k in EVIDENCE_KEYS})
        return data, text

    async def diagnose(self) -> dict:
        ctx, t0 = _Ctx(), time.time()
        plan = await self.decide(ctx)
        if plan:
            plan["evidence"] = ctx.evidence.get(plan["service"], {})
        return {"plan": plan, "mcp_calls": ctx.calls, "latency_s": round(time.time() - t0, 3), "trace": ctx.trace}

    async def decide(self, ctx: _Ctx) -> dict | None:  # pragma: no cover
        raise NotImplementedError


class HeuristicAgent(BaseAgent):
    def __init__(self, error_rate: float = 0.0, seed: int = 0):
        self.error_rate, self.rng = error_rate, random.Random(seed)

    async def decide(self, ctx: _Ctx):
        services = (await self._call(ctx, "list_services", {}))[0]["services"]
        for s in services:
            await self._call(ctx, "get_health", {"service": s})
            await self._call(ctx, "get_metrics", {"service": s})
        sick = [s for s in services if ctx.evidence.get(s, {}).get("status") != "ok"]
        if not sick:
            return None
        target = sorted(sick, key=lambda s: ctx.evidence[s].get("status") != "down")[0]
        await self._call(ctx, "get_logs", {"service": target})
        ev = ctx.evidence[target]
        fault = next((f for f in ("CRASH", "CONFIG_DRIFT", "PACKET_LOSS", "HIGH_LATENCY") if SIGNATURES[f](ev)),
                     "HIGH_LATENCY")
        action = sorted(FIX_MATRIX[fault])[0]
        confidence = self.rng.uniform(0.75, 0.95)
        if self.rng.random() < self.error_rate:  # simulate a misdiagnosis; note it stays over-confident
            action = self.rng.choice([a for a in ACTIONS if a not in FIX_MATRIX[fault]])
            confidence = self.rng.uniform(0.6, 0.95)
        data, _ = await self._call(ctx, "propose_remediation", {
            "service": target, "suspected_fault": fault, "action": action,
            "rationale": f"heuristic: evidence {ev}", "confidence": confidence})
        return data.get("plan")


class LLMAgent(BaseAgent):
    def __init__(self, max_turns: int = 12):
        self.max_turns = max_turns
        self.kind = os.getenv("AGENT_KIND", "gemini")
        if self.kind == "anthropic":
            import anthropic
            self.client = anthropic.AsyncAnthropic()
        elif self.kind == "gemini":
            from google import genai
            api_key = os.getenv("GEMINI_API_KEY")
            self.client = genai.Client(api_key=api_key)
        elif self.kind == "groq":
            from openai import AsyncOpenAI
            # Use an empty string or dummy if not provided, to avoid OpenAI init crash
            api_key = os.getenv("GROQ_API_KEY", "dummy")
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1"
            )
        else:
            raise ValueError(f"Unsupported AGENT_KIND: {self.kind}")

    async def decide(self, ctx: _Ctx):
        tools_result = await self.session.list_tools()
        mcp_tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema if hasattr(t, 'inputSchema') else getattr(t, 'input_schema', {})}
                     for t in tools_result.tools]

        # Gemini expects a list of Tool objects, each containing a list of function_declarations
        gemini_tools = []
        if self.kind == "gemini":
            from google.genai import types
            function_decls = []
            for t in mcp_tools:
                def clean_schema(schema):
                    if not isinstance(schema, dict):
                        return schema
                    return {k: clean_schema(v) if isinstance(v, dict) else
                            ([clean_schema(i) for i in v] if isinstance(v, list) else v)
                            for k, v in schema.items() if k not in ("title", "default")}

                params = clean_schema(t["input_schema"])
                function_decls.append(types.FunctionDeclaration(
                    name=t["name"],
                    description=t["description"],
                    parameters=params
                ))
            gemini_tools = [types.Tool(function_declarations=function_decls)]

        # Normalize tools for Groq/OpenAI format
        openai_tools = []
        if self.kind == "groq":
            for t in mcp_tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"]
                    }
                })

        messages = [{"role": "user", "parts": [{"text": "Check all services and handle any incident."}]}]

        for _ in range(self.max_turns):
            if self.kind == "gemini":
                # Rate limit protection for Free Tier (15 RPM)
                await asyncio.sleep(4.5)
                # Gemini uses a chat session with tool calling
                # For consistency with the existing Loop, we use generate_content
                # and pass the full history.
                resp = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=MODEL,
                    contents=messages,
                    config={"tools": gemini_tools}
                )

                # Gemini parses tool calls into 'parts'
                content = resp.candidates[0].content
                messages.append({"role": "model", "parts": content.parts})

                uses = []
                for part in content.parts:
                    if part.function_call:
                        uses.append(type("ToolUse", (), {
                            "id": None, # Gemini doesn't use call IDs in the same way
                            "name": part.function_call.name,
                            "input": dict(part.function_call.args)
                        })())
            elif self.kind == "anthropic":
                # Convert Gemini parts format back to Anthropic/OpenAI content strings
                anthro_messages = []
                for m in messages:
                    if m["role"] == "model":
                        text = "".join(p.text for p in m["parts"] if p.text)
                        anthro_messages.append({"role": "assistant", "content": text})
                    elif m["role"] == "user":
                        # Handle both parts-list and content-string
                        content = m["parts"][0] if "parts" in m else m.get("content", "")
                        anthro_messages.append({"role": "user", "content": content})
                    elif m["role"] == "function":
                        # Function responses are handled by the loop
                        pass
                    else:
                        anthro_messages.append(m)

                resp = await self.client.messages.create(
                    model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
                    tools=tools, messages=anthro_messages
                )
                # Anthropic response parsing
                content = resp.content
                messages.append({"role": "assistant", "content": content})
                uses = [b for b in content if b.type == "tool_use"]
            else:
                # Groq/OpenAI response parsing
                resp = await self.client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                    tools=openai_tools,
                    tool_choice="auto"
                )
                content = resp.choices[0].message
                # OpenAI/Groq messages store the content in .content (string)
                # We must store it as a string for the next request
                messages.append({"role": "assistant", "content": content.content or ""})
                uses = []
                if content.tool_calls:
                    for tc in content.tool_calls:
                        # Wrap in a pseudo-Anthropic tool_use object to reuse the execution loop
                        uses.append(type("ToolUse", (), {
                            "id": tc.id,
                            "name": tc.function.name,
                            "input": json.loads(tc.function.arguments)
                        })())

            if not uses:
                return None

            results, plan = [], None
            for u in uses:
                data, text = await self._call(ctx, u.name, dict(u.input))
                if self.kind == "gemini":
                    # New google-genai SDK uses 'user' role for function responses
                    # provided they contain a function_response part.
                    messages.append({
                        "role": "user",
                        "parts": [{"function_response": {"name": u.name, "response": {"result": text}}}]
                    })
                else:
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": text})
                if u.name == "propose_remediation" and data.get("status") == "proposed":
                    plan = data["plan"]

            if plan:
                return plan

            if self.kind == "anthropic":
                messages.append({"role": "user", "content": results})
            else:
                # Convert tool results back to OpenAI format
                openai_results = []
                for r in results:
                    openai_results.append({
                        "role": "tool",
                        "tool_call_id": r["tool_use_id"],
                        "content": r["content"]
                    })
                # Only append if we aren't using Gemini (who already got the messages)
                if self.kind != "gemini":
                    messages.append({"role": "tool", "content": results}) # fallback
                # Note: OpenAI expects each tool result as a separate message
                # The actual fix for OpenAI tool result messages is in the loop below

            if self.kind == "groq":
                # Correcting OpenAI tool result message format
                # We need to remove the last 'user' result message and add individual tool messages
                messages.pop()
                for r in results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": r["tool_use_id"],
                        "content": r["content"]
                    })
        return None


def make_agent(kind: str, **kw) -> BaseAgent:
    if kind == "llm":
        return LLMAgent()
    if kind == "heuristic":
        return HeuristicAgent(error_rate=kw.get("error_rate", 0.0), seed=kw.get("seed", 0))
    raise ValueError(f"unknown agent kind {kind}")
