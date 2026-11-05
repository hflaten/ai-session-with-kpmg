"""The reusable agent loop.

    result = run_agent(system_prompt, tools, task)                       # answer is text
    result = run_agent(system_prompt, tools, task, output_schema=schema)  # answer is a dict

Each step: ask the LLM -> if it called tools, run them and feed the results
back -> repeat. Stops when the LLM gives its final answer, or after max_steps.
Every LLM call and tool call is recorded in result.trace.

With output_schema, the final answer must come through the submit_answer tool
(see structured.py) and is validated against the schema.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Callable

from app.agent.structured import SUBMIT_TOOL_NAME, make_submit_tool, validation_errors
from app.llm import Tool, call_llm

MAX_TOOL_OUTPUT_CHARS = 20_000   # what the LLM sees from one tool call
MAX_TRACE_OUTPUT_CHARS = 2_000   # what the trace keeps (readability)


@dataclass
class AgentResult:
    answer: str | dict                 # dict when output_schema was given
    trace: list[dict] = field(default_factory=list)
    stopped_reason: str = "answered"   # answered | max_steps


def _to_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [truncated, {len(text)} chars]"


def _run_tool(tool: Tool | None, name: str, args: dict) -> tuple[str, str | None]:
    """Run one tool. Errors are returned to the LLM as text so it can recover."""
    if tool is None or tool.run is None:
        return f"Error: unknown tool '{name}'", "unknown tool"
    try:
        return _to_text(tool.run(**args)), None
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}", str(e)


def run_agent(system_prompt: str, tools: list[Tool], task: str, max_steps: int = 10,
              output_schema: dict | None = None,
              on_event: Callable[[dict], None] | None = None) -> AgentResult:
    tools = list(tools)
    if output_schema:
        tools.append(make_submit_tool(output_schema))
    tools_by_name = {t.name: t for t in tools}

    messages: list[dict] = [{"role": "user", "content": task}]
    trace: list[dict] = []
    force_tool = None  # set when the model must call submit_answer next

    def record(entry: dict) -> None:
        trace.append(entry)
        if on_event:
            on_event(entry)  # used for live streaming in step 6

    for step in range(1, max_steps + 1):
        started = time.monotonic()
        response = call_llm(system_prompt, messages, tools, force_tool=force_tool)
        force_tool = None
        record({
            "step": step, "type": "llm", "text": response.text,
            "tool_calls": [{"name": c.name, "input": c.input} for c in response.tool_calls],
            "tokens": {"input": response.input_tokens, "output": response.output_tokens},
            "ms": int((time.monotonic() - started) * 1000),
        })
        messages.append({"role": "assistant", "content": response.text, "tool_calls": response.tool_calls})

        # No tool calls: a plain-text final answer...
        if not response.tool_calls:
            if not output_schema:
                return AgentResult(answer=response.text, trace=trace)
            # ...unless we need structured output: ask again and force submit_answer.
            messages.append({"role": "user", "content": f"Now call {SUBMIT_TOOL_NAME} with your final answer."})
            force_tool = SUBMIT_TOOL_NAME
            continue

        for call in response.tool_calls:
            started = time.monotonic()

            if call.name == SUBMIT_TOOL_NAME and output_schema:
                errors = validation_errors(output_schema, call.input)
                record({"step": step, "type": "final_answer", "input": call.input, "valid": not errors,
                        "errors": errors, "ms": int((time.monotonic() - started) * 1000)})
                if not errors:
                    return AgentResult(answer=call.input, trace=trace)
                output = "Your answer does not match the schema. Fix these and call submit_answer again:\n" \
                         + "\n".join(errors)
            else:
                output, error = _run_tool(tools_by_name.get(call.name), call.name, call.input)
                record({
                    "step": step, "type": "tool", "tool": call.name, "input": call.input,
                    "output": _clip(output, MAX_TRACE_OUTPUT_CHARS), "error": error,
                    "ms": int((time.monotonic() - started) * 1000),
                })

            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                             "content": _clip(output, MAX_TOOL_OUTPUT_CHARS)})

    return AgentResult(answer=f"Stopped after {max_steps} steps without a final answer.",
                       trace=trace, stopped_reason="max_steps")
