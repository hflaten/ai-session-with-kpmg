"""OpenAI-compatible Chat Completions adapter.

Works with OpenAI, Azure OpenAI and most company gateways: point
OPENAI_BASE_URL at the endpoint.
"""
import json

import openai

from app.config import settings
from app.llm.types import LLMResponse, Tool, ToolCall

_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    return _client


def to_openai_tools(tools: list[Tool]) -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
        for t in tools
    ]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": json.dumps(tc.input)}}
                    for tc in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


def from_openai_response(resp) -> LLMResponse:
    msg = resp.choices[0].message
    calls = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {"_invalid_json": tc.function.arguments}  # let the agent see and retry
        calls.append(ToolCall(id=tc.id, name=tc.function.name, input=args))
    usage = resp.usage
    return LLMResponse(text=msg.content or "", tool_calls=calls,
                       input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                       output_tokens=getattr(usage, "completion_tokens", 0) or 0)


def call(system: str, messages: list[dict], tools: list[Tool], force_tool: str | None = None) -> LLMResponse:
    kwargs = {}
    if tools:
        kwargs["tools"] = to_openai_tools(tools)
    if force_tool:
        kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
    resp = _get_client().chat.completions.create(
        model=settings.openai_model,
        messages=to_openai_messages(system, messages),
        **kwargs,
    )
    return from_openai_response(resp)
