"""Human review of individual claims: accept or reject each [n] in a run's answer.

One marker = one claim = one entry in answer.sources, so a review always applies to
exactly one claim. Each review row stores a snapshot of the claim text and its
reference (document + page + chunk, table + SQL, or tool + arguments), so the audit record stands on
its own even if the run changes later.

Every review is a new row; the newest row per claim is its current decision
(changing your mind adds a row, so the history is kept).
In the database, source_index = marker - 1 (0-based); `marker` is generated from it.
"""
from psycopg.types.json import Jsonb

from app import db

COLUMNS = ("id, created_at, marker, decision, reviewer, comment, claim, source_kind, "
           "document_name, page_number, chunk_id, table_name, sql_query, tool_name, tool_arguments")


def add_review(run_id: str, marker: int, source: dict, decision: str,
               reviewer: str | None = None, comment: str | None = None) -> dict:
    """source = answer.sources[marker - 1]; its claim and reference are copied into the review."""
    kind = "document" if "chunk_id" in source else ("tool" if "tool" in source else "database")
    with db.connect() as conn:
        return conn.execute(
            f"""insert into claim_reviews
                    (run_id, source_index, decision, reviewer, comment, claim, source_kind,
                     document_name, page_number, chunk_id, table_name, sql_query, tool_name, tool_arguments)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning {COLUMNS}""",
            (run_id, marker - 1, decision, reviewer, comment, source.get("claim"),
             kind, source.get("document_name"), source.get("page_number"), source.get("chunk_id"),
             source.get("table"), source.get("sql"), source.get("tool"),
             Jsonb(source["arguments"]) if "arguments" in source else None),
        ).fetchone()


def latest_reviews(run_id: str) -> dict[int, dict]:
    """{marker: newest review} for one run."""
    with db.connect() as conn:
        rows = conn.execute(
            f"""select distinct on (source_index) {COLUMNS}
                from claim_reviews where run_id = %s
                order by source_index, created_at desc""",
            (run_id,),
        ).fetchall()
    return {r["marker"]: r for r in rows}
