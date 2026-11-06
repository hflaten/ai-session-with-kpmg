"""Reset the data before the hackathon.

Always: drop every uploaded/demo table (including sample_sales), empty
document_chunks, saved_reports, claim_reviews and proposed_orders. With --include-runs: also empty
agent_runs (the run log). The tables themselves, the read-only role and all
other schema are kept.

    .venv/bin/python -m scripts.reset_data                          # dry run: shows what would be deleted
    .venv/bin/python -m scripts.reset_data --yes                    # actually delete
    .venv/bin/python -m scripts.reset_data --include-runs           # dry run, including the run log
    .venv/bin/python -m scripts.reset_data --include-runs --yes     # delete everything, including runs

This cannot be undone. The sample_sales migration stays recorded as applied,
so `supabase db push` will not recreate the demo table.
"""
import sys

from psycopg import sql

from app import db

KEEP = {"agent_runs", "document_chunks", "saved_reports", "claim_reviews", "proposed_orders"}  # the backend's own tables (emptied, not dropped)


def count(conn, table: str) -> int:
    return conn.execute(sql.SQL("select count(*) as n from {}").format(sql.Identifier("public", table))).fetchone()["n"]


def tables_to_drop(conn) -> list[str]:
    tables = [r["relname"] for r in conn.execute(
        """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'public' and c.relkind in ('r', 'p') order by c.relname""").fetchall()]
    return [t for t in tables if t not in KEEP]


def main() -> int:
    really = "--yes" in sys.argv
    include_runs = "--include-runs" in sys.argv
    to_empty = ["document_chunks", "saved_reports", "claim_reviews", "proposed_orders"] + (["agent_runs"] if include_runs else [])

    with db.connect() as conn:  # one transaction: all or nothing
        to_drop = tables_to_drop(conn)
        print("Tables to drop:", ", ".join(to_drop) or "(none)")
        for table in to_empty:
            print(f"Table to empty: {table} ({count(conn, table)} rows)")
        kept = ["agent_runs (rows kept; add --include-runs to empty it)"] if not include_runs else []
        print("Kept:", ", ".join(kept + ["all table definitions, roles and schema"]))
        if not really:
            print("\nDry run - nothing was changed. Run again with --yes to delete.")
            return 0

        for table in to_drop:
            conn.execute(sql.SQL("drop table {}").format(sql.Identifier("public", table)))
        # One truncate for all of them: saved_reports references agent_runs
        conn.execute(sql.SQL("truncate {}").format(
            sql.SQL(", ").join(sql.Identifier("public", t) for t in to_empty)))

    with db.connect() as conn:
        left = tables_to_drop(conn)
        counts = {t: count(conn, t) for t in to_empty}
    print(f"\nDone. Uploaded/demo tables left: {left or 'none'}; rows left: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
