"""Resolve a finished run's cited sources into something a UI can show.

    GET /runs/{run_id}/sources

- document source -> the chunk's original text (from document_chunks)
- database source -> the rows, by re-running the cited SQL as agent_readonly
- tool source     -> the cited row from the call's stored output (tool_calls table): product and
                     location names, the cited fields with their values, and the row's key inputs ("why")

Only SQL stored in the run's validated answer is re-run (it passed the citation
check, so the agent really ran it). The browser never sends SQL.

Each source also gets its inline `marker` ([n] = index + 1) and its latest human `review`.
"""
from app import db, reviews, runs
from app.agent import tool_calls
from app.agent.numbers import explain
from app.agent.structured import answer_text, cited_values, claim_spans, names_in
from app.tools.database import query_database

WHY_KEYS = ("forecast_basis", "trend", "last_8_weeks_units", "same_8_weeks_last_year_units", "forecast_daily_avg",
            "lead_time_days", "lead_time_sd_days", "on_hand", "on_order", "shelf_life_days", "planned_promotions")


def resolve_tool_source(s: dict, answer_claim: str | None = None) -> dict:
    """{kind: "tool", ..., arguments, row, product, location, cited_fields: [{field, value, matches}],
    number_matches, why, error}. answer_claim = the claim as written in the answer text (before its [n])."""
    stored = tool_calls.recall(s["call_id"])
    row, values, problems = cited_values(s)
    base = {"kind": "tool", **s, "arguments": stored["arguments"] if stored else None}
    if row is None:
        return {**base, "row_data": None, "product": None, "location": None, "cited_fields": [], "why": None,
                "error": "; ".join(problems)}
    why = row.get("why") or {}
    # How each number/date in the claim was matched to the cited values (exact, rounded, percent, ...)
    names = names_in(stored["output"])
    number_matches = []
    for where, text in (("answer", answer_claim), ("source", s["claim"])):
        if text:
            matches, unmatched = explain(text, values, names)
            number_matches += [{**m, "in": where} for m in matches]
            number_matches += [{"text": u.split('"')[1], "match_type": "unmatched", "in": where} for u in unmatched]

    def matches_for(field):
        return [{"text": m["text"], "match_type": m["match_type"], "in": m["in"]} for m in number_matches
                if m.get("field", "").split("[")[0] == field
                or any(f.split("[")[0] == field for f in m.get("fields", []))]

    return {**base,
            "row_data": row,                                   # the whole row, so the UI can show it as a sheet
            "product": row.get("product"), "location": row.get("location"),
            "cited_fields": [{"field": f, "value": v, "matches": matches_for(f)} for f, v in values.items()],
            "number_matches": number_matches,
            "why": {k: why[k] for k in WHY_KEYS if k in why} or None,
            "method": stored["output"].get("method") if s["row"] == "summary" else None,
            "error": "; ".join(problems) or None}


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

    latest = reviews.latest_reviews(run_id)
    spans = {}                                   # marker -> the claim as written in the answer text
    for text, markers in claim_spans(answer_text(answer)):
        for m in markers:
            spans[m] = text.strip()
    resolved = []
    for i, s in enumerate(answer.get("sources", [])):
        base = {"index": i, "marker": i + 1, "review": latest.get(i + 1)}
        if "chunk_id" in s:
            text = _chunk_text(s["chunk_id"])
            resolved.append({**base, "kind": "document", **s, "text": text,
                             "error": None if text is not None else "chunk no longer exists (document re-uploaded or reset)"})
        elif "tool" in s:
            resolved.append({**base, **resolve_tool_source(s, spans.get(i + 1))})
        elif "sql" in s:
            try:
                result = query_database(s["sql"])
                resolved.append({**base, "kind": "database", **s, **result, "error": None})
            except Exception as e:
                resolved.append({**base, "kind": "database", **s, "columns": [], "rows": [],
                                 "error": f"{type(e).__name__}: {e}"})
    return resolved


def review_summary(resolved: list[dict]) -> dict:
    """{total, accepted, rejected, unreviewed} for a progress bar ("3 of 7 claims verified")."""
    decisions = [s["review"]["decision"] if s["review"] else None for s in resolved]
    return {"total": len(decisions), "accepted": decisions.count("accepted"),
            "rejected": decisions.count("rejected"), "unreviewed": decisions.count(None)}


def answer_sources(run_id: str) -> list[dict] | None:
    """The sources list of a run's structured answer ([] for text answers); None if no such run."""
    run = runs.get_run(run_id)
    if run is None:
        return None
    answer = run.get("answer")
    return answer.get("sources", []) if isinstance(answer, dict) else []
