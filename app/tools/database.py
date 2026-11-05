"""query_database: lets the agent run read-only SQL.

Two layers of protection:
1. It connects as the agent_readonly role, which can only SELECT (enforced by Postgres).
2. Each query runs in a READ ONLY transaction with a row limit.
"""
from decimal import Decimal

from app import db
from app.llm import Tool

MAX_ROWS = 200


def _clean(row: dict) -> dict:
    """Postgres numeric -> Decimal; turn it into a plain number for JSON."""
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


def query_database(sql: str) -> dict:
    with db.connect(readonly=True) as conn:
        conn.read_only = True  # psycopg starts a READ ONLY transaction
        cur = conn.execute(sql)
        if cur.description is None:
            return {"columns": [], "rows": [], "row_count": 0, "truncated": False}
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        rows = [_clean(r) for r in rows[:MAX_ROWS]]
        return {
            "columns": [c.name for c in cur.description],
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }


query_database_tool = Tool(
    name="query_database",
    description=(
        "Run one read-only PostgreSQL SELECT query and get the rows back as JSON "
        f"(max {MAX_ROWS} rows). Data tables are in the 'public' schema. If you don't know "
        "the tables yet, first run: select table_name, column_name, data_type from "
        "information_schema.columns where table_schema = 'public' order by table_name, ordinal_position. "
        "Aggregate in SQL (sum, avg, group by) rather than fetching raw rows."
    ),
    input_schema={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single PostgreSQL SELECT statement"}},
        "required": ["sql"],
    },
    run=query_database,
)
