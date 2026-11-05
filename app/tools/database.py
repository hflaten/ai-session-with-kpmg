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


MAX_SCHEMA_TABLES = 50


def describe_schema() -> str:
    """List the tables the agent can read, for its system prompt.

    Uses the read-only connection, so it shows exactly what query_database can see.
    Saves the agent a tool call to discover the schema itself.
    """
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            """select c.table_name,
                      string_agg(c.column_name || ' ' || c.data_type, ', ' order by c.ordinal_position) as columns,
                      obj_description(format('public.%%I', c.table_name)::regclass) as comment
               from information_schema.columns c
               where c.table_schema = 'public'
               group by c.table_name
               order by c.table_name
               limit %s""",
            (MAX_SCHEMA_TABLES,),
        ).fetchall()
    if not rows:
        return "The database has no tables yet."
    lines = [f"- {r['table_name']}({r['columns']})" + (f"  -- {r['comment']}" if r["comment"] else "")
             for r in rows]
    return "Tables you can query with query_database (schema 'public'):\n" + "\n".join(lines)


query_database_tool = Tool(
    name="query_database",
    description=(
        "Run one read-only PostgreSQL SELECT query and get the rows back as JSON "
        f"(max {MAX_ROWS} rows). The available tables and columns are listed in your instructions; "
        "only query information_schema if a table you need is not listed. "
        "Aggregate in SQL (sum, avg, group by) rather than fetching raw rows."
    ),
    input_schema={
        "type": "object",
        "properties": {"sql": {"type": "string", "description": "A single PostgreSQL SELECT statement"}},
        "required": ["sql"],
    },
    run=query_database,
)
