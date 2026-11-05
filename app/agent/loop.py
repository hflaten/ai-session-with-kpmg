"""The reusable agent loop.

    result = run_agent(system_prompt, tools, task)                       # answer is text
    result = run_agent(system_prompt, tools, task, output_schema=schema)  # answer is a dict

Each step: ask the LLM -> if it called tools, run them and feed the results
back -> repeat. Stops when the LLM gives its final answer, or after max_steps.
Every LLM call and tool call is recorded in result.trace.

With output_schema, the final answer must come through the submit_answer tool
(see structured.py). It is validated against the schema, it always contains a
`sources` list, and every source is checked against what the agent actually
found in this run.

Tools with requires_approval=True pause the run: the loop records an
`approval_required` entry, calls approve(call) (which blocks until a human
decides), records the decision, and only runs the tool if it was approved.
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent.structured import (SUBMIT_TOOL_NAME, Evidence, citation_errors, make_submit_tool,
                                  validation_errors, with_sources)
from app.llm import Tool, ToolCall, call_llm

MAX_TOOL_OUTPUT_CHARS = 20_000   # what the LLM sees from one tool call
MAX_TRACE_OUTPUT_CHARS = 2_000   # what the trace keeps (readability)


# Two-phase approval, so a fast "approve" click can never arrive before we're listening:
#   wait = approve(call)        1. register the request (returns a function)
#   ...announce it in the trace/stream...
#   decision, reason = wait()   2. block until a human decides
# decision is "approved", "rejected" or "timeout".
Approver = Callable[[ToolCall], Callable[[], tuple[str, str]]]


@dataclass
class AgentResult:
    answer: str | dict                 # dict when output_schema was given
    trace: list[dict] = field(default_factory=list)
    stopped_reason: str = "answered"   # answered | max_steps
    evidence: Evidence = field(default_factory=Evidence)  # chunks found + SQL run (used by the orchestrator)


def _to_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [truncated, {len(text)} chars]"


def _run_tool(tool: Tool | None, name: str, args: dict) -> tuple[Any, str | None]:
    """Run one tool -> (output, error). Errors become text for the LLM so it can recover."""
    if tool is None or tool.run is None:
        return f"Error: unknown tool '{name}'", "unknown tool"
    try:
        return tool.run(**args), None
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}", str(e)


def run_agent(system_prompt: str, tools: list[Tool], task: str, max_steps: int = 10,
              output_schema: dict | None = None,
              on_event: Callable[[dict], None] | None = None,
              run_id: str | None = None, approve: Approver | None = None) -> AgentResult:
    tools = list(tools)
    if output_schema:
        output_schema = with_sources(output_schema)  # every structured answer cites its sources
        tools.append(make_submit_tool(output_schema))
    tools_by_name = {t.name: t for t in tools}

    messages: list[dict] = [{"role": "user", "content": task}]
    trace: list[dict] = []
    evidence = Evidence()  # chunks found and SQL run, for checking citations
    force_tool = None      # set when the model must call submit_answer next

    def record(entry: dict) -> None:
        trace.append(entry)
        if on_event:
            on_event(entry)  # used for live streaming

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
                return AgentResult(answer=response.text, trace=trace, evidence=evidence)
            # ...unless we need structured output: ask again and force submit_answer.
            messages.append({"role": "user", "content": f"Now call {SUBMIT_TOOL_NAME} with your final answer."})
            force_tool = SUBMIT_TOOL_NAME
            continue

        for call in response.tool_calls:
            started = time.monotonic()

            if call.name == SUBMIT_TOOL_NAME and output_schema:
                errors = validation_errors(output_schema, call.input)
                if not errors:
                    errors = citation_errors(call.input["sources"], evidence)
                record({"step": step, "type": "final_answer", "input": call.input, "valid": not errors,
                        "errors": errors, "ms": int((time.monotonic() - started) * 1000)})
                if not errors:
                    return AgentResult(answer=call.input, trace=trace, evidence=evidence)
                output_text = ("Your answer was rejected. Fix these and call submit_answer again:\n"
                               + "\n".join(errors))
            else:
                tool = tools_by_name.get(call.name)

                # Flagged tools wait for a human decision first
                decision, reason = "approved", ""
                if tool is not None and tool.requires_approval:
                    decision, reason = _ask_for_approval(call, step, run_id, approve, record)

                if decision != "approved":
                    # Rejected or timed out: don't run it, and tell the model why
                    output_text = (f"The user did not approve {call.name} ({decision}"
                                   + (f": {reason}" if reason else "") + "). It was NOT run. "
                                   "Do not call it again; tell the user what you would have done instead.")
                else:
                    output, error = _run_tool(tool, call.name, call.input)
                    output_text = _to_text(output)
                    entry = {"step": step, "type": "tool", "tool": call.name, "input": call.input}
                    if error is None:
                        evidence.add(call.name, call.input, output)
                        if tool.trace_fields:
                            entry.update(tool.trace_fields(output))  # e.g. chunk_ids for search_documents
                    entry.update({"output": _clip(output_text, MAX_TRACE_OUTPUT_CHARS), "error": error,
                                  "ms": int((time.monotonic() - started) * 1000)})
                    record(entry)

            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                             "content": _clip(output_text, MAX_TOOL_OUTPUT_CHARS)})

    return AgentResult(answer=f"Stopped after {max_steps} steps without a final answer.",
                       trace=trace, stopped_reason="max_steps", evidence=evidence)


def _ask_for_approval(call: ToolCall, step: int, run_id: str | None, approve: Approver | None,
                      record: Callable[[dict], None]) -> tuple[str, str]:
    """Pause for a human decision and record it in the trace."""
    wait = approve(call) if approve else None   # 1. register first...
    record({"step": step, "type": "approval_required", "run_id": run_id,
            "tool": call.name, "input": call.input})   # 2. ...then announce...
    started = time.monotonic()
    if wait is None:
        decision, reason = "rejected", "no approver is available for this run"
    else:
        decision, reason = wait()                       # 3. ...then wait for the decision
    record({"step": step, "type": "approval", "tool": call.name, "decision": decision,
            "reason": reason, "waited_ms": int((time.monotonic() - started) * 1000)})
    return decision, reason
