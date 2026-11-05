"""Structured output via submit_answer (fake LLM, no network)."""
import pytest

from app.agent import loop
from app.agent.structured import check_output_schema
from app.llm import LLMResponse, ToolCall

SCHEMA = {
    "type": "object",
    "properties": {"region": {"type": "string"}, "margin_pct": {"type": "number"}},
    "required": ["region", "margin_pct"],
    "additionalProperties": False,
}


def scripted(monkeypatch, responses):
    calls = []

    def fake(system, messages, tools, force_tool=None):
        calls.append({"force_tool": force_tool, "tools": [t.name for t in tools], "last": messages[-1]})
        return responses[len(calls) - 1]

    monkeypatch.setattr(loop, "call_llm", fake)
    return calls


def submit(args, id="s1"):
    return LLMResponse(text="", tool_calls=[ToolCall(id, "submit_answer", args)])


def test_valid_submission_is_the_answer(monkeypatch):
    calls = scripted(monkeypatch, [submit({"region": "Oslo", "margin_pct": 46.3, "sources": []})])
    r = loop.run_agent("sys", [], "q", output_schema=SCHEMA)
    assert r.answer == {"region": "Oslo", "margin_pct": 46.3, "sources": []}
    assert calls[0]["tools"] == ["submit_answer"]
    assert r.trace[-1]["type"] == "final_answer" and r.trace[-1]["valid"]


def test_invalid_submission_gets_errors_and_retries(monkeypatch):
    calls = scripted(monkeypatch, [
        submit({"region": "Oslo", "margin_pct": "high", "sources": []}, "s1"),
        submit({"region": "Oslo", "margin_pct": 46.3, "sources": []}, "s2"),
    ])
    r = loop.run_agent("sys", [], "q", output_schema=SCHEMA)
    assert r.answer["margin_pct"] == 46.3
    assert "margin_pct" in calls[1]["last"]["content"]           # model saw the error
    assert [e["valid"] for e in r.trace if e["type"] == "final_answer"] == [False, True]


def test_plain_text_reply_forces_submit_answer(monkeypatch):
    calls = scripted(monkeypatch, [LLMResponse(text="Oslo, 46%"), submit({"region": "Oslo", "margin_pct": 46.0, "sources": []})])
    r = loop.run_agent("sys", [], "q", output_schema=SCHEMA)
    assert calls[0]["force_tool"] is None
    assert calls[1]["force_tool"] == "submit_answer"
    assert isinstance(r.answer, dict)


@pytest.mark.parametrize("bad", [{"type": "array"}, {"type": "object", "properties": {"x": {"type": "nope"}}}])
def test_bad_schemas_are_rejected(bad):
    with pytest.raises(Exception):
        check_output_schema(bad)
