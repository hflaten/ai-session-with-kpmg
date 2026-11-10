"""Orchestrator pattern: one coordinating agent and two specialists (Høyden inventory case).

A specialist is just run_agent() with its own system prompt and tools:
- data_analyst:      query_database + recommend_orders (numbers in the tables, and order plans)
- inventory_planner: recommend_orders only (what/when/how much to order, stockouts, waste, overstock)

The orchestrator has no data access of its own: its tools are `ask_data_analyst` and
`ask_inventory_planner`, which each start a specialist run (logged in agent_runs with
parent_run_id = the orchestrator's run), plus the write tools create_purchase_order and
save_report, which both need human approval (see service.tools_for_run).

The delegation tree is visible in three places:
- the orchestrator's trace: each delegation entry carries child_run_id + child_trace,
- the live stream: specialist steps are forwarded, tagged with their agent name,
- GET /runs/{id}/tree: rebuilt from agent_runs.parent_run_id.
"""
import time
from dataclasses import dataclass
from typing import Callable

from app import runs
from app.agent.language import detect_language
from app.agent.loop import run_agent
from app.llm import Tool
from app.tools.database import query_database_tool
from app.tools.inventory import recommend_orders_tool

ORCHESTRATOR_PROMPT = (
    "You coordinate two specialists and have no direct access to data yourself.\n"
    "- ask_inventory_planner: what, when and how much to order; stockout risk, expected waste and "
    "overstock (deterministic order plan).\n"
    "- ask_data_analyst: other questions about numbers in the database tables (sales, stock value, waste "
    "history, suppliers), and it can also check the order plan.\n"
    "You can create purchase orders yourself with create_purchase_order (a human must approve each one), "
    "but only when the user asks you to place or create orders; use the quantities and dates the planner gave.\n"
    "Split the user's task into focused questions, delegate them (both at once when they are "
    "independent), then combine the answers.\n"
    "Each specialist answers with its own checked `sources`. For every claim you keep, copy the matching "
    "specialist source into your sources (same tool, call_id, row and fields; or the same table and sql), "
    "adjusting only the claim text. Each specialist also reports `sql_run` and `tools_run` (with call_ids). "
    "Never invent numbers, call_ids or citations, and never cite ask_* tools: cite the underlying "
    "recommend_orders call or SQL."
)

# Specialists answer in this shape, so their claims arrive already cited and checked
SPECIALIST_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@dataclass
class Specialist:
    name: str
    description: str      # what the orchestrator reads when choosing whom to ask
    system_prompt: str
    tools: list[Tool]
    sees_tables: bool = False      # put the table list in its prompt?
    sees_documents: bool = False   # put the document list in its prompt? (no current specialist does)


SPECIALISTS = [
    Specialist(
        name="data_analyst",
        description=("Answers questions about numbers in the database tables, using SQL, and order plans "
                     "(what to order, stockouts, waste, overstock) using recommend_orders."),
        system_prompt=("You are a data analyst. Answer the question with query_database, and with recommend_orders "
                       "for order plans, stockout risk, waste risk and overstock. Give exact numbers, "
                       "say how you calculated them, and keep it short."),
        tools=[query_database_tool, recommend_orders_tool],
        sees_tables=True,
    ),
    Specialist(
        name="inventory_planner",
        description=("Plans orders with the deterministic order planner: what, when and how much to order, "
                     "stockout risk dates, expected waste and overstock, with NOK consequences."),
        system_prompt=("You are an inventory planner. Answer with recommend_orders only (filter by location_id "
                       "and/or sku for details). Give the exact quantities, order dates, stockout dates and NOK "
                       "figures it returns, say which location and product each one is for, and keep it short. "
                       "Never invent numbers the tool did not return."),
        tools=[recommend_orders_tool],
    ),
]


def make_delegation_tools(parent_run_id: str, emit: Callable[[dict], None],
                          build_prompt: Callable[..., str],
                          tool_output_saver: Callable[[str], Callable] | None = None) -> list[Tool]:
    """tool_output_saver(child_run_id) -> on_tool_output callback that stores each call's full output."""
    """One ask_<specialist> tool per specialist, bound to this orchestrator run."""
    child_traces: dict[str, list] = {}  # child_run_id -> trace, picked up by trace_fields below

    def make_tool(spec: Specialist) -> Tool:
        def delegate(question: str) -> dict:
            child_run_id = runs.create_run(question, agent_name=spec.name, parent_run_id=parent_run_id)
            emit({"type": "delegation_started", "agent": "orchestrator", "specialist": spec.name,
                  "child_run_id": child_run_id, "question": question})
            started = time.monotonic()

            def forward(entry: dict) -> None:  # stream the specialist's steps live, tagged
                emit({**entry, "agent": spec.name, "run_id": child_run_id})

            try:
                prompt = build_prompt(spec.system_prompt, tables=spec.sees_tables, documents=spec.sees_documents,
                                      language=detect_language(question))
                result = run_agent(prompt, spec.tools, question, output_schema=SPECIALIST_SCHEMA,
                                   on_event=forward, run_id=child_run_id,
                                   on_tool_output=tool_output_saver(child_run_id) if tool_output_saver else None)
            except Exception as e:
                runs.finish_run(child_run_id, "failed", [], error=f"{type(e).__name__}: {e}",
                                duration_ms=int((time.monotonic() - started) * 1000))
                raise
            status = "completed" if result.stopped_reason == "answered" else "failed"
            runs.finish_run(child_run_id, status, result.trace, answer=result.answer,
                            duration_ms=int((time.monotonic() - started) * 1000))
            child_traces[child_run_id] = result.trace

            # What the orchestrator sees: the answer plus the evidence it may cite
            return {"specialist": spec.name, "run_id": child_run_id, "answer": result.answer,
                    "chunks_found": result.evidence.chunk_list(), "sql_run": result.evidence.sql_list(),
                    "tools_run": result.evidence.tool_list()}

        return Tool(
            name=f"ask_{spec.name}",
            description=spec.description + " Ask one focused question; you get back its answer "
                                           "with cited sources, plus chunks_found, sql_run and tools_run.",
            input_schema={"type": "object",
                          "properties": {"question": {"type": "string", "description": "A self-contained question"}},
                          "required": ["question"]},
            run=delegate,
            # The specialist's full trace goes into the orchestrator's trace (not to the LLM): the tree
            trace_fields=lambda output: {"specialist": output["specialist"], "child_run_id": output["run_id"],
                                         "child_trace": child_traces.pop(output["run_id"], [])},
        )

    return [make_tool(spec) for spec in SPECIALISTS]
