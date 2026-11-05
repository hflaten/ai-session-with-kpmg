"""save_report: the agent's one write action. Needs human approval (step 8).

Built per run (make_save_report_tool) so each saved report records which run saved it.
"""
from app import db
from app.llm import Tool


def make_save_report_tool(run_id: str | None) -> Tool:
    def save_report(title: str, content: str) -> dict:
        with db.connect() as conn:
            row = conn.execute(
                "insert into saved_reports (run_id, title, content) values (%s, %s, %s) returning id",
                (run_id, title, content),
            ).fetchone()
        return {"saved": True, "report_id": str(row["id"])}

    return Tool(
        name="save_report",
        description=("Save a finished report or finding so the team can find it later. "
                     "A human must approve this before it runs, so only call it when the user asks "
                     "to save, store or share something."),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title"},
                "content": {"type": "string", "description": "The full report text"},
            },
            "required": ["title", "content"],
        },
        run=save_report,
        requires_approval=True,
    )
