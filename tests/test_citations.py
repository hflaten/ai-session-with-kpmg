"""Sources: schema, citation checks, trace chunk_ids (fake LLM and fake tools, no network)."""
import pytest

from app.agent import loop
from app.agent.structured import (Evidence, answer_text, check_output_schema, citation_errors, marker_errors,
                                  unmarked_fact_errors, with_sources)
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
    a = {"answer": "Target 45% [1]. Oslo 46% [2].", "sources": [DOC_SOURCE, DB_SOURCE]}
    assert marker_errors(a) == []


def test_one_claim_may_have_two_sources():
    a = {"answer": "Bergen missed the target [1][2].", "sources": [DOC_SOURCE, DB_SOURCE]}
    assert marker_errors(a) == []


def test_reused_marker_is_rejected():
    a = {"answer": "Target 45% [1]. Recovery plans due in May [1].", "sources": [DOC_SOURCE]}
    errors = marker_errors(a)
    assert len(errors) == 1 and "[1] are used more than once" in errors[0] and "add a new entry" in errors[0]


def test_same_evidence_two_claims_two_entries_is_fine():
    second = {**DOC_SOURCE, "claim": "Recovery plans are due in May"}
    a = {"answer": "Target 45% [1]. Recovery plans due in May [2].", "sources": [DOC_SOURCE, second]}
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



def test_reused_marker_is_sent_back_then_fixed(monkeypatch):
    second = {**DOC_SOURCE, "claim": "Recovery plans are due in May"}
    seen = scripted(monkeypatch, [
        LLMResponse("", [ToolCall("a", "search_documents", {"query": "target"})]),
        LLMResponse("", [ToolCall("b", "submit_answer", {"answer": "45% [1]. May [1].", "sources": [DOC_SOURCE]})]),
        LLMResponse("", [ToolCall("c", "submit_answer", {"answer": "45% [1]. May [2].", "sources": [DOC_SOURCE, second]})]),
    ])
    r = loop.run_agent("sys", [FAKE_SEARCH], "q", output_schema=SCHEMA)
    assert "used more than once" in seen[2]["content"]
    assert [s["claim"] for s in r.answer["sources"]] == [DOC_SOURCE["claim"], "Recovery plans are due in May"]



# ---------- facts must carry their own marker ----------

LIVE_ANSWER = ("Management set a target gross margin of at least 45 percent for every regional office for Q1 2026. "
               "Gross margin is revenue minus direct delivery cost, divided by revenue [1]. Offices below the target "
               "must present a recovery plan to the board in May 2026 [2].")


def test_the_live_multi_sentence_claim_is_rejected():
    """The real answer where [1] sat after the definition, leaving the 45% sentence unmarked."""
    errors = unmarked_fact_errors({"answer": LIVE_ANSWER, "sources": [DOC_SOURCE, DOC_SOURCE]})
    assert len(errors) == 1
    assert "Management set a target gross margin of at least 45 percent" in errors[0]
    assert "Make it its own claim" in errors[0]


@pytest.mark.parametrize("text", [
    "The target is 45% [1]. It is defined as revenue minus cost over revenue [2].",
    "Some context without facts. The target is 45% [1].",                 # context needs no marker
    "Margin was 41.83 percent [1].",                                      # decimal is not a sentence end
    "## Q1 2026 results\nOslo met the target [1].",                       # headings are skipped
    "1. Oslo met it [1]\n2. Bergen missed it [2]",                         # list numbers don't count
    "Bergen missed it at 41.8% [1], and Trondheim at 38.9% [2].",          # two clauses, one sentence
])
def test_marked_or_fact_free_sentences_pass(text):
    assert unmarked_fact_errors({"answer": text, "sources": []}) == []


@pytest.mark.parametrize("text, flagged", [
    ("Oslo met the target [1]. Revenue was NOK 1.3m in Q1.", "Revenue was NOK 1.3m in Q1."),
    ("The board meets on 20 May 2026.\nOslo met it [1].", "The board meets on 20 May 2026."),
    ("- Bergen: 41.8%\n- Oslo [1]", "Bergen: 41.8%"),
])
def test_unmarked_numbers_dates_percentages_are_rejected(text, flagged):
    errors = unmarked_fact_errors({"answer": text, "sources": []})
    assert len(errors) == 1 and flagged in errors[0]


def test_unmarked_fact_is_sent_back_then_fixed(monkeypatch):
    second = {**DOC_SOURCE, "claim": "Gross margin definition"}
    seen = scripted(monkeypatch, [
        LLMResponse("", [ToolCall("a", "search_documents", {"query": "target"})]),
        LLMResponse("", [ToolCall("b", "submit_answer", {
            "answer": "The target is 45%. Margin is revenue minus cost over revenue [1].", "sources": [DOC_SOURCE]})]),
        LLMResponse("", [ToolCall("c", "submit_answer", {
            "answer": "The target is 45% [1]. Margin is revenue minus cost over revenue [2].", "sources": [DOC_SOURCE, second]})]),
    ])
    r = loop.run_agent("sys", [FAKE_SEARCH], "q", output_schema=SCHEMA)
    assert "The target is 45%." in seen[2]["content"] and "has no marker" in seen[2]["content"]
    assert r.answer["answer"].startswith("The target is 45% [1].")


# ---------- tool sources: call_id + row + fields, numbers checked ----------

from app.agent import tool_calls
from app.agent.structured import evidence_errors

PLAN_OUTPUT = {
    "call_id": "rc-aaaaaa", "today": "2026-11-06", "horizon_days": 56,
    "totals": {"without_recommendation": {"lost_sales_nok": 414493}, "with_recommendation": {"lost_sales_nok": 9745}},
    "rows": [{"sku": "SKU009", "location_id": "L04", "product": "Røkt laks 300 g", "location": "Høyden Nettbutikk",
              "next_order": {"order_date": "2026-11-06", "arrival_date": "2026-11-08", "qty": 40},
              "stockout_risk_date": "2026-11-11", "why": {"trend": 1.091}}],
}
ROW = {"sku": "SKU009", "location_id": "L04"}


def tool_source(claim, fields, row=ROW, call_id="rc-aaaaaa"):
    return {"claim": claim, "tool": "recommend_orders", "call_id": call_id, "row": row, "fields": fields}


def plan_evidence() -> Evidence:
    tool_calls.remember("rc-aaaaaa", "recommend_orders", {}, PLAN_OUTPUT)
    e = Evidence()
    e.add("recommend_orders", {}, PLAN_OUTPUT, call_id="rc-aaaaaa")
    return e


def answer(text, *sources):
    return {"answer": text, "sources": list(sources)}


def test_good_tool_citation_passes_all_checks():
    s = tool_source("Order 40 units of Røkt laks 300 g by 6 Nov", ["next_order.qty", "next_order.order_date"])
    a = answer("Order 40 units of Røkt laks 300 g for Høyden Nettbutikk today, 6 Nov [1].", s)
    e = plan_evidence()
    assert citation_errors(a["sources"], e) == [] and evidence_errors(a, e) == []
    assert unmarked_fact_errors(answer("**Røkt laks 300 g**"), e.names) == []   # digits in a name are not facts


def test_summary_row_rounding_and_differences():
    s = tool_source("Lost sales fall from NOK 414k to NOK 9.7k, saving about NOK 405k",
                    ["totals.without_recommendation.lost_sales_nok", "totals.with_recommendation.lost_sales_nok"],
                    row="summary")
    assert evidence_errors(answer("Lost sales fall from 414,493 NOK to NOK 10k [1].", s), plan_evidence()) == []


def test_unknown_call_id_is_a_hard_error():
    s = tool_source("Order 40", ["next_order.qty"], call_id="rc-ffffff")
    assert "is not a recommend_orders call from this run" in citation_errors([s], plan_evidence())[0]


@pytest.mark.parametrize("source, text, message", [
    (tool_source("Order 40", ["next_order.qty"], row={"sku": "SKU001", "location_id": "L04"}), "Order 40 [1].",
     "is not in the output"),
    (tool_source("Order 40", ["next_order.quantity"]), "Order 40 [1].", "field 'next_order.quantity' does not exist"),
    (tool_source("Order 400 units", ["next_order.qty"]), "Order 40 [1].", 'number "400"'),       # wrong in sources
    (tool_source("Order 40 units", ["next_order.qty"]), "Order 400 units [1].", 'number "400"'),  # wrong in the text
    (tool_source("Arrives 9 Nov", ["next_order.arrival_date"]), "Arrives 8 Nov [1].", 'date "9 Nov"'),
    (tool_source("Order 40, demand up 9%", ["next_order.qty"]), "Order 40 [1].", 'number "9%"'),   # trend not cited
])
def test_wrong_rows_fields_numbers_and_dates_are_caught(source, text, message):
    errors = evidence_errors(answer(text, source), plan_evidence())
    assert errors and any(message in e for e in errors), errors


def test_percent_change_from_a_factor():
    s = tool_source("Demand is 9% above last year", ["why.trend"])
    assert evidence_errors(answer("Demand is 9% above last year [1].", s), plan_evidence()) == []


def test_tool_source_passes_the_schema():
    from app.agent.structured import validation_errors
    schema = with_sources({"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]})
    good = tool_source("Order 40", ["next_order.qty"])
    assert validation_errors(schema, {"answer": "x [1]", "sources": [good]}) == []
    assert validation_errors(schema, {"answer": "x [1]", "sources": [tool_source("x", [], row="summary")]})  # no fields


def test_specialist_tool_calls_are_evidence_for_the_orchestrator():
    tool_calls.remember("rc-bbbbbb", "recommend_orders", {"sku": "SKU009"}, {**PLAN_OUTPUT, "call_id": "rc-bbbbbb"})
    e = Evidence()
    e.add("ask_inventory_planner", {"question": "q"}, {"answer": "a", "chunks_found": [], "sql_run": [],
          "tools_run": [{"tool": "recommend_orders", "call_id": "rc-bbbbbb", "arguments": {"sku": "SKU009"}}]})
    s = tool_source("Order 40 units of Røkt laks 300 g", ["next_order.qty"], call_id="rc-bbbbbb")
    a = answer("Order 40 units of Røkt laks 300 g [1].", s)
    assert citation_errors(a["sources"], e) == [] and evidence_errors(a, e) == []


# ---------- in the loop: wrong numbers go back twice, then accepted with warnings ----------

FAKE_PLAN = Tool("recommend_orders", "plan", {"type": "object", "properties": {}},
                 run=lambda: {k: v for k, v in PLAN_OUTPUT.items() if k != "call_id"})


def plan_then_submit(monkeypatch, claim_texts):
    """LLM: call recommend_orders, then submit each answer text in turn (citing next_order.qty)."""
    seen = []

    def fake(system, messages, tools, force_tool=None):
        seen.append(messages[-1])
        results = [m for m in messages if m["role"] == "tool" and m["name"] == "recommend_orders"]
        if not results:
            return LLMResponse("", [ToolCall("p", "recommend_orders", {})])
        import json
        call_id = json.loads(results[0]["content"])["call_id"]
        text = claim_texts[min(len(seen) - 2, len(claim_texts) - 1)]
        source = tool_source(text, ["next_order.qty"], call_id=call_id)
        return LLMResponse("", [ToolCall(f"s{len(seen)}", "submit_answer", {"answer": f"{text} [1].", "sources": [source]})])
    monkeypatch.setattr(loop, "call_llm", fake)
    return seen


def test_wrong_number_is_sent_back_then_fixed(monkeypatch):
    seen = plan_then_submit(monkeypatch, ["Order 400 units", "Order 40 units"])
    r = loop.run_agent("sys", [FAKE_PLAN], "q", output_schema=SCHEMA)
    assert 'number "400"' in seen[2]["content"]
    assert r.answer["answer"] == "Order 40 units [1]." and "warnings" not in r.answer
    assert r.trace[1]["call_id"].startswith("rc-")                      # every tool call gets a call_id


def test_wrong_number_twice_then_accepted_with_warnings(monkeypatch):
    plan_then_submit(monkeypatch, ["Order 400 units"])                   # never fixed
    r = loop.run_agent("sys", [FAKE_PLAN], "q", output_schema=SCHEMA)
    finals = [e for e in r.trace if e["type"] == "final_answer"]
    assert [e["valid"] for e in finals] == [False, False, True]
    assert r.stopped_reason == "answered"
    assert r.answer["warnings"] and 'number "400"' in r.answer["warnings"][0]
    assert finals[-1]["warnings"] == r.answer["warnings"]


def test_full_tool_output_is_handed_over_for_storage(monkeypatch):
    stored = []
    plan_then_submit(monkeypatch, ["Order 40 units"])
    loop.run_agent("sys", [FAKE_PLAN], "q", output_schema=SCHEMA,
                   on_tool_output=lambda call_id, tool, args, output: stored.append((call_id, tool, output)))
    (call_id, tool, output), = stored
    assert tool == "recommend_orders" and output["call_id"] == call_id and output["rows"][0]["next_order"]["qty"] == 40


def test_rounded_thousands_and_norwegian_dates():
    from app.agent.numbers import claim_mismatches
    assert claim_mismatches("about NOK 49,000 at risk", [49235]) == []
    assert claim_mismatches("from 414,000 to 9,700", [414493, 9745]) == []
    assert claim_mismatches("order 40", [44]) == ['number "40"']           # small numbers stay exact
    assert claim_mismatches("bestill innen 6. nov", ["2026-11-06"]) == []
    text = "Røkt laks 300 g: bestill 40 stk 6. nov [1]."
    assert unmarked_fact_errors({"answer": text, "sources": []}, {"Røkt laks 300 g"}) == []   # one sentence, marked
