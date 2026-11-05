"""Adapter tests without network: real SDK response objects, fake client."""
from types import SimpleNamespace

import pytest
from anthropic.types import Message
from openai.types.chat import ChatCompletion

from app.llm import Tool, ToolCall, anthropic_adapter, openai_adapter, client

WEATHER = Tool(
    name="get_weather",
    description="Weather for a city",
    input_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)

# A conversation that has already used a tool once, in neutral format
CONVERSATION = [
    {"role": "user", "content": "Weather in Oslo and Bergen?"},
    {"role": "assistant", "content": "Checking.", "tool_calls": [
        ToolCall("call_1", "get_weather", {"city": "Oslo"}),
        ToolCall("call_2", "get_weather", {"city": "Bergen"}),
    ]},
    {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "5C"},
    {"role": "tool", "tool_call_id": "call_2", "name": "get_weather", "content": "8C rain"},
]


class FakeCreate:
    """Records the kwargs sent to the SDK and returns a canned response."""
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.response


# ---------- Anthropic ----------

def test_anthropic_messages_merge_tool_results_into_one_user_message():
    out = anthropic_adapter.to_anthropic_messages(CONVERSATION)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["call_1", "call_2"]


def test_anthropic_call_roundtrip(monkeypatch):
    resp = Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "content": [
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Oslo"}},
        ],
    })
    fake = FakeCreate(resp)
    monkeypatch.setattr(anthropic_adapter, "_get_client",
                        lambda: SimpleNamespace(messages=SimpleNamespace(create=fake)))

    r = anthropic_adapter.call("be brief", CONVERSATION[:1], [WEATHER], force_tool="get_weather")

    assert fake.kwargs["system"] == "be brief"
    assert fake.kwargs["tools"][0]["input_schema"] == WEATHER.input_schema
    assert fake.kwargs["tool_choice"] == {"type": "tool", "name": "get_weather"}
    assert r.text == "Let me check."
    assert r.tool_calls == [ToolCall("toolu_1", "get_weather", {"city": "Oslo"})]
    assert (r.input_tokens, r.output_tokens) == (11, 7)


# ---------- OpenAI-compatible ----------

def test_openai_messages_shape():
    out = openai_adapter.to_openai_messages("sys", CONVERSATION)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city": "Oslo"}'}


def test_openai_call_roundtrip(monkeypatch):
    resp = ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_9", "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'}}],
        }}],
    })
    fake = FakeCreate(resp)
    monkeypatch.setattr(openai_adapter, "_get_client",
                        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake))))

    r = openai_adapter.call("sys", CONVERSATION[:1], [WEATHER], force_tool="get_weather")

    assert fake.kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assert fake.kwargs["tools"][0]["function"]["parameters"] == WEATHER.input_schema
    assert fake.kwargs["tool_choice"]["function"]["name"] == "get_weather"
    assert r.text == ""
    assert r.tool_calls == [ToolCall("call_9", "get_weather", {"city": "Oslo"})]


def test_openai_bad_json_arguments_do_not_crash():
    resp = ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "x", "type": "function",
                            "function": {"name": "get_weather", "arguments": "{broken"}}]}}],
    })
    r = openai_adapter.from_openai_response(resp)
    assert r.tool_calls[0].input == {"_invalid_json": "{broken"}


# ---------- Provider switch ----------

@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_call_llm_routes_by_provider(monkeypatch, provider):
    called = []
    for name, adapter in client.ADAPTERS.items():
        monkeypatch.setattr(adapter, "call", lambda *a, n=name: called.append(n))
    monkeypatch.setattr(client, "settings", SimpleNamespace(llm_provider=provider))
    client.call_llm("s", [])
    assert called == [provider]


def test_unknown_provider_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(client, "settings", SimpleNamespace(llm_provider="gemini"))
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        client.call_llm("s", [])


@pytest.mark.parametrize("effort, expected", [("none", "none"), ("", None)])
def test_reasoning_effort_only_sent_when_set(monkeypatch, effort, expected):
    from dataclasses import replace
    resp = ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}]})
    fake = FakeCreate(resp)
    monkeypatch.setattr(openai_adapter, "_get_client",
                        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake))))
    monkeypatch.setattr(openai_adapter, "settings", replace(openai_adapter.settings, openai_reasoning_effort=effort))
    openai_adapter.call("s", [{"role": "user", "content": "x"}], [])
    assert fake.kwargs.get("reasoning_effort") == expected
