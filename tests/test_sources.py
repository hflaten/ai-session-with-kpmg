"""GET /runs/{id}/sources (fake run + fake lookups, no network)."""
from fastapi.testclient import TestClient

from app import sources
from app.main import app

client = TestClient(app, headers={"X-API-Key": "test-key"})
RUN = "33333333-3333-3333-3333-333333333333"
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
