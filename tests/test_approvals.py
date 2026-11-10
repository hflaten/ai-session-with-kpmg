"""Human-in-the-loop (fake LLM, fake DB logging, no network)."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.agent import approvals, loop, service
from app.llm import LLMResponse, Tool, ToolCall
from app.main import app

client = TestClient(app, headers={"X-API-Key": "test-key"})

ran = []
FLAGGED = Tool("save_report", "save", {"type": "object", "properties": {"title": {"type": "string"}}},
               run=lambda title: ran.append(title) or {"saved": True}, requires_approval=True)


def wait_until_pending(run_id: str, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(p["run_id"] == run_id for p in approvals.list_pending()):
            return
        time.sleep(0.01)
    raise AssertionError("run never started waiting")


# ---------- the registry ----------

def test_decide_wakes_the_waiting_run():
    result = {}
    t = threading.Thread(target=lambda: result.update(d=approvals.wait_for_decision("r1", "save_report", {})), daemon=True)
    t.start()
    wait_until_pending("r1")
    assert approvals.decide("r1", approved=False, reason="not now")
    t.join(2)
    assert result["d"] == ("rejected", "not now")
    assert approvals.list_pending() == []


def test_timeout_counts_as_not_approved():
    assert approvals.wait_for_decision("r2", "save_report", {}, timeout=0.05)[0] == "timeout"


def test_deciding_when_nothing_waits_returns_false():
    assert approvals.decide("nobody", approved=True) is False


# ---------- in the loop ----------

def scripted(monkeypatch, responses):
    seen = []

    def fake(system, messages, tools, force_tool=None):
        seen.append(messages[-1])
        return responses[len(seen) - 1]

    monkeypatch.setattr(loop, "call_llm", fake)
    return seen


SAVE = [LLMResponse("", [ToolCall("c1", "save_report", {"title": "Q1"})]), LLMResponse("Done.")]


@pytest.mark.parametrize("decision, runs_tool", [("approved", True), ("rejected", False), ("timeout", False)])
def test_flagged_tool_waits_for_decision(monkeypatch, decision, runs_tool):
    ran.clear()
    seen = scripted(monkeypatch, SAVE)
    r = loop.run_agent("sys", [FLAGGED], "save it", run_id="run-x", approve=lambda call: lambda: (decision, "because"))
    types = [e["type"] for e in r.trace]
    assert types[:3] == ["llm", "approval_required", "approval"]
    assert r.trace[1]["run_id"] == "run-x" and r.trace[1]["input"] == {"title": "Q1"}
    assert r.trace[2]["decision"] == decision and r.trace[2]["reason"] == "because"
    assert (ran == ["Q1"]) is runs_tool
    assert ("tool" in types) is runs_tool
    if not runs_tool:
        assert "NOT run" in seen[1]["content"] and decision in seen[1]["content"]


def test_no_approver_means_rejected(monkeypatch):
    ran.clear()
    scripted(monkeypatch, SAVE)
    r = loop.run_agent("sys", [FLAGGED], "save it")
    assert r.trace[2]["decision"] == "rejected" and ran == []


def test_unflagged_tools_never_ask(monkeypatch):
    plain = Tool("add", "add", {"type": "object", "properties": {}}, run=lambda: 1)
    scripted(monkeypatch, [LLMResponse("", [ToolCall("c", "add", {})]), LLMResponse("1")])
    asked = []
    loop.run_agent("sys", [plain], "q", approve=lambda call: asked.append(call) or (lambda: ("approved", "")))
    assert asked == []


# ---------- over HTTP ----------

@pytest.fixture
def fake_service(monkeypatch):
    statuses = []
    monkeypatch.setattr(service.runs, "create_run", lambda task, **kw: "11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(service.runs, "update_status", lambda run_id, status, trace=None: statuses.append(status))
    monkeypatch.setattr(service.runs, "finish_run", lambda *a, **kw: None)
    monkeypatch.setattr(service, "build_system_prompt", lambda custom, **kw: "sys")
    monkeypatch.setattr(service, "tools_for_run", lambda run_id, *a: [FLAGGED])
    return statuses


@pytest.mark.parametrize("endpoint, decision", [("approve", "approved"), ("reject", "rejected")])
def test_approve_and_reject_endpoints(monkeypatch, fake_service, endpoint, decision):
    ran.clear()
    scripted(monkeypatch, SAVE)
    run_id = "11111111-1111-1111-1111-111111111111"
    result = {}
    t = threading.Thread(target=lambda: result.update(r=service.execute(service.RunRequest(task="save it"))), daemon=True)
    t.start()
    wait_until_pending(run_id)

    assert client.get("/approvals").json()["pending"][0]["tool"] == "save_report"
    r = client.post(f"/runs/{run_id}/{endpoint}", json={"reason": "checked by reviewer"})
    assert r.status_code == 200 and r.json()["decision"] == decision
    t.join(5)

    approval = next(e for e in result["r"]["trace"] if e["type"] == "approval")
    assert approval["decision"] == decision and approval["reason"] == "checked by reviewer"
    assert fake_service == ["awaiting_approval", "running"]      # status shown while waiting
    assert (ran == ["Q1"]) is (decision == "approved")


def test_approving_a_run_that_is_not_waiting_is_404():
    assert client.post("/runs/22222222-2222-2222-2222-222222222222/approve").status_code == 404


def test_instant_decision_right_after_the_event_is_not_lost(monkeypatch, fake_service):
    """Regression: a client that approves the moment it sees approval_required must not get 404.

    The approval has to be registered BEFORE the event is announced.
    """
    ran.clear()
    scripted(monkeypatch, SAVE)
    accepted = []

    def on_event(e):
        if e["type"] == "approval_required":   # the fastest possible client
            accepted.append(approvals.decide(e["run_id"], approved=True, reason="instant"))

    result = {}
    t = threading.Thread(target=lambda: result.update(
        r=service.execute(service.RunRequest(task="save it"), on_event=on_event)), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "run is stuck waiting: the instant approval was lost"
    assert accepted == [True]
    assert ran == ["Q1"]
