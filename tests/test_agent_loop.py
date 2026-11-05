"""Agent loop tests with a scripted fake LLM (no network)."""
import pytest

from app.agent import loop
from app.llm import LLMResponse, Tool, ToolCall

ADD = Tool("add", "Add two numbers",
           {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
           run=lambda a, b: a + b)
BROKEN = Tool("broken", "Always fails", {"type": "object", "properties": {}},
              run=lambda: 1 / 0)


def scripted_llm(monkeypatch, responses):
    """Replace call_llm with one that returns `responses` in order and records what it was sent."""
    seen = []

    def fake(system, messages, tools, force_tool=None):
        seen.append([dict(m) for m in messages])
        return responses[len(seen) - 1]

    monkeypatch.setattr(loop, "call_llm", fake)
    return seen


def test_answers_directly_without_tools(monkeypatch):
    scripted_llm(monkeypatch, [LLMResponse(text="Hello!")])
    r = loop.run_agent("sys", [ADD], "hi")
    assert r.answer == "Hello!"
    assert r.stopped_reason == "answered"
    assert [e["type"] for e in r.trace] == ["llm"]


def test_tool_result_is_fed_back_then_answer(monkeypatch):
    seen = scripted_llm(monkeypatch, [
        LLMResponse(text="", tool_calls=[ToolCall("c1", "add", {"a": 2, "b": 3})]),
        LLMResponse(text="It is 5."),
    ])
    events = []
    r = loop.run_agent("sys", [ADD], "2+3?", on_event=events.append)

    assert r.answer == "It is 5."
    assert [(e["step"], e["type"]) for e in r.trace] == [(1, "llm"), (1, "tool"), (2, "llm")]
    assert r.trace[1]["output"] == "5"
    # second LLM call saw the tool result
    assert seen[1][-1] == {"role": "tool", "tool_call_id": "c1", "name": "add", "content": "5"}
    assert events == r.trace  # every trace entry was emitted live


def test_tool_errors_are_returned_to_the_llm(monkeypatch):
    seen = scripted_llm(monkeypatch, [
        LLMResponse(text="", tool_calls=[ToolCall("c1", "broken", {}), ToolCall("c2", "nope", {})]),
        LLMResponse(text="Sorry, tools failed."),
    ])
    r = loop.run_agent("sys", [BROKEN], "go")
    assert r.answer == "Sorry, tools failed."
    assert r.trace[1]["error"] == "division by zero"
    assert r.trace[2]["error"] == "unknown tool"
    assert seen[1][-1]["content"].startswith("Error: unknown tool")


def test_stops_after_max_steps(monkeypatch):
    forever = LLMResponse(text="", tool_calls=[ToolCall("c", "add", {"a": 1, "b": 1})])
    scripted_llm(monkeypatch, [forever] * 10)
    r = loop.run_agent("sys", [ADD], "loop", max_steps=10)
    assert r.stopped_reason == "max_steps"
    assert sum(e["type"] == "llm" for e in r.trace) == 10


def test_long_tool_output_is_clipped(monkeypatch):
    big = Tool("big", "big output", {"type": "object", "properties": {}}, run=lambda: "x" * 50_000)
    seen = scripted_llm(monkeypatch, [
        LLMResponse(text="", tool_calls=[ToolCall("c", "big", {})]), LLMResponse(text="ok")])
    r = loop.run_agent("sys", [big], "go")
    assert len(r.trace[1]["output"]) < 2_100
    assert len(seen[1][-1]["content"]) < 20_100
