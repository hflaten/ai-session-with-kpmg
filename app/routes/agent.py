from fastapi import APIRouter
from pydantic import BaseModel

from app.agent import run_agent
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


@router.post("/run")
def run(req: RunRequest):
    """Run the agent to completion and return the answer plus the full trace."""
    result = run_agent(req.system_prompt or DEFAULT_SYSTEM_PROMPT, TOOLS, req.task)
    return {"answer": result.answer, "stopped_reason": result.stopped_reason, "trace": result.trace}
