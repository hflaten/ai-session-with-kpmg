"""Live tests against Supabase (skipped if the read-only URL is not configured)."""
import pytest

from app.config import settings
from app.routes import agent as agent_routes
from app.tools.database import MAX_ROWS, describe_schema, query_database

pytestmark = pytest.mark.skipif(not settings.readonly_db_url, reason="no SUPABASE_READONLY_DB_URL")


def test_select_returns_columns_and_plain_numbers():
    r = query_database("select region, sum(revenue) as revenue from sample_sales group by region order by region")
    assert r["columns"] == ["region", "revenue"]
    assert r["rows"][0] == {"region": "Bergen", "revenue": 1022500.0}


@pytest.mark.parametrize("sql", [
    "delete from sample_sales",
    "update sample_sales set units = 0",
    "create table hack(x int)",
    "select * from agent_runs",
])
def test_writes_and_run_logs_are_blocked(sql):
    with pytest.raises(Exception):
        query_database(sql)


def test_row_limit():
    r = query_database("select generate_series(1, 500) as n")
    assert r["row_count"] == MAX_ROWS and r["truncated"] is True


def test_schema_lists_readable_tables_only():
    text = describe_schema()
    assert "- sample_sales(id integer, month date, region text" in text
    assert "Demo data" in text            # table comment included
    assert "agent_runs" not in text       # not readable by the agent -> not shown


def test_system_prompt_survives_db_failure(monkeypatch):
    def boom():
        raise ConnectionError("db down")
    monkeypatch.setattr(agent_routes, "describe_schema", boom)
    assert agent_routes.build_system_prompt("Be brief.") == "Be brief."
