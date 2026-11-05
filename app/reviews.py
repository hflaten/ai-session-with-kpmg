"""Human review of cited sources: accept or reject each claim in a run's answer.

Every review is a new row in claim_reviews; the newest row per source is its
current decision (so changing your mind just adds a row, history is kept).
source_index is 0-based, like `index` in GET /runs/{id}/sources; marker [n] = index + 1.
"""
from app import db


def add_review(run_id: str, source_index: int, decision: str,
               reviewer: str | None = None, comment: str | None = None) -> dict:
    with db.connect() as conn:
        return conn.execute(
            """insert into claim_reviews (run_id, source_index, decision, reviewer, comment)
               values (%s, %s, %s, %s, %s)
               returning id, created_at, source_index, decision, reviewer, comment""",
            (run_id, source_index, decision, reviewer, comment),
        ).fetchone()


def latest_reviews(run_id: str) -> dict[int, dict]:
    """{source_index: newest review} for one run."""
    with db.connect() as conn:
        rows = conn.execute(
            """select distinct on (source_index) id, created_at, source_index, decision, reviewer, comment
               from claim_reviews where run_id = %s
               order by source_index, created_at desc""",
            (run_id,),
        ).fetchall()
    return {r["source_index"]: r for r in rows}
