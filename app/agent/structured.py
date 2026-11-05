"""Structured final answers via a `submit_answer` tool, with checked sources.

The caller's JSON schema becomes the tool's input schema, so the model "fills
in a form" to finish. This works the same on Anthropic and OpenAI-compatible
APIs, unlike each provider's own JSON mode.

Every structured answer also gets a required `sources` list. Each entry is
either a document citation or a database citation:

    {"claim": "...", "document_name": "report.pdf", "page_number": 2, "chunk_id": "report.pdf:p2:c1"}
    {"claim": "...", "table": "sample_sales", "sql": "select ..."}

Citations are checked against what actually happened in this run (Evidence):
a chunk_id must have been returned by search_documents (with matching document
and page), and the sql must be a query_database call that succeeded, with the
table appearing in it.

The answer text must also cite inline: "Oslo met the target [1], Bergen missed it [2]."
[n] is the 1-based position in `sources`. Every marker must point to a source, every
source must be referenced, and each marker appears exactly once: one marker = one claim,
so each claim can be reviewed on its own. Two claims resting on the same evidence get
two entries in `sources` (marker_errors).

Invalid answers go back to the model to fix.
"""
import copy
import re

from jsonschema import Draft202012Validator

from app.llm import Tool

SUBMIT_TOOL_NAME = "submit_answer"

SOURCES_SCHEMA = {
    "type": "array",
    "description": ("One entry per factual claim and the evidence behind it. A claim backed by "
                    "two chunks or queries gets two entries. Use [] only if the answer makes no factual claims. "
                    "In your answer text, put [n] right after each claim, where n is the 1-based position "
                    "of its entry here (e.g. [1], or [1][2] for a claim with two sources). Each [n] must appear "
                    "EXACTLY ONCE: if two claims rest on the same chunk or query, add a separate entry for each "
                    "claim (repeat the document/table reference) with its own claim text."),
    "items": {
        "anyOf": [
            {
                "title": "document source",
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "document_name": {"type": "string"},
                    "page_number": {"type": "integer"},
                    "chunk_id": {"type": "string"},
                },
                "required": ["claim", "document_name", "page_number", "chunk_id"],
                "additionalProperties": False,
            },
            {
                "title": "database source",
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "table": {"type": "string"},
                    "sql": {"type": "string", "description": "The exact SQL you ran with query_database"},
                },
                "required": ["claim", "table", "sql"],
                "additionalProperties": False,
            },
        ]
    },
}


def check_output_schema(schema: dict) -> None:
    """Raise ValueError if the schema can't be used as a tool input schema."""
    if schema.get("type") != "object":
        raise ValueError("output_schema must have \"type\": \"object\" at the top level")
    if "sources" in schema.get("properties", {}):
        raise ValueError("'sources' is added automatically; remove it from output_schema")
    Draft202012Validator.check_schema(schema)  # raises SchemaError if malformed


def with_sources(schema: dict) -> dict:
    """Copy of the caller's schema with the required `sources` field added."""
    full = copy.deepcopy(schema)
    full.setdefault("properties", {})["sources"] = SOURCES_SCHEMA
    full["required"] = [*full.get("required", []), "sources"]
    return full


def make_submit_tool(schema: dict) -> Tool:
    return Tool(
        name=SUBMIT_TOOL_NAME,
        description=("Submit your final answer. Call this exactly once, when you are done, with arguments "
                     "that match the schema. Do not answer in plain text. In `sources`, cite every factual "
                     "claim: chunks you got from search_documents (chunk_id, document_name, page_number) "
                     "and/or the exact SQL you ran with query_database (table, sql). In the answer text, "
                     "mark each claim inline with [n] = its 1-based position in `sources`, right after the "
                     "claim, e.g. \"Oslo met the target [1], Bergen missed it [2].\" Every source must be marked, "
                     "and each marker exactly once: one marker per claim. If two claims rest on the same evidence, "
                     "give each claim its own entry in `sources` (repeating the reference), so each claim can be "
                     "reviewed separately."),
        input_schema=schema,
    )


def validation_errors(schema: dict, answer: dict) -> list[str]:
    """Empty list = valid. Otherwise readable messages to send back to the model."""
    validator = Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in validator.iter_errors(answer)]


# ---------- citation checking ----------

def normalize_sql(sql: str) -> str:
    """Compare queries ignoring whitespace, case and a trailing semicolon."""
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").strip().lower()


class Evidence:
    """What the agent actually saw in this run: chunks found and SQL that ran.

    For the orchestrator, this also includes what its specialists found
    (delegation tools return `chunks_found` and `sql_run`).
    """

    def __init__(self) -> None:
        self.chunks: dict[str, dict] = {}   # chunk_id -> {document_name, page_number}
        self.sql: dict[str, str] = {}       # normalized SQL -> SQL as written, for successful queries

    def add(self, tool_name: str, args: dict, output) -> None:
        if tool_name == "search_documents":
            for r in output["results"]:
                self.chunks[r["chunk_id"]] = {"document_name": r["document_name"], "page_number": r["page_number"]}
        elif tool_name == "query_database":
            self.sql[normalize_sql(args["sql"])] = args["sql"]
        elif isinstance(output, dict) and "chunks_found" in output:  # a specialist's report
            for c in output["chunks_found"]:
                self.chunks[c["chunk_id"]] = {"document_name": c["document_name"], "page_number": c["page_number"]}
            for sql in output["sql_run"]:
                self.sql[normalize_sql(sql)] = sql

    def chunk_list(self) -> list[dict]:
        return [{"chunk_id": cid, **ref} for cid, ref in self.chunks.items()]

    def sql_list(self) -> list[str]:
        return list(self.sql.values())


def _table_in_sql(table: str, sql: str) -> bool:
    name = re.escape(table.removeprefix("public."))
    return re.search(rf'(?<![\w$]){name}(?![\w$])', sql, re.IGNORECASE) is not None


def citation_errors(sources: list[dict], evidence: Evidence) -> list[str]:
    errors = []
    for i, s in enumerate(sources):
        where = f"sources/{i}"
        if "chunk_id" in s:
            seen = evidence.chunks.get(s["chunk_id"])
            if seen is None:
                errors.append(f"{where}: chunk_id '{s['chunk_id']}' was not returned by search_documents in this run")
            elif (seen["document_name"], seen["page_number"]) != (s["document_name"], s["page_number"]):
                errors.append(f"{where}: chunk '{s['chunk_id']}' is from {seen['document_name']} "
                              f"page {seen['page_number']}, not {s['document_name']} page {s['page_number']}")
        elif "sql" in s:
            if normalize_sql(s["sql"]) not in evidence.sql:
                errors.append(f"{where}: this sql was not run successfully with query_database in this run; "
                              "copy the exact query you ran")
            elif not _table_in_sql(s["table"], s["sql"]):
                errors.append(f"{where}: table '{s['table']}' does not appear in the sql")
    return errors


# ---------- inline markers ----------

MARKER = re.compile(r"\[(\d+)\]")


def answer_text(answer) -> str:
    """All text in the answer except the sources list (the caller's schema decides the fields)."""
    if isinstance(answer, str):
        return answer
    if isinstance(answer, dict):
        return "\n".join(answer_text(v) for k, v in answer.items() if k != "sources")
    if isinstance(answer, list):
        return "\n".join(answer_text(v) for v in answer)
    return ""


def marker_errors(answer: dict) -> list[str]:
    """Every [n] must point to a source, every source must be referenced, each [n] exactly once."""
    n_sources = len(answer.get("sources", []))
    found = [int(m) for m in MARKER.findall(answer_text(answer))]
    used = set(found)
    errors = [f"marker [{m}] has no matching source (there are {n_sources} sources; markers are 1-based)"
              for m in sorted(used) if not 1 <= m <= n_sources]
    repeated = sorted(m for m in used if found.count(m) > 1)
    if repeated:
        errors.append(f"markers {', '.join(f'[{m}]' for m in repeated)} are used more than once. Each marker must "
                      "appear exactly once (one marker = one claim). For each extra claim, add a new entry to "
                      "sources with its own claim text (repeating the same document/table reference) and use "
                      "its new number")
    missing = [i for i in range(1, n_sources + 1) if i not in used]
    if missing:
        errors.append(f"sources {', '.join(f'[{i}]' for i in missing)} are never referenced in the answer text; "
                      "put the marker right after the claim it supports")
    return errors
