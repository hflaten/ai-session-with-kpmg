from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent import run_agent
from app.agent.structured import check_output_schema
from app.llm import Tool
from app.tools.database import query_database_tool

router = APIRouter(prefix="/agent")

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful analyst. Use the available tools when they help. "
    "Be concise and say when you are unsure."
)

# Tools available to /agent/run. Filled in by later steps (query_database, search_documents).
TOOLS: list[Tool] = [query_database_tool]


class RunRequest(BaseModel):
    task: str
    system_prompt: str | None = None
    output_schema: dict | None = None   # JSON schema for a structured answer


@router.post("/run")
def run(req: RunRequest):
    """Run the agent to completion and return the answer plus the full trace."""
    if req.output_schema:
        try:
            check_output_schema(req.output_schema)
        except Exception as e:
            raise HTTPException(400, f"Invalid output_schema: {e}")
    result = run_agent(req.system_prompt or DEFAULT_SYSTEM_PROMPT, TOOLS, req.task,
                       output_schema=req.output_schema)
    return {"answer": result.answer, "stopped_reason": result.stopped_reason, "trace": result.trace}
