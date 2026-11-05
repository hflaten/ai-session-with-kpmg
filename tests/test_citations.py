"""Sources: schema, citation checks, trace chunk_ids (fake LLM and fake tools, no network)."""
import pytest

from app.agent import loop
from app.agent.structured import Evidence, answer_text, check_output_schema, citation_errors, marker_errors, with_sources
from app.llm import LLMResponse, Tool, ToolCall

CHUNK = {"chunk_id": "report.pdf:p2:c1", "document_name": "report.pdf", "page_number": 2, "text": "Target 45%"}
SQL = "SELECT region FROM sample_sales"

FAKE_SEARCH = Tool("search_documents", "search", {"type": "object", "properties": {"query": {"type": "string"}}},
                   run=lambda query: {"results": [CHUNK]},
                   trace_fields=lambda out: {"chunk_ids": [r["chunk_id"] for r in out["results"]]})
FAKE_SQL = Tool("query_database", "sql", {"type": "object", "properties": {"sql": {"type": "string"}}},
                run=lambda sql: {"rows": [{"region": "Oslo"}]})

DOC_SOURCE = {"claim": "Target is 45%", "document_name": "report.pdf", "page_number": 2, "chunk_id": "report.pdf:p2:c1"}
DB_SOURCE = {"claim": "Oslo exists", "table": "sample_sales", "sql": SQL}


def evidence() -> Evidence:
    e = Evidence()
    e.add("search_documents", {"query": "q"}, {"results": [CHUNK]})
    e.add("query_database", {"sql": SQL}, {"rows": []})
    return e


# ---------- schema ----------

def test_sources_added_and_required():
    s = with_sources({"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]})
    assert s["required"] == ["x", "sources"]
    assert "sources" in s["properties"]


def test_caller_cannot_define_sources():
    with pytest.raises(ValueError):
        check_output_schema({"type": "object", "properties": {"sources": {"type": "array"}}})


# ---------- citation checks ----------

def test_valid_document_and_database_sources():
    assert citation_errors([DOC_SOURCE, DB_SOURCE], evidence()) == []


def test_sql_match_ignores_whitespace_case_and_semicolon():
    src = {**DB_SOURCE, "sql": "select region\n  from   sample_sales;"}
    assert citation_errors([src], evidence()) == []


@pytest.mark.parametrize("source, message", [
    ({**DOC_SOURCE, "chunk_id": "report.pdf:p9:c9"}, "was not returned"),
    ({**DOC_SOURCE, "page_number": 3}, "not report.pdf page 3"),
    ({**DOC_SOURCE, "document_name": "other.pdf"}, "not other.pdf"),
    ({**DB_SOURCE, "sql": "select * from sample_sales"}, "was not run"),
    ({**DB_SOURCE, "table": "agent_runs"}, "does not appear"),
    ({**DB_SOURCE, "table": "sales"}, "does not appear"),  # 'sales' is only part of 'sample_sales'
])
def test_invalid_citations_are_caught(source, message):
    errors = citation_errors([source], evidence())
    assert len(errors) == 1 and message in errors[0]


def test_failed_queries_are_not_evidence():
    e = Evidence()  # nothing added, as when a query raised an error
    assert "was not run" in citation_errors([DB_SOURCE], e)[0]


# ---------- in the loop ----------

def scripted(monkeypatch, responses):
    seen = []

    def fake(system, messages, tools, force_tool=None):
        seen.append(messages[-1])
        return responses[len(seen) - 1]

    monkeypatch.setattr(loop, "call_llm", fake)
    return seen


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def test_search_trace_has_chunk_ids_and_good_citations_pass(monkeypatch):
    scripted(monkeypatch, [
        LLMResponse("", [ToolCall("a", "search_documents", {"query": "target"}),
                         ToolCall("b", "query_database", {"sql": SQL})]),
        LLMResponse("", [ToolCall("c", "submit_answer", {"answer": "Target is 45% [1] and Oslo exists [2].", "sources": [DOC_SOURCE, DB_SOURCE]})]),
    ])
    r = loop.run_agent("sys", [FAKE_SEARCH, FAKE_SQL], "q", output_schema=SCHEMA)
    search_entry = next(e for e in r.trace if e.get("tool") == "search_documents")
    assert search_entry["chunk_ids"] == ["report.pdf:p2:c1"]
    assert r.answer["sources"] == [DOC_SOURCE, DB_SOURCE]


def test_invented_citation_is_rejected_then_fixed(monkeypatch):
    invented = {**DOC_SOURCE, "chunk_id": "report.pdf:p7:c4"}
    seen = scripted(monkeypatch, [
        LLMResponse("", [ToolCall("a", "search_documents", {"query": "target"})]),
        LLMResponse("", [ToolCall("b", "submit_answer", {"answer": "x [1]", "sources": [invented]})]),
        LLMResponse("", [ToolCall("c", "submit_answer", {"answer": "x [1]", "sources": [DOC_SOURCE]})]),
    ])
    r = loop.run_agent("sys", [FAKE_SEARCH], "q", output_schema=SCHEMA)
    assert "was not returned" in seen[2]["content"]         # model was told why
    assert [e["valid"] for e in r.trace if e["type"] == "final_answer"] == [False, True]



# ---------- inline markers ----------

def test_markers_valid():
    a = {"answer": "Target 45% [1]. Oslo 46% [2][1].", "sources": [DOC_SOURCE, DB_SOURCE]}
    assert marker_errors(a) == []


def test_marker_without_source_and_unreferenced_source():
    a = {"answer": "Target 45% [1]. Something [3].", "sources": [DOC_SOURCE, DB_SOURCE]}
    errors = marker_errors(a)
    assert any("[3] has no matching source" in e for e in errors)
    assert any("[2] are never referenced" in e for e in errors)


def test_zero_is_not_a_valid_marker():
    assert "[0] has no matching source" in marker_errors({"answer": "x [0] [1]", "sources": [DOC_SOURCE]})[0]


def test_markers_found_in_any_text_field_but_not_in_sources():
    a = {"summary": "Short [1].", "details": [{"text": "More [2]."}],
         "sources": [{**DOC_SOURCE, "claim": "[2] inside a claim does not count"}, DB_SOURCE]}
    assert "[1]" in answer_text(a) and "inside a claim" not in answer_text(a)
    assert marker_errors(a) == []


def test_no_sources_no_markers_is_fine():
    assert marker_errors({"answer": "Hello.", "sources": []}) == []


def test_missing_markers_are_sent_back_then_fixed(monkeypatch):
    seen = scripted(monkeypatch, [
        LLMResponse("", [ToolCall("a", "search_documents", {"query": "target"})]),
        LLMResponse("", [ToolCall("b", "submit_answer", {"answer": "Target is 45%.", "sources": [DOC_SOURCE]})]),
        LLMResponse("", [ToolCall("c", "submit_answer", {"answer": "Target is 45% [1].", "sources": [DOC_SOURCE]})]),
    ])
    r = loop.run_agent("sys", [FAKE_SEARCH], "q", output_schema=SCHEMA)
    assert "never referenced" in seen[2]["content"]
    assert r.answer["answer"] == "Target is 45% [1]."
