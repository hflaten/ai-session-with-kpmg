"""Orchestrator + specialists (fake LLM, fake tools, fake run logging; no network)."""
import json

import pytest

from app import runs
from app.agent import loop, orchestrator, service
from app.llm import LLMResponse, Tool, ToolCall

SQL = "select sum(stock_value_nok) from inventory_current"
PLAN = {"rows": [{"sku": "SKU009", "location_id": "L04", "product": "Røkt laks 300 g",
                  "location": "Høyden Nettbutikk", "next_order": {"order_date": "2026-11-06", "qty": 40}}]}

FAKE_PLAN = Tool("recommend_orders", "plan", {"type": "object", "properties": {"sku": {"type": "string"}}},
                 run=lambda **kw: PLAN)
FAKE_SQL = Tool("query_database", "sql", {"type": "object", "properties": {"sql": {"type": "string"}}},
                run=lambda sql: {"rows": [{"sum": 393306}]})
DB_SOURCE = {"claim": "Stock is worth NOK 393k", "table": "inventory_current", "sql": SQL}


@pytest.fixture
def setup(monkeypatch):
    """Fake specialists' tools, fake run logging, and an LLM that acts by role."""
    monkeypatch.setattr(orchestrator.SPECIALISTS[0], "tools", [FAKE_SQL])
    monkeypatch.setattr(orchestrator.SPECIALISTS[1], "tools", [FAKE_PLAN])
    created, finished = [], []
    counter = iter(range(1, 100))

    def create_run(task, agent_name="agent", parent_run_id=None):
        run_id = f"{agent_name}-{next(counter)}"
        created.append((run_id, parent_run_id))
        return run_id

    for module in (service.runs, orchestrator.runs):
        monkeypatch.setattr(module, "create_run", create_run)
        monkeypatch.setattr(module, "finish_run", lambda run_id, status, trace, **kw: finished.append((run_id, status)))
    monkeypatch.setattr(runs, "save_tool_call", lambda *a, **kw: None)
    monkeypatch.setattr(service, "build_system_prompt", lambda custom, **kw: custom)
    return created, finished


def tool_outputs(messages) -> list[dict]:
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool" and m["content"].startswith("{")]


def role_llm(orchestrator_sources=None):
    """Orchestrator: delegate to both, then submit the specialists' sources (or the given ones).
    Specialists: use their tool, then submit a cited answer."""
    def fake(system, messages, tools, force_tool=None):
        names = [t.name for t in tools]
        results = tool_outputs(messages)
        if "ask_data_analyst" in names:                       # orchestrator
            if not results:
                return LLMResponse("", [ToolCall("d1", "ask_data_analyst", {"question": "Stock value?"}),
                                        ToolCall("d2", "ask_inventory_planner", {"question": "What to order?"})])
            sources = orchestrator_sources or [s for r in results for s in r["answer"]["sources"]]
            text = " ".join(f"{s['claim']} [{i}]." for i, s in enumerate(sources, 1))
            return LLMResponse("", [ToolCall("s", "submit_answer", {"summary": text, "sources": sources})])
        if "query_database" in names:                         # data analyst
            if not results:
                return LLMResponse("", [ToolCall("q", "query_database", {"sql": SQL})])
            return LLMResponse("", [ToolCall("a", "submit_answer", {"answer": "Stock is NOK 393k [1].",
                                                                    "sources": [DB_SOURCE]})])
        if not results:                                       # inventory planner
            return LLMResponse("", [ToolCall("q", "recommend_orders", {"sku": "SKU009"})])
        source = {"claim": "Order 40 units of Røkt laks 300 g", "tool": "recommend_orders",
                  "call_id": results[0]["call_id"], "row": {"sku": "SKU009", "location_id": "L04"},
                  "fields": ["next_order.qty"]}
        return LLMResponse("", [ToolCall("a", "submit_answer", {"answer": "Order 40 units [1].", "sources": [source]})])
    return fake


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def run_orchestrator(events=None):
    return service.execute(service.RunRequest(task="What should we order for Christmas?", mode="orchestrator",
                                              output_schema=SCHEMA), on_event=(events if events is not None else []).append)


def test_delegation_tree_in_trace_and_runs(monkeypatch, setup):
    created, finished = setup
    monkeypatch.setattr(loop, "call_llm", role_llm())
    events = []
    r = run_orchestrator(events)

    # agent_runs: orchestrator + two children pointing at it (children start in parallel: any order)
    assert created[0] == ("orchestrator-1", None)
    children = {run_id.rsplit("-", 1)[0]: (run_id, parent) for run_id, parent in created[1:]}
    assert set(children) == {"data_analyst", "inventory_planner"}
    assert all(parent == "orchestrator-1" for _, parent in children.values())
    assert all((run_id, "completed") in finished for run_id, _ in children.values())

    # trace: each delegation entry nests the specialist's own trace
    delegations = [e for e in r["trace"] if e["type"] == "tool"]
    assert [d["specialist"] for d in delegations] == ["data_analyst", "inventory_planner"]
    assert [d["child_run_id"] for d in delegations] == [children["data_analyst"][0], children["inventory_planner"][0]]
    assert [e["tool"] for e in delegations[0]["child_trace"] if e["type"] == "tool"] == ["query_database"]
    assert [e["tool"] for e in delegations[1]["child_trace"] if e["type"] == "tool"] == ["recommend_orders"]
    assert all(e["agent"] == "orchestrator" for e in r["trace"])

    # stream: specialist steps arrive live, tagged with agent + their own run_id
    started = [e for e in events if e["type"] == "delegation_started"]
    assert sorted(e["specialist"] for e in started) == ["data_analyst", "inventory_planner"]
    child_tools = {(e["agent"], e["run_id"], e["tool"]) for e in events if e["type"] == "tool" and e["agent"] != "orchestrator"}
    assert child_tools == {("data_analyst", children["data_analyst"][0], "query_database"),
                           ("inventory_planner", children["inventory_planner"][0], "recommend_orders")}

    # the orchestrator re-cited its specialists' checked sources: SQL, and the planner's call_id + row + fields
    sources = r["answer"]["sources"]
    assert DB_SOURCE in sources
    tool_source = next(s for s in sources if "tool" in s)
    planner_call = next(e for e in delegations[1]["child_trace"] if e["type"] == "tool")["call_id"]
    assert tool_source["call_id"] == planner_call and tool_source["fields"] == ["next_order.qty"]
    assert "warnings" not in r["answer"]


def test_orchestrator_cannot_cite_a_call_that_did_not_run(monkeypatch, setup):
    invented = [{"claim": "Order 40", "tool": "recommend_orders", "call_id": "rc-000000",
                 "row": {"sku": "SKU009", "location_id": "L04"}, "fields": ["next_order.qty"]}]
    monkeypatch.setattr(loop, "call_llm", role_llm(invented))  # keeps submitting the bad citation
    r = run_orchestrator()
    rejected = [e for e in r["trace"] if e["type"] == "final_answer"]
    assert rejected and not any(e["valid"] for e in rejected)
    assert "call_id 'rc-000000' is not a recommend_orders call from this run" in rejected[0]["errors"][0]
    assert r["stopped_reason"] == "max_steps"     # a made-up call_id is never accepted


def test_orchestrator_has_no_direct_data_tools():
    names = [t.name for t in service.tools_for_run("run-1", "orchestrator")]
    assert names == ["ask_data_analyst", "ask_inventory_planner", "save_report", "create_purchase_order"]
    tools = {t.name: t for t in service.tools_for_run("run-1", "orchestrator")}
    assert tools["create_purchase_order"].requires_approval


def test_specialists_have_the_right_tools():
    analyst, planner = orchestrator.SPECIALISTS
    assert (analyst.name, [t.name for t in analyst.tools]) == ("data_analyst", ["query_database", "recommend_orders"])
    assert (planner.name, [t.name for t in planner.tools]) == ("inventory_planner", ["recommend_orders"])


def test_specialists_only_see_their_own_context(monkeypatch):
    monkeypatch.setattr(service, "describe_schema", lambda: "TABLES")
    monkeypatch.setattr(service, "describe_documents", lambda: "DOCS")
    monkeypatch.setattr(service, "business_context", lambda: "CONTEXT")
    analyst, planner = orchestrator.SPECIALISTS
    analyst_prompt = service.build_system_prompt("p", tables=analyst.sees_tables, documents=analyst.sees_documents)
    planner_prompt = service.build_system_prompt("p", tables=planner.sees_tables, documents=planner.sees_documents)
    assert "TABLES" in analyst_prompt and "DOCS" not in analyst_prompt
    assert planner_prompt == f"p\n\n{service.ANSWER_STYLE}\n\nCONTEXT"   # no SQL, so no table list


def test_specialists_run_in_parallel_and_tree_stays_correct(monkeypatch, setup):
    """Both specialists are working at the same time; trace and run links are unchanged."""
    import time
    log = []

    def slow(name, result):
        def run(**kwargs):
            log.append((name, "start", time.monotonic()))
            time.sleep(0.4)
            log.append((name, "end", time.monotonic()))
            return result
        return run

    monkeypatch.setattr(FAKE_SQL, "run", slow("sql", {"rows": [{"sum": 393306}]}))
    monkeypatch.setattr(FAKE_PLAN, "run", slow("plan", PLAN))
    monkeypatch.setattr(loop, "call_llm", role_llm())
    created, finished = setup

    t0 = time.monotonic()
    r = run_orchestrator()
    elapsed = time.monotonic() - t0

    starts = {n: t for n, k, t in log if k == "start"}
    ends = {n: t for n, k, t in log if k == "end"}
    assert starts["plan"] < ends["sql"] and starts["sql"] < ends["plan"]   # overlapping
    assert elapsed < 0.75, f"took {elapsed:.2f}s - specialists ran one after the other"

    delegations = [e for e in r["trace"] if e["type"] == "tool"]
    assert [d["specialist"] for d in delegations] == ["data_analyst", "inventory_planner"]
    assert all(d["parallel"] for d in delegations)
    assert {c for c, parent in created if parent == "orchestrator-1"} == {d["child_run_id"] for d in delegations}
