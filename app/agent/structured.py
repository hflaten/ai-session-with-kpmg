"""Structured final answers via a `submit_answer` tool, with checked sources.

The caller's JSON schema becomes the tool's input schema, so the model "fills
in a form" to finish. This works the same on Anthropic and OpenAI-compatible
APIs, unlike each provider's own JSON mode.

Every structured answer also gets a required `sources` list. Each entry is
a document, database or tool citation:

    {"claim": "...", "document_name": "report.pdf", "page_number": 2, "chunk_id": "report.pdf:p2:c1"}
    {"claim": "...", "table": "sales_daily", "sql": "select ..."}
    {"claim": "...", "tool": "recommend_orders", "call_id": "rc-3f9a1c",
     "row": {"sku": "SKU009", "location_id": "L04"}, "fields": ["next_order.qty", "next_order.order_date"]}

Citations are checked against what actually happened in this run (Evidence):
a chunk_id must have been returned by search_documents (with matching document
and page), and the sql must be a query_database call that succeeded, with the
table appearing in it; a tool citation must name a call_id of a citable tool
(CITABLE_TOOLS: deterministic calculations) that ran in this run. Those are hard errors.

Tool citations are then checked against that call's stored output (evidence_errors):
the row exists, the fields exist, and every number and date in the claim matches a
cited value (numbers.py: rounding and "414k" / "46%" formatting allowed). These are soft
errors: sent back to the model twice, then the answer is accepted with `warnings`.

The answer text must also cite inline: "Oslo met the target [1], Bergen missed it [2]."
[n] is the 1-based position in `sources`. Every marker must point to a source, every
source must be referenced, and each marker appears exactly once: one marker = one claim,
so each claim can be reviewed on its own. Two claims resting on the same evidence get
two entries in `sources` (marker_errors). Each claim is a single sentence or clause with
its marker right after it, and any sentence containing a number, percentage or date must
carry a marker (unmarked_fact_errors).

Invalid answers go back to the model to fix.
"""
import copy
import re

from jsonschema import Draft202012Validator

from app.agent import tool_calls
from app.agent.numbers import claim_mismatches, flatten, strip_names
from app.llm import Tool

SUBMIT_TOOL_NAME = "submit_answer"

SOURCES_SCHEMA = {
    "type": "array",
    "description": ("One entry per factual claim and the evidence behind it. A claim backed by "
                    "two chunks or queries gets two entries. Use [] only if the answer makes no factual claims. "
                    "In your answer text, put [n] right after each claim, where n is the 1-based position "
                    "of its entry here (e.g. [1], or [1][2] for a claim with two sources). Each [n] must appear "
                    "EXACTLY ONCE: if two claims rest on the same chunk or query, add a separate entry for each "
                    "claim (repeat the document/table reference) with its own claim text. Each claim is a single "
                    "sentence or clause with its marker immediately after it; never let one marker cover several "
                    "sentences. Every sentence with a number, percentage or date needs a marker."),
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
                "title": "tool source",
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "tool": {"type": "string", "description": "A calculation tool you called, e.g. recommend_orders"},
                    "call_id": {"type": "string", "description": "The call_id in that tool result, e.g. rc-3f9a1c"},
                    "row": {
                        "description": ('The row the numbers come from, by its keys, e.g. {"sku": "SKU009", '
                                        '"location_id": "L04"}; or "summary" for the totals'),
                        "anyOf": [{"type": "object", "additionalProperties": {"type": "string"}, "minProperties": 1},
                                  {"const": "summary"}],
                    },
                    "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                               "description": ("The fields holding every number and date in the claim, as dot paths, "
                                               'e.g. ["next_order.qty", "without_recommendation.lost_sales_nok"]')},
                },
                "required": ["claim", "tool", "call_id", "row", "fields"],
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
                     "claim: chunks you got from search_documents (chunk_id, document_name, page_number), "
                     "the exact SQL you ran with query_database (table, sql), and/or a calculation tool you "
                     "called such as recommend_orders: give its call_id (from the tool result), the row (its keys, "
                     "or \"summary\" for totals) and the fields that hold every number and date in the claim, e.g. "
                     "{\"claim\": \"Order 40 units of Røkt laks 300 g for Høyden Nettbutikk by 6 Nov\", "
                     "\"tool\": \"recommend_orders\", \"call_id\": \"rc-3f9a1c\", "
                     "\"row\": {\"sku\": \"SKU009\", \"location_id\": \"L04\"}, "
                     "\"fields\": [\"next_order.qty\", \"next_order.order_date\"]}. Every number in the claim must "
                     "match a cited field (rounding like 414k or 46% is fine). In the answer text, "
                     "mark each claim inline with [n] = its 1-based position in `sources`, right after the "
                     "claim, e.g. \"Oslo met the target [1], Bergen missed it [2].\" Every source must be marked, "
                     "and each marker exactly once: one marker per claim. If two claims rest on the same evidence, "
                     "give each claim its own entry in `sources` (repeating the reference), so each claim can be "
                     "reviewed separately. Each claim is a SINGLE sentence or clause with its marker immediately "
                     "after it; never let one marker cover several sentences (write \"The target is 45% [1]. It "
                     "is defined as revenue minus cost over revenue [2].\", not one marker after both). Context "
                     "sentences without facts need no marker, but every sentence that contains a number, "
                     "percentage or date must carry its own marker."),
        input_schema=schema,
    )


def validation_errors(schema: dict, answer: dict) -> list[str]:
    """Empty list = valid. Otherwise readable messages to send back to the model."""
    validator = Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in validator.iter_errors(answer)]


# ---------- citation checking ----------

CITABLE_TOOLS = {"recommend_orders"}   # deterministic tools whose results can back a claim


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
        self.tool_calls: dict[str, str] = {}   # call_id -> tool name, for citable tools (outputs: tool_calls.recall)
        self.names: set[str] = set()           # names with digits seen in outputs ("Røkt laks 300 g")

    def add(self, tool_name: str, args: dict, output, call_id: str | None = None) -> None:
        self.names |= names_in(output)
        if tool_name == "search_documents":
            for r in output["results"]:
                self.chunks[r["chunk_id"]] = {"document_name": r["document_name"], "page_number": r["page_number"]}
        elif tool_name == "query_database":
            self.sql[normalize_sql(args["sql"])] = args["sql"]
        elif tool_name in CITABLE_TOOLS and call_id:
            self.tool_calls[call_id] = tool_name
        elif isinstance(output, dict) and "chunks_found" in output:  # a specialist's report
            for c in output["chunks_found"]:
                self.chunks[c["chunk_id"]] = {"document_name": c["document_name"], "page_number": c["page_number"]}
            for sql in output["sql_run"]:
                self.sql[normalize_sql(sql)] = sql
            for t in output.get("tools_run", []):
                self.tool_calls[t["call_id"]] = t["tool"]
                stored = tool_calls.recall(t["call_id"])
                if stored:
                    self.names |= names_in(stored["output"])

    def chunk_list(self) -> list[dict]:
        return [{"chunk_id": cid, **ref} for cid, ref in self.chunks.items()]

    def sql_list(self) -> list[str]:
        return list(self.sql.values())

    def tool_list(self) -> list[dict]:
        """[{tool, call_id, arguments}] - what a specialist reports to the orchestrator."""
        out = []
        for call_id, name in self.tool_calls.items():
            stored = tool_calls.recall(call_id)
            out.append({"tool": name, "call_id": call_id, "arguments": stored["arguments"] if stored else {}})
        return out


NAME_WITH_DIGITS = re.compile(r"^(?=.*[A-Za-zÆØÅæøå]{2})(?=.*\d)(?!\d{4}-\d{2}-\d{2}).*\s.*$")


def names_in(output) -> set[str]:
    """Product/place names that contain digits, e.g. "Røkt laks 300 g", from any tool output.
    Used so the digits in names are not taken for facts or numbers."""
    return {v for v in flatten(output) if isinstance(v, str) and len(v) <= 60 and NAME_WITH_DIGITS.match(v)}


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
        elif "tool" in s:
            ran = evidence.tool_calls.get(s["call_id"])
            if ran is None:
                errors.append(f"{where}: call_id '{s['call_id']}' is not a {s['tool']} call from this run; "
                              f"use a call_id shown in a tool result (calls: {', '.join(evidence.tool_calls) or 'none'})")
            elif ran != s["tool"]:
                errors.append(f"{where}: call_id '{s['call_id']}' is a {ran} call, not {s['tool']}")
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
        return "\n".join(answer_text(v) for k, v in answer.items()
                         if k not in ("sources", "warnings", "unverified_numbers"))
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



# ---------- facts without a marker ----------

SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")                 # "41.83" is not a sentence end
NORWEGIAN_DATE = re.compile(r"\b(\d{1,2})\.(?=\s+(?:jan|feb|mar|apr|mai|jun|jul|aug|sep|okt|nov|des))", re.IGNORECASE)


def _no_date_dots(text: str) -> str:
    """"6. nov" -> "6  nov", so the dot in a Norwegian date is not read as the end of a sentence.
    (Same length as the input, so character positions stay valid.)"""
    return NORWEGIAN_DATE.sub(r"\1 ", text)
LIST_PREFIX = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")          # "- " or "1. " list bullets


NUMBER_SPAN = re.compile(r"\d+(?:[.,  ]\d+)*\s?%?")


def text_fields(answer, path: str = "") -> list[tuple[str, str]]:
    """[(path, text)] for every text in the answer except sources/warnings: [("answer", "...")]"""
    if isinstance(answer, str):
        return [(path, answer)]
    if isinstance(answer, dict):
        return [x for k, v in answer.items() if k not in ("sources", "warnings", "unverified_numbers")
                for x in text_fields(v, f"{path}.{k}" if path else k)]
    if isinstance(answer, list):
        return [x for n, v in enumerate(answer) for x in text_fields(v, f"{path}[{n}]")]
    return []


def unmarked_numbers(answer: dict, names=()) -> list[dict]:
    """Sentences that contain a number, percentage or date but no marker, with exact positions:
    [{"field": "answer", "start": 120, "end": 168, "text": "...", "numbers": [{"text": "56", "start": 131, "end": 133}]}]
    start/end index into that field's text. Headings and list numbering are skipped; digits inside
    `names` (e.g. "Røkt laks 300 g") don't count; spelled-out numbers ("four") are not detected."""
    found = []
    for field, text in text_fields(answer):
        masked = _no_date_dots(strip_names(text, names))        # same length as text
        for line in re.finditer(r"[^\n]+", masked):
            if line.group(0).lstrip().startswith("#"):
                continue
            prefix = LIST_PREFIX.match(line.group(0))
            begin = line.start() + (prefix.end() if prefix else 0)
            cuts = [begin] + [line.start() + m.end() for m in SENTENCE_END.finditer(line.group(0))
                              if line.start() + m.end() > begin] + [line.end()]
            for a, b in zip(cuts, cuts[1:]):
                sentence = masked[a:b]
                if MARKER.search(sentence):
                    continue
                spans = [(a + m.start(), a + m.start() + len(m.group(0).rstrip())) for m in NUMBER_SPAN.finditer(sentence)]
                numbers = [{"text": text[x:y], "start": x, "end": y} for x, y in spans]
                if numbers:
                    lead = len(sentence) - len(sentence.lstrip())
                    start, end = a + lead, a + len(sentence.rstrip())
                    found.append({"field": field, "start": start, "end": end, "text": text[start:end],
                                  "numbers": numbers})
    return found


def unmarked_fact_errors(answer: dict, names=()) -> list[str]:
    """A sentence that contains a number, percentage or date must carry a marker.

    Catches claims whose marker sits after a later sentence ("Target is 45%. It is
    defined as ... [1]."), so every fact gets its own reviewable marker.
    """
    return describe_unmarked(unmarked_numbers(answer, names))


def describe_unmarked(found: list[dict]) -> list[str]:
    errors = []
    for u in found:
        shown = u["text"] if len(u["text"]) <= 90 else u["text"][:87] + "..."
        errors.append(f'sentence "{shown}" contains a number, percentage or date but has no marker. '
                      "Make it its own claim: add an entry to sources and put [n] right after it, "
                      "or remove the number if you have no evidence for it")
    return errors


# ---------- tool evidence: row, fields, numbers (soft checks) ----------

SUMMARY = "summary"


def find_row(output, row) -> dict | None:
    """The row a tool source points at: "summary" = the top level (totals etc.); otherwise the
    first item, in any list of rows in the output, whose keys match (e.g. sku + location_id)."""
    if not isinstance(output, dict):
        return None
    if row == SUMMARY:
        return {k: v for k, v in output.items() if not (isinstance(v, list) and v and isinstance(v[0], dict))}
    for value in output.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and all(str(item.get(k)) == str(v) for k, v in row.items()):
                    return item
    return None


MISSING = object()


def get_field(row: dict, path: str):
    """'next_order.qty' -> row["next_order"]["qty"]; MISSING if it isn't there."""
    value = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def cited_values(source: dict) -> tuple[dict | None, dict, list[str]]:
    """(row, {field: value}, problems) for one tool source, from the call's stored output."""
    stored = tool_calls.recall(source["call_id"])
    if stored is None:
        return None, {}, [f"call {source['call_id']} has no stored output"]
    row = find_row(stored["output"], source["row"])
    if row is None:
        return None, {}, [f"row {source['row']} is not in the output of {source['call_id']}"]
    values, problems = {}, []
    for f in source["fields"]:
        v = get_field(row, f)
        if v is MISSING:
            problems.append(f"field '{f}' does not exist in that row (it has: {', '.join(row)})")
        else:
            values[f] = v
    return row, values, problems


def claim_spans(text: str) -> list[tuple[str, list[int]]]:
    """[(claim text, [markers])] - the text before each marker group, back to the sentence start.
    The same split as splitClaims() in the frontend (LOVABLE.md)."""
    text = _no_date_dots(text)
    spans, last = [], 0
    for m in re.finditer(r"(?:\[\d+\])+", text):
        before = text[last:m.start()]
        starts = [b.end() for b in re.finditer(r"[.!?](?=\s)\s*|\n", before)]
        spans.append((before[starts[-1] if starts else 0:], [int(n) for n in re.findall(r"\d+", m.group(0))]))
        last = m.end()
    return spans


def evidence_errors(answer: dict, evidence: Evidence) -> list[str]:
    """Soft checks for tool sources. Hard checks (call_id ran) are in citation_errors."""
    errors = []
    sources = answer.get("sources", [])
    resolved: dict[int, tuple[dict | None, dict]] = {}
    for i, s in enumerate(sources):
        if "tool" not in s or s["call_id"] not in evidence.tool_calls:
            continue
        row, values, problems = cited_values(s)
        resolved[i + 1] = (row, values)
        errors += [f"sources/{i}: {p}" for p in problems]
        if row is not None:
            bad = claim_mismatches(s["claim"], list(values.values()), evidence.names)
            if bad:
                errors.append(f"sources/{i}: the claim's {', '.join(bad)} do not match the cited fields "
                              f"{values}; cite the field that holds it, or fix the number")

    # The claim as written in the answer text (what the reviewer sees highlighted) must match too
    for text, markers in claim_spans(answer_text(answer)):
        if not markers or not all(m in resolved for m in markers):
            continue      # not only tool sources (SQL / documents are checked differently)
        values = [v for m in markers for v in resolved[m][1].values()]
        bad = claim_mismatches(text, values, evidence.names)
        if bad:
            shown = text.strip()[:90]
            errors.append(f'claim "{shown}" {"".join(f"[{m}]" for m in markers)}: {", ".join(bad)} '
                          "not found in the cited fields; cite the field that holds it, or fix the number")
    return errors
