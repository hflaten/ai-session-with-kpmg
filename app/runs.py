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
