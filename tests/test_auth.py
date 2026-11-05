from dataclasses import replace

from fastapi.testclient import TestClient

from app import auth, db
from app.main import app
from app.routes import agent as agent_routes

client = TestClient(app)


def test_health_is_open(monkeypatch):
    monkeypatch.setattr(db, "ping", lambda: True)
    assert client.get("/health").status_code == 200


def test_missing_or_wrong_key_is_401():
    assert client.post("/agent/run", json={"task": "x"}).status_code == 401
    assert client.post("/agent/run", json={"task": "x"}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/runs/00000000-0000-0000-0000-000000000000").status_code == 401
    assert client.get("/agent/stream", params={"task": "x"}).status_code == 401


def test_correct_key_passes(monkeypatch):
    monkeypatch.setattr(agent_routes.runs, "get_run", lambda run_id: None)
    r = client.get("/runs/00000000-0000-0000-0000-000000000000", headers={"X-API-Key": "test-key"})
    assert r.status_code == 404  # got past auth


def test_query_param_fallback_for_eventsource(monkeypatch):
    monkeypatch.setattr(agent_routes.runs, "get_run", lambda run_id: None)
    r = client.get("/runs/00000000-0000-0000-0000-000000000000", params={"api_key": "test-key"})
    assert r.status_code == 404


def test_fails_closed_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(auth, "settings", replace(auth.settings, app_api_key=""))
    r = client.post("/agent/run", json={"task": "x"}, headers={"X-API-Key": ""})
    assert r.status_code == 503


def test_cors_preflight_allows_the_header():
    r = client.options("/agent/run", headers={
        "Origin": "https://demo.lovable.app", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-api-key, content-type"})
    assert r.status_code == 200
    assert "x-api-key" in r.headers["access-control-allow-headers"].lower()
