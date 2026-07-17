"""main.py app tests (QA1-21): the investor's two emergency controls —
kill switch and resume — plus /api/health, exercised through the real FastAPI
app with its lifespan (not a test-only router shell). Runs in testnet mode
with no credentials: no gateway, no network, everything else wired for real.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

import trader.main as main_mod

UI = {"x-marnar-ui": "1"}  # the QA1-01 CSRF header


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"mode": "testnet", "data_dir": str(tmp_path)}))
    monkeypatch.setattr(main_mod, "CONFIG_PATH", str(cfg))
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)
    with TestClient(main_mod.app) as c:
        yield c


def test_health_reports_unconfigured_and_locked(client):
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert data["exchange"] == "unconfigured"
    assert data["trading_enabled"] is False
    assert data["risk"]["state"] == "RUNNING"


def test_kill_switch_requires_csrf_header_then_halts(client):
    denied = client.post("/api/kill-switch", json={"reason": "test"})
    assert denied.status_code == 403
    assert client.get("/api/health").json()["risk"]["state"] == "RUNNING"

    ok = client.post("/api/kill-switch", json={"reason": "test"}, headers=UI)
    assert ok.status_code == 200
    assert ok.json()["state"] == "HALTED_MANUAL"
    assert client.get("/api/health").json()["risk"]["state"] == "HALTED_MANUAL"


def test_resume_requires_csrf_header_and_clean_reconciliation(client):
    client.post("/api/kill-switch", json={"reason": "test"}, headers=UI)

    assert client.post("/api/resume").status_code == 403  # no header

    # Dirty reconciliation blocks resume (main.py refuses, D-07 discipline).
    main_mod.app.state.reconciliation = {"clean": False, "errors": ["boom"]}
    assert client.post("/api/resume", headers=UI).status_code == 409
    assert client.get("/api/health").json()["risk"]["state"] == "HALTED_MANUAL"

    main_mod.app.state.reconciliation = None
    res = client.post("/api/resume", headers=UI)
    assert res.status_code == 200
    assert res.json()["state"] == "RUNNING"
    # No gateway in this mode, so trading stays off even after resume.
    assert res.json()["trading_enabled"] is False
