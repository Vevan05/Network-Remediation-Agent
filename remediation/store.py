"""SQLite store shared by the orchestrator and the API: pending plans, decisions, UI event stream."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import RESULTS_DIR

DB_PATH = RESULTS_DIR / "remediation.db"


@contextmanager
def db():
    RESULTS_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS plans(
              id TEXT PRIMARY KEY, created REAL, status TEXT, payload TEXT,
              decision TEXT, edited_action TEXT, decided REAL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, type TEXT, payload TEXT);
        """)


def _plan_row(r: sqlite3.Row) -> dict:
    d = json.loads(r["payload"])
    return {"id": r["id"], "status": r["status"], "created": r["created"], "decision": r["decision"],
            "edited_action": r["edited_action"], "decided": r["decided"], **d}


def create_plan(plan: dict, risk: dict, meta: dict | None = None) -> str:
    init()
    pid = uuid.uuid4().hex[:8]
    payload = {"plan": plan, "risk": risk, **(meta or {})}
    with db() as c:
        c.execute("INSERT INTO plans(id,created,status,payload) VALUES(?,?,?,?)",
                  (pid, time.time(), "pending", json.dumps(payload)))
    return pid


def pending() -> list[dict]:
    init()
    with db() as c:
        return [_plan_row(r) for r in c.execute("SELECT * FROM plans WHERE status='pending' ORDER BY created")]


def history(limit: int = 50) -> list[dict]:
    init()
    with db() as c:
        return [_plan_row(r) for r in c.execute(
            "SELECT * FROM plans WHERE status!='pending' ORDER BY decided DESC LIMIT ?", (limit,))]


def decide(plan_id: str, decision: str, edited_action: str | None = None) -> bool:
    assert decision in ("approve", "reject")
    init()
    with db() as c:
        cur = c.execute(
            "UPDATE plans SET status=?, decision=?, edited_action=?, decided=? WHERE id=? AND status='pending'",
            ("decided", decision, edited_action, time.time(), plan_id))
        return cur.rowcount == 1


def wait_for_decision(plan_id: str, timeout_s: float = 900, poll_s: float = 0.5) -> dict | None:
    end = time.time() + timeout_s
    while time.time() < end:
        with db() as c:
            r = c.execute("SELECT * FROM plans WHERE id=? AND status='decided'", (plan_id,)).fetchone()
            if r:
                return _plan_row(r)
        time.sleep(poll_s)
    return None


def add_event(etype: str, payload: dict) -> int:
    """Events follow AG-UI-style names (RUN_STARTED, STATE_SNAPSHOT, CUSTOM, RUN_FINISHED...)."""
    init()
    with db() as c:
        return c.execute("INSERT INTO events(ts,type,payload) VALUES(?,?,?)",
                         (time.time(), etype, json.dumps(payload))).lastrowid


def events_since(last_id: int) -> list[dict]:
    init()
    with db() as c:
        return [{"id": r["id"], "ts": r["ts"], "type": r["type"], "payload": json.loads(r["payload"])}
                for r in c.execute("SELECT * FROM events WHERE id>? ORDER BY id", (last_id,))]
