"""Anthropic Messages API adapter."""
import anthropic

from app.config import settings
from app.llm.types import LLMResponse, Tool, ToolCall

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def to_anthropic_tools(tools: list[Tool]) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Neutral messages -> Anthropic. Tool results go inside a *user* message,
    and consecutive tool results must be merged into that one message."""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for tc in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            result = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            previous = out[-1] if out else None
            if previous and previous["role"] == "user" and isinstance(previous["content"], list):
                previous["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


def from_anthropic_response(resp) -> LLMResponse:
    text = "".join(b.text for b in resp.content if b.type == "text")
    calls = [ToolCall(id=b.id, name=b.name, input=b.input) for b in resp.content if b.type == "tool_use"]
    return LLMResponse(text=text, tool_calls=calls,
                       input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens)


def call(system: str, messages: list[dict], tools: list[Tool], force_tool: str | None = None) -> LLMResponse:
    kwargs = {}
    if tools:
        kwargs["tools"] = to_anthropic_tools(tools)
    if force_tool:
        kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

    def create(**extra):
        return _get_client().messages.create(model=settings.anthropic_model, max_tokens=16000, system=system,
                                             messages=to_anthropic_messages(messages), **extra)
    try:
        resp = create(**kwargs)
    except anthropic.BadRequestError as e:
        # Some models don't support forcing a tool. The loop has already asked for it in
        # the conversation, so retry with the model choosing (it will call the tool).
        if "tool_choice" not in kwargs or "tool_choice" not in str(e):
            raise
        kwargs.pop("tool_choice")
        resp = create(**kwargs)
    return from_anthropic_response(resp)
