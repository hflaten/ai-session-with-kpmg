"""Live tests against Supabase (skipped if the read-only URL is not configured)."""
import pytest

from app.config import settings
from app.agent import service
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


def test_schema_lists_readable_tables_with_comments():
    text = describe_schema()
    assert "- sample_sales  -- Demo data" in text                          # table comment
    assert "    revenue numeric(12,2)  -- Revenue in NOK, excluding VAT" in text  # column + comment
    assert "agent_runs" not in text       # not readable by the agent -> not shown


def test_schema_is_read_fresh_each_call():
    """No caching: a table created after the first call appears on the next one."""
    from app import db
    name = "zz_schema_fresh_test"
    assert name not in describe_schema()
    try:
        with db.connect() as conn:
            conn.execute(f"create table public.{name} (amount numeric)")
            conn.execute(f"comment on table public.{name} is 'temp table'")
            conn.execute(f"comment on column public.{name}.amount is 'amount in NOK'")
        text = describe_schema()
        assert f"- {name}  -- temp table" in text
        assert "    amount numeric  -- amount in NOK" in text
    finally:
        with db.connect() as conn:
            conn.execute(f"drop table if exists public.{name}")


def test_system_prompt_survives_db_failure(monkeypatch):
    def boom():
        raise ConnectionError("db down")
    monkeypatch.setattr(service, "describe_schema", boom)
    monkeypatch.setattr(service, "describe_documents", boom)
    assert service.build_system_prompt("Be brief.") == "Be brief."


def test_run_tree_nests_children():
    """Live: a parent run with two children comes back nested from runs.get_tree."""
    from app import db, runs
    parent = runs.create_run("tree test", agent_name="orchestrator")
    try:
        a = runs.create_run("child a", agent_name="data_analyst", parent_run_id=parent)
        b = runs.create_run("child b", agent_name="document_researcher", parent_run_id=parent)
        tree = runs.get_tree(parent)
        assert tree["agent_name"] == "orchestrator"
        assert [c["id"] for c in tree["children"]] == [a, b]
        assert runs.get_tree(a)["children"] == []
    finally:
        with db.connect() as conn:  # children are deleted by the foreign key cascade
            conn.execute("delete from agent_runs where id = %s", (parent,))



def test_review_snapshot_latest_wins_and_history_is_kept():
    """Live: reviews store a self-contained snapshot; the newest per claim is current."""
    from app import db, reviews, runs
    doc = {"claim": "Target is 45%", "document_name": "r.pdf", "page_number": 1, "chunk_id": "r.pdf:p1:c0"}
    sql_src = {"claim": "Oslo 46%", "table": "sample_sales", "sql": "select 1"}
    run = runs.create_run("review test")
    try:
        first = reviews.add_review(run, 1, doc, "rejected", "Ana", "first look")
        assert first["marker"] == 1 and first["claim"] == "Target is 45%" and first["source_kind"] == "document"
        assert (first["document_name"], first["page_number"], first["chunk_id"]) == ("r.pdf", 1, "r.pdf:p1:c0")
        reviews.add_review(run, 1, doc, "accepted", "Ana", "checked again")
        db_review = reviews.add_review(run, 2, sql_src, "rejected", None, None)
        assert (db_review["source_kind"], db_review["table_name"], db_review["sql_query"]) == ("database", "sample_sales", "select 1")
        latest = reviews.latest_reviews(run)
        assert latest[1]["decision"] == "accepted" and latest[1]["comment"] == "checked again"
        assert latest[2]["decision"] == "rejected"
        with db.connect() as conn:
            assert conn.execute("select count(*) n from claim_reviews where run_id = %s", (run,)).fetchone()["n"] == 3
    finally:
        with db.connect() as conn:
            conn.execute("delete from agent_runs where id = %s", (run,))   # reviews go with it (cascade)
