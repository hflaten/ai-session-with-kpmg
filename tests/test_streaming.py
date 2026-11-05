"""SSE streaming + run logging (fake agent and fake DB, no network)."""
import json

import pytest
from fastapi.testclient import TestClient

from app.agent import AgentResult
from app.main import app
from app.agent import service
from app.routes import agent as agent_routes

client = TestClient(app, headers={"X-API-Key": "test-key"})


@pytest.fixture
def fake_runs(monkeypatch):
    log = {"created": [], "finished": []}
    monkeypatch.setattr(service, "describe_schema", lambda: "Tables: (stubbed)")
    monkeypatch.setattr(service, "describe_documents", lambda: "Docs: (stubbed)")
    monkeypatch.setattr(service.runs, "create_run", lambda task, **kw: log["created"].append(task) or "run-1")
    monkeypatch.setattr(service.runs, "finish_run",
                        lambda run_id, status, trace, **kw: log["finished"].append((run_id, status, len(trace), kw)))
    return log


def fake_agent(fail=False):
    def run(system, tools, task, output_schema=None, on_event=None, **kwargs):
        on_event({"step": 1, "type": "llm", "text": "", "tool_calls": [{"name": "query_database"}]})
        on_event({"step": 1, "type": "tool", "tool": "query_database", "output": "[]"})
        if fail:
            raise RuntimeError("LLM down")
        on_event({"step": 2, "type": "llm", "text": "Done.", "tool_calls": []})
        return AgentResult(answer="Done.", trace=[{}, {}, {}])
    return run


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_stream_emits_events_in_order_and_logs_run(monkeypatch, fake_runs):
    monkeypatch.setattr(service, "run_agent", fake_agent())
    r = client.post("/agent/stream", json={"task": "q"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert [name for name, _ in events] == ["run_started", "step", "step", "step", "done"]
    assert events[0][1]["run_id"] == "run-1"
    assert events[-1][1]["answer"] == "Done."
    assert fake_runs["created"] == ["q"]
    assert fake_runs["finished"][0][:2] == ("run-1", "completed")


def test_get_stream_works_for_eventsource(monkeypatch, fake_runs):
    monkeypatch.setattr(service, "run_agent", fake_agent())
    r = client.get("/agent/stream", params={"task": "q"})
    assert parse_sse(r.text)[-1][0] == "done"


def test_failed_run_is_logged_and_streamed_as_error(monkeypatch, fake_runs):
    monkeypatch.setattr(service, "run_agent", fake_agent(fail=True))
    events = parse_sse(client.post("/agent/stream", json={"task": "q"}).text)
    assert events[-1][0] == "error" and "LLM down" in events[-1][1]["error"]
    run_id, status, trace_len, kw = fake_runs["finished"][0]
    assert status == "failed" and trace_len == 2 and "LLM down" in kw["error"]


def test_run_endpoint_returns_run_id(monkeypatch, fake_runs):
    monkeypatch.setattr(service, "run_agent", fake_agent())
    body = client.post("/agent/run", json={"task": "q"}).json()
    assert body["run_id"] == "run-1" and body["answer"] == "Done."


def test_unknown_run_is_404(monkeypatch):
    monkeypatch.setattr(agent_routes.runs, "get_run", lambda run_id: None)
    assert client.get("/runs/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/runs/not-a-uuid").status_code == 422
