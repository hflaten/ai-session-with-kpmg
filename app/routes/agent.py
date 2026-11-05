"""Agent endpoints.

POST /agent/run      run to completion, return answer + trace
POST /agent/stream   same, but stream each step live (Server-Sent Events)
GET  /agent/stream   same, with ?task=... (for the browser's EventSource, which only does GET)
GET  /runs/{id}      a logged run from agent_runs
"""
import asyncio
import json
import queue
import threading
import time
from typing import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import runs
from app.agent import run_agent
from app.agent.structured import check_output_schema
from app.llm import Tool
from app.tools.database import describe_schema, query_database_tool

router = APIRouter()

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful analyst. Use the available tools when they help. "
    "Be concise and say when you are unsure."
)

# Tools available to the agent. search_documents is added in step 7.
TOOLS: list[Tool] = [query_database_tool]


class RunRequest(BaseModel):
    task: str
    system_prompt: str | None = None
    output_schema: dict | None = None   # JSON schema for a structured answer


def _validate(req: RunRequest) -> None:
    if req.output_schema:
        try:
            check_output_schema(req.output_schema)
        except Exception as e:
            raise HTTPException(400, f"Invalid output_schema: {e}")


def build_system_prompt(custom: str | None) -> str:
    """Caller's prompt (or the default) + the current table schemas.

    The schema is read fresh each run, so newly uploaded tables show up.
    If the database is unreachable the agent still runs, just without it.
    """
    base = custom or DEFAULT_SYSTEM_PROMPT
    try:
        return f"{base}\n\n{describe_schema()}"
    except Exception:
        return base


def execute(req: RunRequest, on_event: Callable[[dict], None] | None = None) -> dict:
    """Run the agent and log it to agent_runs. Shared by /run and /stream."""
    emit = on_event or (lambda e: None)
    run_id = runs.create_run(req.task)
    emit({"type": "run_started", "run_id": run_id})
    started = time.monotonic()
    trace: list[dict] = []

    def on_step(entry: dict) -> None:
        trace.append(entry)
        emit(entry)

    try:
        result = run_agent(build_system_prompt(req.system_prompt), TOOLS, req.task,
                           output_schema=req.output_schema, on_event=on_step)
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


@router.post("/agent/run")
def run(req: RunRequest):
    _validate(req)
    return execute(req)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream(req: RunRequest) -> StreamingResponse:
    """Run the agent in a thread; send each event to the client as it happens."""
    _validate(req)
    events: queue.Queue = queue.Queue()

    def worker():
        try:
            result = execute(req, on_event=events.put)
            events.put({"type": "done", **result})
        except Exception as e:
            events.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            events.put(None)  # end of stream

    threading.Thread(target=worker, daemon=True).start()

    async def body():
        while (event := await asyncio.to_thread(events.get)) is not None:
            name = event["type"] if event["type"] in ("run_started", "done", "error") else "step"
            yield _sse(name, event)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agent/stream")
def stream_post(req: RunRequest):
    return _stream(req)


@router.get("/agent/stream")
def stream_get(task: str, system_prompt: str | None = None):
    return _stream(RunRequest(task=task, system_prompt=system_prompt))


@router.get("/runs/{run_id}")
def get_run(run_id: UUID):
    row = runs.get_run(str(run_id))
    if row is None:
        raise HTTPException(404, "Run not found")
    return row
