"""Logging agent runs to the agent_runs table (main DB connection)."""
from psycopg.types.json import Jsonb

from app import db
from app.config import settings


def create_run(task: str, agent_name: str = "agent", parent_run_id: str | None = None) -> str:
    with db.connect() as conn:
        row = conn.execute(
            """insert into agent_runs (task, agent_name, parent_run_id, provider, model, status)
               values (%s, %s, %s, %s, %s, 'running') returning id""",
            (task, agent_name, parent_run_id, settings.llm_provider, settings.llm_model),
        ).fetchone()
    return str(row["id"])


def update_status(run_id: str, status: str, trace: list | None = None) -> None:
    """Used mid-run, e.g. 'awaiting_approval' in step 8."""
    with db.connect() as conn:
        if trace is None:
            conn.execute("update agent_runs set status = %s where id = %s", (status, run_id))
        else:
            conn.execute("update agent_runs set status = %s, trace = %s where id = %s",
                         (status, Jsonb(trace), run_id))


def finish_run(run_id: str, status: str, trace: list, answer=None, error: str | None = None,
               duration_ms: int | None = None) -> None:
    with db.connect() as conn:
        conn.execute(
            """update agent_runs
               set status = %s, answer = %s, trace = %s, error = %s, duration_ms = %s
               where id = %s""",
            (status, Jsonb(answer) if answer is not None else None, Jsonb(trace), error, duration_ms, run_id),
        )


def get_run(run_id: str) -> dict | None:
    with db.connect() as conn:
        return conn.execute("select * from agent_runs where id = %s", (run_id,)).fetchone()


def get_tree(run_id: str) -> dict | None:
    """The run and all runs it delegated to, nested: {id, agent_name, status, task, ..., children: [...]}."""
    with db.connect() as conn:
        rows = conn.execute(
            """with recursive tree as (
                   select * from agent_runs where id = %s
                   union all
                   select r.* from agent_runs r join tree t on r.parent_run_id = t.id
               )
               select id, parent_run_id, agent_name, task, status, duration_ms, created_at
               from tree order by created_at""",
            (run_id,),
        ).fetchall()
    if not rows:
        return None
    nodes = {str(r["id"]): {**r, "id": str(r["id"]), "children": []} for r in rows}
    for node in nodes.values():
        parent = node["parent_run_id"] and str(node["parent_run_id"])
        if parent in nodes:
            nodes[parent]["children"].append(node)
    return nodes[run_id]


# ---------- full tool outputs (evidence for tool citations) ----------

def save_tool_call(run_id: str, call_id: str, tool: str, arguments: dict, output) -> None:
    """Store one tool call's full output. json round-trip turns dates etc. into plain JSON."""
    with db.connect() as conn:
        conn.execute(
            "insert into tool_calls (call_id, run_id, tool, arguments, output) values (%s, %s, %s, %s, %s) "
            "on conflict (call_id) do nothing",
            (call_id, run_id, tool, Jsonb(_plain_json(arguments)), Jsonb(_plain_json(output))))


def get_tool_call(call_id: str) -> dict | None:
    with db.connect() as conn:
        return conn.execute("select call_id, run_id, tool, arguments, output from tool_calls where call_id = %s",
                            (call_id,)).fetchone()


def _plain_json(value):
    import json
    return json.loads(json.dumps(value, default=str))
