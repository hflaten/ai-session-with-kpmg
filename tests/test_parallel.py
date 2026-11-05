"""Parallel tool calls in one LLM response (fake LLM, no network)."""
import time

from app.agent import loop
from app.llm import LLMResponse, Tool, ToolCall


def slow_tool(name: str, seconds: float, log: list) -> Tool:
    def run():
        log.append((name, "start", time.monotonic()))
        time.sleep(seconds)
        log.append((name, "end", time.monotonic()))
        return f"{name} done"
    return Tool(name, name, {"type": "object", "properties": {}}, run=run)


def scripted(monkeypatch, responses):
    seen = []

    def fake(system, messages, tools, force_tool=None):
        seen.append([dict(m) for m in messages])
        return responses[len(seen) - 1]

    monkeypatch.setattr(loop, "call_llm", fake)
    return seen


def test_tools_in_one_response_overlap_and_keep_their_order(monkeypatch):
    log = []
    slow_first = slow_tool("a", 0.4, log)   # finishes LAST
    fast_second = slow_tool("b", 0.2, log)  # finishes first
    seen = scripted(monkeypatch, [
        LLMResponse("", [ToolCall("1", "a", {}), ToolCall("2", "b", {})]),
        LLMResponse("both done"),
    ])
    t0 = time.monotonic()
    r = loop.run_agent("sys", [slow_first, fast_second], "go")
    elapsed = time.monotonic() - t0

    assert elapsed < 0.55, f"took {elapsed:.2f}s - tools ran one after the other"
    starts = {name: t for name, kind, t in log if kind == "start"}
    ends = {name: t for name, kind, t in log if kind == "end"}
    assert starts["b"] < ends["a"]                                   # b started before a finished
    tools = [e for e in r.trace if e["type"] == "tool"]
    assert [e["tool"] for e in tools] == ["a", "b"]                  # trace keeps call order
    assert all(e["parallel"] for e in tools)
    assert tools[0]["ms"] >= 390 and tools[1]["ms"] < 390            # each keeps its own timing
    assert [m["tool_call_id"] for m in seen[1] if m["role"] == "tool"] == ["1", "2"]  # LLM sees call order


def test_single_tool_call_is_not_marked_parallel(monkeypatch):
    log = []
    scripted(monkeypatch, [LLMResponse("", [ToolCall("1", "a", {})]), LLMResponse("ok")])
    r = loop.run_agent("sys", [slow_tool("a", 0.01, log)], "go")
    assert r.trace[1]["parallel"] is False


def test_flagged_tools_are_not_run_in_parallel(monkeypatch):
    log, order = [], []
    flagged = Tool("save_report", "save", {"type": "object", "properties": {}},
                   run=lambda: order.append("save") or "saved", requires_approval=True)
    scripted(monkeypatch, [
        LLMResponse("", [ToolCall("1", "a", {}), ToolCall("2", "b", {}), ToolCall("3", "save_report", {})]),
        LLMResponse("ok"),
    ])

    def approve(call):
        order.append("asked")
        return lambda: ("approved", "")

    r = loop.run_agent("sys", [slow_tool("a", 0.1, log), slow_tool("b", 0.1, log), flagged], "go", approve=approve)
    assert order == ["asked", "save"]                      # approval first, then the tool ran
    assert [e["type"] for e in r.trace] == ["llm", "tool", "tool", "approval_required", "approval", "tool", "llm"]
    assert r.trace[5]["parallel"] is False                 # save_report ran on its own
