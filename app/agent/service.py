"""Running an agent for an HTTP request: prompt, tools, logging, approvals.

execute() is shared by /agent/run and /agent/stream. It:
1. creates a row in agent_runs,
2. builds the system prompt (+ current tables and documents) and the tools,
3. runs the agent, pausing for human approval on flagged tools,
4. saves the answer, trace and status.
"""
import time
from typing import Callable, Literal

from pydantic import BaseModel

from app import runs
from app.agent import approvals
from app.agent.loop import run_agent
from app.agent.orchestrator import ORCHESTRATOR_PROMPT, make_delegation_tools
from app.llm import Tool, ToolCall
from app.tools.database import describe_schema, query_database_tool
from app.tools.documents import describe_documents, search_documents_tool
from app.tools.reports import make_save_report_tool

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful analyst. Use the available tools when they help. "
    "Be concise and say when you are unsure."
)


class RunRequest(BaseModel):
    task: str
    system_prompt: str | None = None
    output_schema: dict | None = None   # JSON schema for a structured answer
    mode: Literal["agent", "orchestrator"] = "agent"   # orchestrator = delegate to two specialists


def build_system_prompt(custom: str | None, tables: bool = True, documents: bool = True) -> str:
    """Caller's prompt (or the default) + the current tables and/or documents.

    Read fresh each run (no caching), so new uploads show up immediately.
    Specialists only get the context for their own tool (tables OR documents).
    If the database is unreachable the agent still runs, just without them.
    """
    parts = [custom or DEFAULT_SYSTEM_PROMPT]
    describers = ([describe_schema] if tables else []) + ([describe_documents] if documents else [])
    for describe in describers:
        try:
            parts.append(describe())
        except Exception:
            pass
    return "\n\n".join(parts)


def tools_for_run(run_id: str, mode: str = "agent", emit: Callable[[dict], None] | None = None) -> list[Tool]:
    if mode == "orchestrator":
        # No direct data access: only delegation (+ the flagged save_report)
        return [*make_delegation_tools(run_id, emit or (lambda e: None), build_system_prompt),
                make_save_report_tool(run_id)]
    return [query_database_tool, search_documents_tool, make_save_report_tool(run_id)]


def make_approver(run_id: str, trace: list[dict]):
    """Pauses the run until POST /runs/{run_id}/approve or /reject (or the timeout).

    approve(call) registers the request right away and returns wait(); the loop
    announces the request in between, so an instant click is never lost.
    """
    def approve(call: ToolCall) -> Callable[[], tuple[str, str]]:
        pending = approvals.request(run_id, call.name, call.input)

        def wait() -> tuple[str, str]:
            runs.update_status(run_id, "awaiting_approval", trace)
            try:
                return approvals.wait(pending)
            finally:
                runs.update_status(run_id, "running", trace)
        return wait
    return approve


def execute(req: RunRequest, on_event: Callable[[dict], None] | None = None) -> dict:
    """Run the agent and log it to agent_runs."""
    emit = on_event or (lambda e: None)
    agent_name = req.mode                        # "agent" or "orchestrator"
    run_id = runs.create_run(req.task, agent_name=agent_name)
    emit({"type": "run_started", "run_id": run_id, "agent": agent_name})
    started = time.monotonic()
    trace: list[dict] = []

    def on_step(entry: dict) -> None:
        entry["agent"] = agent_name              # specialists' steps arrive tagged with their own name
        trace.append(entry)
        emit(entry)

    default_prompt = ORCHESTRATOR_PROMPT if req.mode == "orchestrator" else DEFAULT_SYSTEM_PROMPT
    try:
        result = run_agent(build_system_prompt(req.system_prompt or default_prompt),
                           tools_for_run(run_id, req.mode, emit), req.task,
                           output_schema=req.output_schema, on_event=on_step,
                           run_id=run_id, approve=make_approver(run_id, trace))
    except Exception as e:
        runs.finish_run(run_id, "failed", trace, error=f"{type(e).__name__}: {e}",
                        duration_ms=int((time.monotonic() - started) * 1000))
        raise
    status = "completed" if result.stopped_reason == "answered" else "failed"
    runs.finish_run(run_id, status, result.trace, answer=result.answer,
                    error=None if status == "completed" else result.answer,
                    duration_ms=int((time.monotonic() - started) * 1000))
    return {"run_id": run_id, "answer": result.answer,
            "stopped_reason": result.stopped_reason, "trace": result.trace}
