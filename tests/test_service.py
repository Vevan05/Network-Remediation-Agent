import importlib.util
import os
import pathlib
import tempfile


def load_service():
    os.environ["STATE_PATH"] = str(pathlib.Path(tempfile.mkdtemp()) / "state.json")
    os.environ["SERVICE_NAME"] = "service-test"
    path = pathlib.Path(__file__).parent.parent / "services" / "svc" / "app.py"
    spec = importlib.util.spec_from_file_location("svc_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_latency_fault_is_observable_and_curable():
    c = load_service().app.test_client()
    assert c.get("/health").json["status"] == "ok"
    c.post("/admin/fault", json={"type": "HIGH_LATENCY", "latency_ms": 300})
    assert c.get("/health").json["status"] == "degraded"
    assert c.post("/admin/action", json={"action": "reroute_traffic"}).json["effective"] is True
    assert c.get("/health").json["status"] == "ok"


def test_config_drift_cured_only_by_reapply():
    c = load_service().app.test_client()
    c.post("/admin/fault", json={"type": "CONFIG_DRIFT"})
    assert c.get("/metrics").json["config_checksum_ok"] is False
    assert c.post("/admin/action", json={"action": "reroute_traffic"}).json["effective"] is False
    assert c.get("/metrics").json["config_checksum_ok"] is False
    assert c.post("/admin/action", json={"action": "reapply_config"}).json["effective"] is True
    assert c.get("/metrics").json["config_checksum_ok"] is True


def test_misfired_reroute_reports_ineffective():
    c = load_service().app.test_client()  # healthy service: nothing to reroute around
    assert c.post("/admin/action", json={"action": "reroute_traffic"}).json["effective"] is False


def test_fault_state_persists_across_reload():
    mod = load_service()
    mod.app.test_client().post("/admin/fault", json={"type": "PACKET_LOSS", "loss_pct": 40})
    path = os.environ["STATE_PATH"]
    mod2 = importlib.util.module_from_spec(importlib.util.spec_from_file_location("svc_app2", mod.__file__))
    os.environ["STATE_PATH"] = path
    mod2.__spec__.loader.exec_module(mod2)  # simulates `docker restart`
    assert mod2.STATE["loss_pct"] == 40.0
