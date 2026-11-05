"""End-to-end checks against a running backend (local or Render).

    .venv/bin/python -m scripts.live_check https://ai-session-with-kpmg.onrender.com
    .venv/bin/python -m scripts.live_check http://localhost:8000 health stream approve
    checks: health run stream approve reject orchestrate   (default: all)

Reads APP_API_KEY from .env. Prints a timeline of events; never prints secrets.
"""
import json
import sys
import time

import httpx
from dotenv import dotenv_values

KEY = dotenv_values(".env").get("APP_API_KEY", "")
H = {"X-API-Key": KEY}


def events(base: str, body: dict, on_event=None):
    """POST /agent/stream and yield (seconds, event_name, data) as they arrive."""
    t0 = time.monotonic()
    event = None
    with httpx.stream("POST", f"{base}/agent/stream", headers=H, json=body, timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                yield time.monotonic() - t0, event, data


def describe(event: str, d: dict) -> str:
    if event == "run_started":
        return d["run_id"]
    if event == "approval_required":
        return f"{d['tool']} {json.dumps(d['input'])[:100]} (run {d['run_id'][:8]})"
    if event == "done":
        a = d["answer"]
        return (a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))[:160]
    if event == "error":
        return d["error"]
    agent = f"[{d['agent']}] " if d.get("agent") else ""
    if d["type"] == "tool":
        extra = d.get("chunk_ids") or d["input"].get("sql") or d["input"].get("question") or d["input"]
        return f"{agent}tool {d['tool']}: {str(extra)[:110]}" + (f"  ERROR {d['error']}" if d.get("error") else "")
    if d["type"] == "llm":
        return f"{agent}llm -> {[c['name'] for c in d['tool_calls']] or 'answer'}"
    if d["type"] == "approval":
        return f"{agent}approval {d['tool']}: {d['decision']} ({d['reason']}) after {d['waited_ms']} ms"
    if d["type"] == "delegation_started":
        return f"{agent}delegating to {d['specialist']} (run {d['child_run_id'][:8]}): {d['question'][:80]}"
    return f"{agent}{d['type']}"


def timeline(base: str, body: dict, decide: str | None = None) -> dict:
    done = {}
    for t, event, d in events(base, body):
        print(f"  {t:5.1f}s  {event:17} {describe(event, d)}")
        if event == "approval_required" and decide:
            reason = "approved by live_check" if decide == "approve" else "rejected by live_check: not needed"
            r = httpx.post(f"{base}/runs/{d['run_id']}/{decide}", headers=H, json={"reason": reason}, timeout=30)
            print(f"  {'':5}   -> POST /runs/{d['run_id'][:8]}/{decide}: HTTP {r.status_code} {r.json()}")
        if event == "done":
            done = d
    return done


def check_health(base):
    r = httpx.get(f"{base}/health", timeout=120)
    print(f"  HTTP {r.status_code} {r.json()}")
    bad = httpx.post(f"{base}/agent/run", json={"task": "x"}, timeout=60).status_code
    print(f"  without key: HTTP {bad} (expect 401)")


def check_run(base):
    t = time.monotonic()
    r = httpx.post(f"{base}/agent/run", headers=H, timeout=300,
                   json={"task": "Total revenue per region in Q1 2026?"}).json()
    print(f"  {time.monotonic() - t:.1f}s  {r['stopped_reason']}: {r['answer'][:160]!r}")


def check_stream(base):
    timeline(base, {"task": "Which product had the highest revenue in Trondheim across all months?"})


SAVE_TASK = ("Calculate total Q1 2026 revenue per region, then save it as a report titled "
             "'Q1 2026 revenue by region'.")


def check_approve(base):
    timeline(base, {"task": SAVE_TASK}, decide="approve")


def check_reject(base):
    timeline(base, {"task": SAVE_TASK}, decide="reject")


def check_orchestrate(base):
    done = timeline(base, {"task": "Which offices missed the Q1 2026 margin target in the management report, "
                                   "by how much, and why?", "mode": "orchestrator",
                           "output_schema": {"type": "object", "required": ["summary"],
                                             "properties": {"summary": {"type": "string"}}}})
    if done:
        print("  sources:")
        for s in done["answer"].get("sources", []):
            print("   -", f"{s['document_name']} p{s['page_number']} ({s['chunk_id']})" if "chunk_id" in s
                  else f"{s['table']}: {s['sql'][:90]}")
        resolved = httpx.get(f"{base}/runs/{done['run_id']}/sources", headers=H, timeout=60).json()["sources"]
        print("  resolved sources (GET /runs/{id}/sources):")
        for s in resolved:
            shown = (f"page text: {s['text'][:70]!r}" if s["kind"] == "document"
                     else f"{s.get('row_count', 0)} rows, first: {s['rows'][:1]}")
            print(f"   [{s['index']}] {s['kind']:8} {shown}" + (f"  ERROR {s['error']}" if s["error"] else ""))
        tree = httpx.get(f"{base}/runs/{done['run_id']}/tree", headers=H, timeout=60).json()
        print("  delegation tree (GET /runs/{id}/tree):")

        def show(node, depth=1):
            print(f"  {'  ' * depth}- {node['agent_name']} ({node['status']}, run {node['id'][:8]})")
            for child in node["children"]:
                show(child, depth + 1)
        show(tree)


CHECKS = {"health": check_health, "run": check_run, "stream": check_stream,
          "approve": check_approve, "reject": check_reject, "orchestrate": check_orchestrate}

if __name__ == "__main__":
    base = sys.argv[1].rstrip("/")
    for name in sys.argv[2:] or CHECKS:
        print(f"== {name}")
        try:
            CHECKS[name](base)
        except Exception as e:
            print(f"  FAILED: {type(e).__name__}: {e}")
