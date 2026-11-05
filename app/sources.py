"""Resolve a finished run's cited sources into something a UI can show.

    GET /runs/{run_id}/sources

- document source -> the chunk's original text (from document_chunks)
- database source -> the rows, by re-running the cited SQL as agent_readonly

Only SQL stored in the run's validated answer is re-run (it passed the citation
check, so the agent really ran it). The browser never sends SQL.
"""
from app import db, runs
from app.tools.database import query_database


def _chunk_text(chunk_id: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute("select text from document_chunks where chunk_id = %s", (chunk_id,)).fetchone()
    return row["text"] if row else None


def resolve_sources(run_id: str) -> list[dict] | None:
    """None if the run doesn't exist; [] if its answer has no sources."""
    run = runs.get_run(run_id)
    if run is None:
        return None
    answer = run.get("answer")
    if not isinstance(answer, dict):
        return []

    resolved = []
    for i, s in enumerate(answer.get("sources", [])):
        if "chunk_id" in s:
            text = _chunk_text(s["chunk_id"])
            resolved.append({"index": i, "kind": "document", **s, "text": text,
                             "error": None if text is not None else "chunk no longer exists (document re-uploaded or reset)"})
        elif "sql" in s:
            try:
                result = query_database(s["sql"])
                resolved.append({"index": i, "kind": "database", **s, **result, "error": None})
            except Exception as e:
                resolved.append({"index": i, "kind": "database", **s, "columns": [], "rows": [],
                                 "error": f"{type(e).__name__}: {e}"})
    return resolved
