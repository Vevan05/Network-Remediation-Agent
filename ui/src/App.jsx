import { useEffect, useState } from "react";

const ACTIONS = ["restart_service", "reapply_config", "reroute_traffic", "scale_service"];
const FAULTS = ["CRASH", "HIGH_LATENCY", "PACKET_LOSS", "CONFIG_DRIFT"];
const SERVICES = ["service-a", "service-b", "service-c"];
const MODES = ["adaptive", "full-human", "full-auto"];

const api = (path, opts) => fetch(`/api${path}`, opts).then((r) => r.json());
const post = (path, body) =>
  fetch(`/api${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function RiskBars({ breakdown, maxTerm = 0.35 }) {
  // Each term is already weighted, so the bars sum to the total risk score.
  return (
    <div className="bars">
      {Object.entries(breakdown).map(([k, v]) => (
        <div key={k} className="bar-row">
          <span className="bar-label">{k.replace(/_/g, " ")}</span>
          <div className="bar-track"><div className="bar-fill" style={{ width: `${Math.min(v / maxTerm, 1) * 100}%` }} /></div>
          <span className="bar-val">{v.toFixed(3)}</span>
        </div>
      ))}
    </div>
  );
}

function PlanCard({ item }) {
  const { plan, risk } = item;
  const [action, setAction] = useState(plan.action);
  const decide = (decision) =>
    post(`/plans/${item.id}/decision`, { decision, edited_action: decision === "approve" && action !== plan.action ? action : null });
  return (
    <div className="card">
      <div className="card-head">
        <strong>{plan.service}</strong> <span className="tag">{plan.suspected_fault}</span>
        <span className="risk">risk {risk.score.toFixed(2)}</span>
      </div>
      <p className="rationale">{plan.rationale}</p>
      <p>Self-reported confidence {plan.confidence.toFixed(2)} → calibrated {risk.confidence_used.toFixed(2)}</p>
      <p className="muted">
        Decision latency {item.decision_latency_s?.toFixed(1) ?? "?"}s · {item.mcp_calls ?? "?"} MCP tool calls
        {item.risk_version ? ` · risk formula ${item.risk_version}` : ""}
      </p>
      <RiskBars breakdown={risk.breakdown} maxTerm={risk.max_term ?? 0.35} />
      <details><summary>Evidence the agent saw</summary><pre>{JSON.stringify(plan.evidence, null, 2)}</pre></details>
      <div className="row">
        <label>Action&nbsp;
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            {ACTIONS.map((a) => <option key={a}>{a}</option>)}
          </select>
        </label>
        <button className="ok" onClick={() => decide("approve")}>{action === plan.action ? "Approve" : "Approve edited"}</button>
        <button className="no" onClick={() => decide("reject")}>Reject (fix manually)</button>
      </div>
    </div>
  );
}

export default function App() {
  const [pending, setPending] = useState([]);
  const [history, setHistory] = useState([]);
  const [health, setHealth] = useState({});
  const [events, setEvents] = useState([]);
  const [inc, setInc] = useState({ fault: "HIGH_LATENCY", target: "service-b", mode: "adaptive" });

  useEffect(() => {
    const tick = async () => {
      try {
        setPending(await api("/plans/pending"));
        setHistory(await api("/plans/history"));
        setHealth(await api("/services/health"));
      } catch { /* API not up yet */ }
    };
    tick();
    const t = setInterval(tick, 1500);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onmessage = (e) => setEvents((prev) => [JSON.parse(e.data), ...prev].slice(0, 40));
    return () => es.close();
  }, []);

  return (
    <div className="page">
      <h1>Remediation Console</h1>

      <section>
        <h2>Services</h2>
        <div className="row">
          {SERVICES.map((s) => (
            <div key={s} className={`svc ${health[s]?.status || "down"}`}>{s}<br /><small>{health[s]?.status || "…"}</small></div>
          ))}
        </div>
      </section>

      <section>
        <h2>Start an incident</h2>
        <div className="row">
          <select value={inc.fault} onChange={(e) => setInc({ ...inc, fault: e.target.value })}>{FAULTS.map((f) => <option key={f}>{f}</option>)}</select>
          <select value={inc.target} onChange={(e) => setInc({ ...inc, target: e.target.value })}>{SERVICES.map((s) => <option key={s}>{s}</option>)}</select>
          <select value={inc.mode} onChange={(e) => setInc({ ...inc, mode: e.target.value })}>{MODES.map((m) => <option key={m}>{m}</option>)}</select>
          <button onClick={() => post("/incidents", inc)}>Inject fault + run agent</button>
        </div>
      </section>

      <section>
        <h2>Awaiting approval ({pending.length})</h2>
        {pending.length === 0 && <p className="muted">Nothing pending. Low-risk plans in adaptive mode run without you.</p>}
        {pending.map((p) => <PlanCard key={p.id} item={p} />)}
      </section>

      <section>
        <h2>Decision history</h2>
        {history.map((h) => (
          <div key={h.id} className="hist">
            {h.plan.service} · {h.plan.action} → <b>{h.decision}</b>{h.edited_action ? ` (edited to ${h.edited_action})` : ""} · risk {h.risk.score.toFixed(2)}
            {h.decision_latency_s != null ? ` · ${h.decision_latency_s.toFixed(1)}s decision latency · ${h.mcp_calls} MCP calls` : ""}
          </div>
        ))}
      </section>

      <section>
        <h2>Event stream (AG-UI style)</h2>
        <pre className="events">{events.map((e) => `${e.type}  ${JSON.stringify(e.payload)}`).join("\n")}</pre>
      </section>
    </div>
  );
}
