from fastapi.testclient import TestClient

from app import db
from app.config import cors_origin_regex
from app.main import app

client = TestClient(app)


def test_health(monkeypatch):
    monkeypatch.setattr(db, "ping", lambda: True)  # no network needed
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # no secret-looking values in the response
    assert not any("key" in k.lower() for k in body)


def test_cors_allows_lovable_and_blocks_others(monkeypatch):
    monkeypatch.setattr(db, "ping", lambda: True)
    for origin, allowed in [
        ("https://my-app.lovable.app", True),
        ("https://abc-123.lovableproject.com", True),
        ("http://localhost:3000", True),
        ("https://evil.com", False),
        ("https://lovable.app.evil.com", False),
    ]:
        r = client.get("/health", headers={"Origin": origin})
        assert (r.headers.get("access-control-allow-origin") == origin) is allowed, origin


def test_wildcard_matches_one_subdomain_only():
    import re
    rx = re.compile(cors_origin_regex(["https://*.lovable.app"]))
    assert rx.match("https://x.lovable.app")
    assert not rx.match("https://a.b.lovable.app")
