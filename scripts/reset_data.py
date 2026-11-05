"""Reset the data before the hackathon: drop every uploaded/demo table
(including sample_sales) and empty document_chunks. Keeps agent_runs, the
document_chunks table itself, the read-only role and all other schema.

    .venv/bin/python -m scripts.reset_data          # dry run: shows what would be deleted
    .venv/bin/python -m scripts.reset_data --yes    # actually delete

This cannot be undone. The sample_sales migration stays recorded as applied,
so `supabase db push` will not recreate the demo table.
"""
import sys

from psycopg import sql

from app import db

KEEP = {"agent_runs", "document_chunks"}  # the backend's own tables


def plan(conn) -> tuple[list[str], int]:
    tables = [r["relname"] for r in conn.execute(
        """select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'public' and c.relkind in ('r', 'p') order by c.relname""").fetchall()]
    to_drop = [t for t in tables if t not in KEEP]
    chunks = conn.execute("select count(*) as n from document_chunks").fetchone()["n"]
    return to_drop, chunks


def main() -> int:
    really = "--yes" in sys.argv
    with db.connect() as conn:  # one transaction: all or nothing
        to_drop, chunks = plan(conn)
        docs = conn.execute("select count(distinct document_name) as n from document_chunks").fetchone()["n"]
        print("Tables to drop:", ", ".join(to_drop) or "(none)")
        print(f"document_chunks: {chunks} chunks from {docs} documents to delete")
        print("Kept:", ", ".join(sorted(KEEP)), "(table), plus roles and schema")
        if not really:
            print("\nDry run - nothing was changed. Run again with --yes to delete.")
            return 0
        for table in to_drop:
            conn.execute(sql.SQL("drop table {}").format(sql.Identifier("public", table)))
        conn.execute("truncate document_chunks")
    with db.connect() as conn:
        left, chunks_left = plan(conn)
    print(f"\nDone. Remaining droppable tables: {left or 'none'}; chunks left: {chunks_left}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
