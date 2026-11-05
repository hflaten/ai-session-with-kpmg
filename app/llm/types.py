"""Provider-neutral types. Everything outside app/llm/ only uses these.

Conversation messages are plain dicts in one neutral shape:

    {"role": "user",      "content": "question"}
    {"role": "assistant", "content": "text", "tool_calls": [ToolCall, ...]}
    {"role": "tool",      "tool_call_id": "...", "name": "...", "content": "result"}

Each adapter translates these to and from its provider's format.
"""
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    """A tool, defined once for every provider."""
    name: str
    description: str
    input_schema: dict                       # JSON schema of the arguments
    run: Callable[..., Any] | None = None    # python function called with the arguments
    requires_approval: bool = False          # step 8: pause for a human first


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str                                       # may be "" when only tools are called
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
