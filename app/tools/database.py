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


SCHEMA_SQL = """
    select c.relname                                as table_name,
           obj_description(c.oid, 'pg_class')       as table_comment,
           a.attname                                as column_name,
           format_type(a.atttypid, a.atttypmod)     as data_type,
           col_description(c.oid, a.attnum)         as column_comment
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
    where n.nspname = 'public'
      and c.relkind in ('r', 'v', 'm', 'p')          -- tables, views, materialized views
      and has_table_privilege(c.oid, 'SELECT')       -- only what this role can read
    order by c.relname, a.attnum
"""


def describe_schema() -> str:
    """List the tables the agent can read, with table and column comments.

    Called at the start of EVERY run, on purpose without caching, so newly
    uploaded tables and edited comments show up immediately.
    Uses the read-only connection, so it shows exactly what query_database can see.

    Output looks like:
        - sample_sales  -- Demo data: ...
            month date  -- First day of the month the sales belong to
            revenue numeric(12,2)  -- Revenue in NOK, excl. VAT
    """
    with db.connect(readonly=True) as conn:
        rows = conn.execute(SCHEMA_SQL).fetchall()
    if not rows:
        return "The database has no tables yet."

    tables: dict[str, list[str]] = {}
    for r in rows:
        if r["table_name"] not in tables:
            if len(tables) == MAX_SCHEMA_TABLES:
                break
            header = f"- {r['table_name']}" + (f"  -- {r['table_comment']}" if r["table_comment"] else "")
            tables[r["table_name"]] = [header]
        column = f"    {r['column_name']} {r['data_type']}"
        if r["column_comment"]:
            column += f"  -- {r['column_comment']}"
        tables[r["table_name"]].append(column)

    body = "\n".join(line for lines in tables.values() for line in lines)
    return "Tables you can query with query_database (schema 'public'). Comments after -- explain the data:\n" + body


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
