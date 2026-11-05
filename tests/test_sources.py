"""GET /runs/{id}/sources (fake run + fake lookups, no network)."""
from fastapi.testclient import TestClient

import pytest

from app import sources
from app.main import app

client = TestClient(app, headers={"X-API-Key": "test-key"})
RUN = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def no_reviews_db(monkeypatch):
    """By default: no reviews stored (and no database call)."""
    monkeypatch.setattr(sources.reviews, "latest_reviews", lambda run_id: {})
ANSWER = {"summary": "x", "sources": [
    {"claim": "Target 45%", "document_name": "r.pdf", "page_number": 1, "chunk_id": "r.pdf:p1:c0"},
    {"claim": "Oslo 46%", "table": "sample_sales", "sql": "select 1"},
]}


def test_sources_resolve_to_text_and_rows(monkeypatch):
    monkeypatch.setattr(sources.runs, "get_run", lambda run_id: {"id": run_id, "answer": ANSWER})
    monkeypatch.setattr(sources, "_chunk_text", lambda cid: "The target is 45 percent.")
    ran = []
    monkeypatch.setattr(sources, "query_database",
                        lambda sql: ran.append(sql) or {"columns": ["region"], "rows": [{"region": "Oslo"}],
                                                        "row_count": 1, "truncated": False})
    body = client.get(f"/runs/{RUN}/sources").json()
    doc, data = body["sources"]
    assert doc["kind"] == "document" and doc["text"] == "The target is 45 percent." and doc["page_number"] == 1
    assert data["kind"] == "database" and data["rows"] == [{"region": "Oslo"}] and data["claim"] == "Oslo 46%"
    assert ran == ["select 1"]   # only the SQL stored in the validated answer


def test_missing_chunk_and_failing_sql_are_reported_not_raised(monkeypatch):
    monkeypatch.setattr(sources.runs, "get_run", lambda run_id: {"id": run_id, "answer": ANSWER})
    monkeypatch.setattr(sources, "_chunk_text", lambda cid: None)

    def boom(sql):
        raise RuntimeError("table gone")
    monkeypatch.setattr(sources, "query_database", boom)
    doc, data = client.get(f"/runs/{RUN}/sources").json()["sources"]
    assert "no longer exists" in doc["error"] and "table gone" in data["error"]


def test_text_answers_have_no_sources_and_unknown_runs_404(monkeypatch):
    monkeypatch.setattr(sources.runs, "get_run", lambda run_id: {"id": run_id, "answer": "plain text"})
    assert client.get(f"/runs/{RUN}/sources").json()["sources"] == []
    monkeypatch.setattr(sources.runs, "get_run", lambda run_id: None)
    assert client.get(f"/runs/{RUN}/sources").status_code == 404



# ---------- markers + reviews ----------

def test_sources_carry_marker_review_and_summary(monkeypatch):
    monkeypatch.setattr(sources.runs, "get_run", lambda run_id: {"id": run_id, "answer": ANSWER})
    monkeypatch.setattr(sources, "_chunk_text", lambda cid: "text")
    monkeypatch.setattr(sources, "query_database", lambda sql: {"columns": [], "rows": [], "row_count": 0, "truncated": False})
    monkeypatch.setattr(sources.reviews, "latest_reviews",
                        lambda run_id: {1: {"decision": "rejected", "reviewer": "Ana", "comment": "wrong quarter"}})
    body = client.get(f"/runs/{RUN}/sources").json()
    assert [(s["index"], s["marker"]) for s in body["sources"]] == [(0, 1), (1, 2)]
    assert body["sources"][0]["review"] is None
    assert body["sources"][1]["review"]["decision"] == "rejected"
    assert body["summary"] == {"total": 2, "accepted": 0, "rejected": 1, "unreviewed": 1}


@pytest.fixture
def review_api(monkeypatch):
    saved = []
    monkeypatch.setattr(sources, "source_count", lambda run_id: 2 if run_id == RUN else None)
    from app.routes import agent as agent_routes
    monkeypatch.setattr(agent_routes.reviews, "add_review",
                        lambda run_id, i, decision, reviewer, comment: saved.append((i, decision, reviewer, comment))
                        or {"source_index": i, "decision": decision, "reviewer": reviewer, "comment": comment})
    return saved


def test_review_a_source(review_api):
    r = client.post(f"/runs/{RUN}/sources/1/review", json={"decision": "accepted", "reviewer": "Ana", "comment": "checked p.2"})
    assert r.status_code == 200
    assert r.json()["marker"] == 2 and r.json()["review"]["decision"] == "accepted"
    assert review_api == [(1, "accepted", "Ana", "checked p.2")]
    assert client.post(f"/runs/{RUN}/sources/0/review", json={"decision": "rejected"}).status_code == 200  # optional fields


@pytest.mark.parametrize("path, body, status", [
    (f"/runs/{RUN}/sources/2/review", {"decision": "accepted"}, 404),        # only indexes 0 and 1
    (f"/runs/{RUN}/sources/-1/review", {"decision": "accepted"}, 404),
    ("/runs/44444444-4444-4444-4444-444444444444/sources/0/review", {"decision": "accepted"}, 404),  # no such run
    (f"/runs/{RUN}/sources/0/review", {"decision": "maybe"}, 422),           # only accepted / rejected
    (f"/runs/{RUN}/sources/0/review", {}, 422),
])
def test_bad_reviews_are_refused(review_api, path, body, status):
    assert client.post(path, json=body).status_code == status
    assert review_api == []
