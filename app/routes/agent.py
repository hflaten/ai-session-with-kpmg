"""Agent endpoints (HTTP only; the logic is in app/agent/service.py).

POST /agent/run               run to completion, return answer + trace
POST /agent/stream            same, but stream each step live (Server-Sent Events)
GET  /agent/stream            same, with ?task=... (for the browser's EventSource, which only does GET)
GET  /runs/{id}               a logged run from agent_runs
GET  /approvals               runs currently waiting for a human decision
POST /runs/{id}/approve       let the waiting action run      body: {"reason": "..."} (optional)
POST /runs/{id}/reject        don't run it                     body: {"reason": "..."} (optional)
"""
import asyncio
import json
import queue
import threading
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import runs
from app.agent import approvals, service
from app.agent.service import RunRequest
from app.agent.structured import check_output_schema

router = APIRouter()

# Event types that get their own SSE event name; every other trace entry is sent as "step"
NAMED_EVENTS = {"run_started", "approval_required", "done", "error"}


def _validate(req: RunRequest) -> None:
    if req.output_schema:
        try:
            check_output_schema(req.output_schema)
        except Exception as e:
            raise HTTPException(400, f"Invalid output_schema: {e}")


@router.post("/agent/run")
def run(req: RunRequest):
    _validate(req)
    return service.execute(req)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream(req: RunRequest) -> StreamingResponse:
    """Run the agent in a thread; send each event to the client as it happens."""
    _validate(req)
    events: queue.Queue = queue.Queue()

    def worker():
        try:
            result = service.execute(req, on_event=events.put)
            events.put({"type": "done", **result})
        except Exception as e:
            events.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            events.put(None)  # end of stream

    threading.Thread(target=worker, daemon=True).start()

    async def body():
        while (event := await asyncio.to_thread(events.get)) is not None:
            yield _sse(event["type"] if event["type"] in NAMED_EVENTS else "step", event)

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


# ---------- human-in-the-loop ----------

class Decision(BaseModel):
    reason: str = ""


@router.get("/approvals")
def pending_approvals():
    return {"pending": approvals.list_pending()}


def _decide(run_id: UUID, approved: bool, body: Decision | None):
    if not approvals.decide(str(run_id), approved, body.reason if body else ""):
        raise HTTPException(404, "This run is not waiting for an approval")
    return {"run_id": str(run_id), "decision": "approved" if approved else "rejected"}


@router.post("/runs/{run_id}/approve")
def approve(run_id: UUID, body: Decision | None = None):
    return _decide(run_id, True, body)


@router.post("/runs/{run_id}/reject")
def reject(run_id: UUID, body: Decision | None = None):
    return _decide(run_id, False, body)
