"""Orchestrator + specialists (fake LLM, fake tools, fake run logging; no network)."""
import pytest

from app.agent import loop, orchestrator, service
from app.llm import LLMResponse, Tool, ToolCall

CHUNK = {"chunk_id": "r.pdf:p1:c0", "document_name": "r.pdf", "page_number": 1, "text": "Target is 45%"}
SQL = "select region, margin from sample_sales"

FAKE_SEARCH = Tool("search_documents", "search", {"type": "object", "properties": {"query": {"type": "string"}}},
                   run=lambda query: {"results": [CHUNK]},
                   trace_fields=lambda out: {"chunk_ids": [r["chunk_id"] for r in out["results"]]})
FAKE_SQL = Tool("query_database", "sql", {"type": "object", "properties": {"sql": {"type": "string"}}},
                run=lambda sql: {"rows": [{"region": "Bergen", "margin": 41.8}]})

SOURCES = [
    {"claim": "Target is 45%", "document_name": "r.pdf", "page_number": 1, "chunk_id": "r.pdf:p1:c0"},
    {"claim": "Bergen was 41.8%", "table": "sample_sales", "sql": SQL},
]


@pytest.fixture
def setup(monkeypatch):
    """Fake specialists' tools, fake run logging, and an LLM that acts by role."""
    monkeypatch.setattr(orchestrator.SPECIALISTS[0], "tools", [FAKE_SQL])
    monkeypatch.setattr(orchestrator.SPECIALISTS[1], "tools", [FAKE_SEARCH])
    created, finished = [], []
    counter = iter(range(1, 100))

    def create_run(task, agent_name="agent", parent_run_id=None):
        run_id = f"{agent_name}-{next(counter)}"
        created.append((run_id, parent_run_id))
        return run_id

    for module in (service.runs, orchestrator.runs):
        monkeypatch.setattr(module, "create_run", create_run)
        monkeypatch.setattr(module, "finish_run", lambda run_id, status, trace, **kw: finished.append((run_id, status)))
    monkeypatch.setattr(service, "build_system_prompt", lambda custom, **kw: custom)
    return created, finished


def role_llm(final_sources):
    """Orchestrator: delegate to both, then submit. Specialists: use their tool, then answer."""
    def fake(system, messages, tools, force_tool=None):
        names = [t.name for t in tools]
        tool_results = [m for m in messages if m["role"] == "tool"]
        if "ask_data_analyst" in names:                       # orchestrator
            if not tool_results:
                return LLMResponse("", [ToolCall("d1", "ask_data_analyst", {"question": "Bergen margin?"}),
                                        ToolCall("d2", "ask_document_researcher", {"question": "Target?"})])
            summary = "Target 45% [1]; Bergen missed it [2]." if len(final_sources) == 2 else "Bergen missed [1]."
            return LLMResponse("", [ToolCall("s", "submit_answer", {"summary": summary, "sources": final_sources})])
        if names == ["query_database"]:                       # data analyst
            return LLMResponse("41.8%") if tool_results else LLMResponse("", [ToolCall("q", "query_database", {"sql": SQL})])
        return LLMResponse("45%") if tool_results else LLMResponse("", [ToolCall("q", "search_documents", {"query": "target"})])
    return fake


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def run_orchestrator(events=None):
    return service.execute(service.RunRequest(task="Did Bergen meet the target?", mode="orchestrator",
                                              output_schema=SCHEMA), on_event=(events or []).append)


def test_delegation_tree_in_trace_and_runs(monkeypatch, setup):
    created, finished = setup
    monkeypatch.setattr(loop, "call_llm", role_llm(SOURCES))
    events = []
    r = service.execute(service.RunRequest(task="Did Bergen meet the target?", mode="orchestrator",
                                           output_schema=SCHEMA), on_event=events.append)

    # agent_runs: orchestrator + two children pointing at it (children start in parallel: any order)
    assert created[0] == ("orchestrator-1", None)
    children = {run_id.rsplit("-", 1)[0]: (run_id, parent) for run_id, parent in created[1:]}
    assert set(children) == {"data_analyst", "document_researcher"}
    assert all(parent == "orchestrator-1" for _, parent in children.values())
    assert all((run_id, "completed") in finished for run_id, _ in children.values())

    # trace: each delegation entry nests the specialist's own trace
    delegations = [e for e in r["trace"] if e["type"] == "tool"]
    assert [d["specialist"] for d in delegations] == ["data_analyst", "document_researcher"]
    assert [d["child_run_id"] for d in delegations] == [children["data_analyst"][0], children["document_researcher"][0]]
    assert [e["tool"] for e in delegations[0]["child_trace"] if e["type"] == "tool"] == ["query_database"]
    assert delegations[1]["child_trace"][1]["chunk_ids"] == ["r.pdf:p1:c0"]
    assert all(e["agent"] == "orchestrator" for e in r["trace"])

    # stream: specialist steps arrive live, tagged with agent + their own run_id
    started = [e for e in events if e["type"] == "delegation_started"]
    assert sorted(e["specialist"] for e in started) == ["data_analyst", "document_researcher"]
    child_tools = {(e["agent"], e["run_id"], e["tool"]) for e in events if e["type"] == "tool" and e["agent"] != "orchestrator"}
    assert child_tools == {("data_analyst", children["data_analyst"][0], "query_database"),
                           ("document_researcher", children["document_researcher"][0], "search_documents")}

    # the orchestrator could cite what its specialists found
    assert r["answer"]["sources"] == SOURCES


def test_orchestrator_cannot_cite_what_specialists_did_not_find(monkeypatch, setup):
    invented = [{**SOURCES[0], "chunk_id": "r.pdf:p9:c9"}]
    monkeypatch.setattr(loop, "call_llm", role_llm(invented))  # keeps submitting the bad citation
    r = run_orchestrator()
    rejected = [e for e in r["trace"] if e["type"] == "final_answer"]
    assert rejected and not any(e["valid"] for e in rejected)
    assert "was not returned" in rejected[0]["errors"][0]
    assert r["stopped_reason"] == "max_steps"


def test_orchestrator_has_no_direct_data_tools():
    names = [t.name for t in service.tools_for_run("run-1", "orchestrator")]
    assert names == ["ask_data_analyst", "ask_document_researcher", "save_report", "create_purchase_order"]


def test_specialists_only_see_their_own_context(monkeypatch):
    monkeypatch.setattr(service, "describe_schema", lambda: "TABLES")
    monkeypatch.setattr(service, "describe_documents", lambda: "DOCS")
    analyst, researcher = orchestrator.SPECIALISTS
    analyst_prompt = service.build_system_prompt("p", tables=analyst.sees_tables, documents=analyst.sees_documents)
    researcher_prompt = service.build_system_prompt("p", tables=researcher.sees_tables, documents=researcher.sees_documents)
    assert "TABLES" in analyst_prompt and "DOCS" not in analyst_prompt
    assert "DOCS" in researcher_prompt and "TABLES" not in researcher_prompt


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

    monkeypatch.setattr(FAKE_SQL, "run", slow("sql", {"rows": [{"region": "Bergen", "margin": 41.8}]}))
    monkeypatch.setattr(FAKE_SEARCH, "run", slow("search", {"results": [CHUNK]}))
    monkeypatch.setattr(loop, "call_llm", role_llm(SOURCES))
    created, finished = setup

    t0 = time.monotonic()
    r = run_orchestrator()
    elapsed = time.monotonic() - t0

    starts = {n: t for n, k, t in log if k == "start"}
    ends = {n: t for n, k, t in log if k == "end"}
    assert starts["search"] < ends["sql"] and starts["sql"] < ends["search"]   # overlapping
    assert elapsed < 0.75, f"took {elapsed:.2f}s - specialists ran one after the other"

    delegations = [e for e in r["trace"] if e["type"] == "tool"]
    assert [d["specialist"] for d in delegations] == ["data_analyst", "document_researcher"]
    assert all(d["parallel"] for d in delegations)
    assert {c for c, parent in created if parent == "orchestrator-1"} == {d["child_run_id"] for d in delegations}
    assert [e["tool"] for e in delegations[0]["child_trace"] if e["type"] == "tool"] == ["query_database"]
    assert [e["tool"] for e in delegations[1]["child_trace"] if e["type"] == "tool"] == ["search_documents"]
    assert r["answer"]["sources"] == SOURCES
