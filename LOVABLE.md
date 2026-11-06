# Connecting a Lovable frontend

How a frontend talks to this backend, plus a ready-to-paste Lovable prompt at the end.

Placeholders used below — replace them in the Lovable project, never commit real values:

| Placeholder | Value |
|---|---|
| `{{BACKEND_URL}}` | e.g. `https://ai-session-with-kpmg.onrender.com` (no trailing slash) |
| `{{APP_API_KEY}}` | the `APP_API_KEY` from the backend's environment |

> The API key ends up in the browser, so anyone who opens the app can read it. It only stops
> casual abuse of the URL. Rotate it after the hackathon. The frontend never talks to Supabase
> directly and never needs any Supabase key.

## 1. Auth and basics

- Every request except `GET /health` needs the header **`X-API-Key: {{APP_API_KEY}}`**.
- Browser `EventSource` can't set headers; for `GET /agent/stream` use `?api_key={{APP_API_KEY}}` instead.
- CORS allows `http://localhost:3000`, `https://*.lovable.app` and `https://*.lovableproject.com`.
- The backend runs on Render's free plan and **sleeps when idle**: the first request can take
  30–60 s. Call `GET /health` when the app loads and show a "waking up the backend…" state.

| Status | Meaning |
|---|---|
| 401 | missing or wrong `X-API-Key` |
| 503 | the server has no `APP_API_KEY` configured (deploy issue) |
| 400 | bad request, e.g. invalid `output_schema` or wrong file type |
| 404 | run not found / run is not waiting for an approval |
| 413 | upload larger than 20 MB |
| 422 | PDF with no text layer (scanned) |

## 2. Endpoints

| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/health` | – | `{status, llm_provider, llm_model, database, commit}` |
| POST | `/agent/stream` | JSON `RunRequest` (below) | **SSE stream** — the main endpoint for a chat UI |
| POST | `/agent/run` | JSON `RunRequest` | `{run_id, answer, stopped_reason, trace}` when finished (no live steps) |
| GET | `/agent/stream` | `?task=&mode=&api_key=` | SSE stream for `EventSource` (no `output_schema`) |
| GET | `/runs/{run_id}` | – | the stored run: `{id, task, status, answer, trace, agent_name, parent_run_id, …}` |
| GET | `/runs/{run_id}/sources` | – | the answer's sources **resolved**: page text / table rows, `marker`, latest `review`, plus a review `summary` (sections 5 and 8) |
| POST | `/runs/{run_id}/claims/{marker}/review` | `{decision: "accepted" \| "rejected", reviewer?, comment?}` | `{run_id, marker, review}` — `marker` is the `[n]` in the text (section 8) |
| GET | `/runs/{run_id}/tree` | – | delegation tree `{id, agent_name, task, status, duration_ms, children: [...]}` |
| GET | `/approvals` | – | `{pending: [{run_id, tool, input, waiting_seconds}]}` |
| POST | `/runs/{run_id}/approve` | `{reason?: string}` | `{run_id, decision: "approved"}` |
| POST | `/runs/{run_id}/reject` | `{reason?: string}` | `{run_id, decision: "rejected"}` |
| POST | `/upload/table` | multipart: `file` (.csv/.xlsx/.xls), `column_descriptions?` (JSON string) | `{tables: [{table, rows, comment, columns}], unused_column_descriptions}` |
| POST | `/upload/pdf` | multipart: `file` (.pdf) | `{document_name, pages, chunks}` |
| GET | `/insights/summary` | `?horizon_days=56` | KPI cards for the inventory dashboard (section 9) |
| GET | `/insights/recommendations` | `?limit=30&location_id=&sku=&horizon_days=56` | `{today, horizon_days, recommendations, overstock}` (section 9) |
| POST | `/orders` | `{sku, location_id, qty, order_date, decision: "approved" \| "rejected", reason?, decided_by?}` | `{order}` — a human decision on a recommended order |
| GET | `/orders` | `?limit=100` | `{orders: [...]}` newest first |

**`RunRequest`**

```json
{
  "task": "Which offices missed the Q1 margin target, and why?",
  "mode": "agent",
  "output_schema": {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "description": "The answer in Markdown"}}
  },
  "system_prompt": null
}
```

- `mode`: `"agent"` (one agent with all tools, faster) or `"orchestrator"` (delegates to a
  `data_analyst` and a `document_researcher`; slower, shows a delegation tree).
- `output_schema` (optional, recommended): JSON schema with `"type": "object"` at the top.
  With a schema, `answer` is an object and **always includes `sources`** (don't define
  `sources` yourself). Without one, `answer` is a plain Markdown string with no sources.

## 3. Consuming the stream with `fetch`

`EventSource` only supports GET without headers, so use `fetch` and read the body:

```ts
export const BACKEND_URL = "{{BACKEND_URL}}";
export const API_KEY = "{{APP_API_KEY}}";

type Handler = (event: string, data: any) => void;

export async function streamAgent(body: object, onEvent: Handler, signal?: AbortSignal) {
  const res = await fetch(`${BACKEND_URL}/agent/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}: ${await res.text()}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {      // events are separated by a blank line
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}
```

### Events, in order

| SSE `event` | `data` | What to show |
|---|---|---|
| `run_started` | `{type, run_id, agent}` | keep `run_id`; start the trace panel |
| `step` | one trace entry (below) | append to the live trace |
| `approval_required` | `{type, step, run_id, tool, input, agent}` | an approval card with Approve / Reject; the stream **pauses** until you call approve/reject |
| `done` | `{type: "done", run_id, answer, stopped_reason, trace}` | the final answer; then fetch `/runs/{run_id}/sources` |
| `error` | `{type: "error", error}` | an error message; the stream ends |

`stopped_reason` is `"answered"` or `"max_steps"` (gave up after 10 steps).

### Trace entries (`step` events)

Every entry has `step`, `type` and `agent` (`agent`, `orchestrator`, `data_analyst`, `document_researcher`).

| `type` | Fields | Display |
|---|---|---|
| `llm` | `text`, `tool_calls: [{name, input}]`, `tokens`, `ms` | "Thinking… → calls query_database" |
| `tool` | `tool`, `input`, `output` (string, max 2000 chars), `error`, `ms`, `parallel` (ran at the same time as other calls); `chunk_ids` for `search_documents`; `specialist`, `child_run_id`, `child_trace` for delegations | tool name + input (show `input.sql` as code, `input.query` as text) |
| `approval` | `tool`, `decision` (`approved` \| `rejected` \| `timeout`), `reason`, `waited_ms` | the human decision |
| `final_answer` | `input`, `valid`, `errors` | if `valid` is false: "answer rejected, retrying" + `errors` |
| `delegation_started` | `specialist`, `child_run_id`, `question` | orchestrator mode: "Asking data_analyst: …"; that specialist's own steps follow (with their own `agent` and `run_id`) |

Indent steps whose `agent` is a specialist under the orchestrator.

## 4. Answers and the `sources` format

With an `output_schema`, `answer` looks like this (your fields + `sources`):

```json
{
  "answer": "The target was at least 45% for every office [1], and offices below it must present a recovery plan in May [2]. Only Oslo met it, at 46.3% [3].",
  "sources": [
    {"claim": "The target is at least 45% gross margin for every office",
     "document_name": "nordvik_q1_2026_report.pdf", "page_number": 1,
     "chunk_id": "nordvik_q1_2026_report.pdf:p1:c0"},
    {"claim": "Offices below target must present a recovery plan in May 2026",
     "document_name": "nordvik_q1_2026_report.pdf", "page_number": 1,
     "chunk_id": "nordvik_q1_2026_report.pdf:p1:c0"},
    {"claim": "Oslo's actual Q1 2026 margin was 46.3%",
     "table": "sample_sales",
     "sql": "select region, round(100*(sum(revenue)-sum(cost))/sum(revenue),2) as margin from sample_sales ..."}
  ]
}
```

Three kinds of source — tell them apart by which key exists:

- **Document**: has `chunk_id` → `{claim, document_name, page_number, chunk_id}`
- **Database**: has `sql` → `{claim, table, sql}`
- **Tool**: has `tool` → `{claim, tool, arguments}` — a deterministic calculation, e.g.
  `{"tool": "recommend_orders", "arguments": {"location_id": "L01"}}`

The backend has already checked that every cited chunk was really found by a search in this
run (same document and page), every SQL really ran, and every tool really ran with those
arguments. One claim can have several entries.

### Inline markers `[n]` — one marker = one claim

The answer text cites inline: **`[n]` right after a claim, where `n` is the 1-based position in
`sources`** — `[1]` is `sources[0]`, `[2]` is `sources[1]`. The backend guarantees:

- every marker points to an existing source, and every source is marked;
- **each marker appears exactly once** — one marker is one claim, so each claim can be reviewed on
  its own. Two claims resting on the same evidence get **two entries** in `sources` (same
  document/page/chunk or table/SQL, different `claim`), as in the example above (`[1]` and `[2]`);
- a claim backed by two sources gets two adjacent markers, `[1][2]` — still one claim;
- **each claim is a single sentence or clause, with its marker right after it** — a marker never
  covers several sentences, and every sentence that contains a number, percentage or date has a
  marker. Context sentences without facts have none. So a claim span never runs over a sentence end.

Markers can appear in any text field of your schema, not only `answer`.

**Rendering claim spans.** Highlight each claim's own text, not the whole sentence, so a sentence
with two claims can show one green and one red. A claim's span runs from the end of the previous
marker (or the start of the sentence / line, whichever is later) up to its marker. This function
does the split (tested against real answers; joining the parts gives back the original text):

```js
// Split answer text into plain text and claim spans.
// A claim span runs from the end of the previous marker (or the start of the
// sentence / line, whichever is later) up to its marker(s). "[1][2]" = one claim, two sources.
function splitClaims(text) {
  const parts = [];                                   // {type: "text"|"claim", text, markers?}
  const group = /(?:\[\d+\])+/g;                      // one or more adjacent markers
  let last = 0, m;
  while ((m = group.exec(text))) {
    const before = text.slice(last, m.index);
    // the claim starts after the last sentence end or line break in `before`
    let start = 0;
    for (const b of before.matchAll(/[.!?](?=\s)\s*|\n/g)) start = b.index + b[0].length;
    // keep joiners, list bullets and spaces outside the highlight
    const lead = before.slice(start).match(/^[\s,;:]*(?:(?:and|but|while)\s+)?(?:[-*•]\s+|\d+\.\s+)?/i)[0];
    const claimStart = start + lead.length;
    if (claimStart > 0) parts.push({ type: "text", text: before.slice(0, claimStart) });
    const markers = m[0].match(/\d+/g).map(Number);
    parts.push({ type: "claim", text: before.slice(claimStart), markers });   // keeps the space before the marker
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ type: "text", text: text.slice(last) });
  return parts;
}
```

Example — `splitClaims("The target is 45% [1], and plans are due in May [2].")` returns:

```json
[{"type": "claim", "text": "The target is 45% ", "markers": [1]},
 {"type": "text",  "text": ", and "},
 {"type": "claim", "text": "plans are due in May ", "markers": [2]},
 {"type": "text",  "text": "."}]
```

## 5. Clickable sources: `GET /runs/{run_id}/sources`

Call this after `done`. It returns the same sources, in the same order, with the evidence,
the inline `marker` (`index + 1`) and the latest human `review` (section 8):

```json
{
  "run_id": "b9f8fc9e-...",
  "summary": {"total": 4, "accepted": 1, "rejected": 1, "unreviewed": 2},
  "sources": [
    {"index": 0, "marker": 1, "kind": "document", "claim": "...", "document_name": "nordvik_q1_2026_report.pdf",
     "page_number": 1, "chunk_id": "nordvik_q1_2026_report.pdf:p1:c0",
     "text": "Nordvik Advisory - Q1 2026 Management Report ...", "error": null,
     "review": {"marker": 1, "decision": "accepted", "reviewer": "Ana", "comment": "matches page 1",
                "created_at": "2026-11-05T15:02:11Z", "id": "...",
                "claim": "The target is at least 45% gross margin for every office", "source_kind": "document",
                "document_name": "nordvik_q1_2026_report.pdf", "page_number": 1,
                "chunk_id": "nordvik_q1_2026_report.pdf:p1:c0", "table_name": null, "sql_query": null}},
    {"index": 1, "marker": 2, "kind": "database", "claim": "...", "table": "sample_sales", "sql": "select ...",
     "columns": ["region", "revenue", "cost", "margin_pct"],
     "rows": [{"region": "Bergen", "revenue": 710000.0, "cost": 413000.0, "margin_pct": 41.83}],
     "row_count": 3, "truncated": false, "error": null, "review": null}
  ]
}
```

- `document` → show `text` (the original page text) with "document_name — page N".
- `database` → show `rows` as a table with `columns` as headers, plus the `sql` (collapsible).
- `tool` → show "Calculated by {tool}" with the `arguments`, and `result` (the tool's output,
  re-run now) as collapsible JSON; for `recommend_orders`, `result.rows` is a table-friendly list.
- `error` is non-null if the evidence is gone (e.g. data was reset) — show it instead.

## 6. Human-in-the-loop: approve / reject

Two tools need approval: **`save_report`** (`input: {title, content}`), used when the user asks to
save/store/share something, and **`create_purchase_order`** (`input: {sku, location_id, qty,
order_date, reason}`), used when the user asks the agent to place orders.

1. The stream sends `approval_required` with `run_id`, `tool` and `input`, then **pauses**.
2. Show the tool and its input (title + content) with **Approve** and **Reject** buttons and an
   optional reason field.
3. Call:

```ts
await fetch(`${BACKEND_URL}/runs/${runId}/approve`, {   // or /reject
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
  body: JSON.stringify({ reason: "Looks right" }),
});
```

4. The stream continues with a `step` of `type: "approval"`, then the agent's final answer.
   Rejected actions are not run, and the agent says so.

- **404** = the run is not waiting (already decided, or timed out). Disable the buttons.
- No decision within **10 minutes** = treated as rejected (`decision: "timeout"`).
- `GET /approvals` lists everything waiting (useful after a page reload).

## 7. Uploads

```ts
const form = new FormData();
form.append("file", file);                               // .csv, .xlsx, .xls -> /upload/table
// optional, tables only: what columns mean (keys = original header or snake_case name)
form.append("column_descriptions", JSON.stringify({ "Revenue (NOK)": "Revenue excl. VAT" }));
const res = await fetch(`${BACKEND_URL}/upload/table`, {  // or /upload/pdf for .pdf
  method: "POST", headers: { "X-API-Key": API_KEY }, body: form,   // don't set Content-Type
});
```

- Table upload → `{tables: [{table, rows, columns: [{name, original_header, comment}]}]}`.
  One table per CSV or per Excel sheet. Re-uploading the same file name replaces it.
- PDF upload → `{document_name, pages, chunks}`. Takes a few seconds (embeddings).
- Uploaded data is available to the agent in the very next question.

## 8. Per-claim review: accept / reject a claim

A reviewer accepts or rejects each **claim** — each `[n]` in the answer — on its own.

```ts
await fetch(`${BACKEND_URL}/runs/${runId}/claims/${marker}/review`, {   // marker = the n in [n]
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
  body: JSON.stringify({ decision: "accepted", reviewer: "Ana", comment: "matches page 1" }),
});
// -> { run_id, marker, review: { id, created_at, marker, decision, reviewer, comment,
//      claim, source_kind, document_name, page_number, chunk_id, table_name, sql_query,
//      tool_name, tool_arguments } }
```

- **One numbering:** the URL takes the marker number, 1-based, exactly as written in the text.
  `[3]` → `/claims/3/review`. (`index` in `/sources` is only the 0-based list position.)
- `decision`: `"accepted"` or `"rejected"`; `reviewer` and `comment` are optional.
- Each review stores a **snapshot** of the claim text and its reference (document + page + chunk,
  table + SQL, or tool + arguments), so the audit record stands on its own even if the run changes later.
- Reviewing again adds a new review; the newest is the current decision (`/sources` returns it as
  `review`). History is kept in the database.
- `GET /runs/{run_id}/sources` → `summary: {total, accepted, rejected, unreviewed}`; `total` is the
  number of claims. Progress bar: "{accepted} of {total} claims verified".
- 404 = unknown run, or marker out of range (markers start at 1). 422 = bad `decision`.

## 9. Inventory dashboard (Høyden case)

No LLM here: plain SQL plus a deterministic order planner (Python). Results are cached for
5 minutes (cleared on upload). The first call after the backend wakes up takes a few seconds.

**`GET /insights/summary`** → four KPI cards, each `{value, definition, inputs}`:

| Key | Value |
|---|---|
| `total_stock_value_nok` | stock on hand now at purchase price |
| `waste_last_90_days_nok` | written-off stock at purchase price, last 90 days |
| `stockout_rate_last_90_days` | **fraction** (0.0357 = 3.6 %) of product-days that sold out |
| `capital_in_overstock_nok` | stock value of items with > 6 weeks of cover; `inputs.capital_releasable_nok` = the part above 6 weeks |

Plus `plan_totals: {horizon_days, with_recommendation, without_recommendation, overstock_items,
capital_releasable_nok}`, where both scenarios are `{capital_tied_nok, lost_sales_nok, waste_nok}`
over the horizon (8 weeks by default).

**`GET /insights/recommendations`** → `{today, horizon_days, recommendations: [...], overstock: [...]}`.
`recommendations` = top 30 sku × location rows by `nok_impact`; `overstock` = every row with
`overstock: true`, by `capital_releasable_nok`. Each row:

```json
{
  "sku": "SKU009", "product_name": "Røkt laks 300 g", "category": "Fisk og sjømat",
  "location_id": "L04", "location_name": "Høyden Nettbutikk", "supplier_name": "Nordfjord Sjømat",
  "action": "order",                      // order | reduce (overstock) | ok
  "days_of_cover": 0.0,                   // on hand / forecast daily demand; null = no expected demand
  "stockout_risk_date": "2026-11-11",     // with current stock + open orders only; null = none
  "expected_waste_units": 0.0, "expected_waste_nok": 0,
  "next_order": {"order_date": "2026-11-06", "arrival_date": "2026-11-08", "qty": 40},
  "recommended_orders": [{"order_date": "...", "arrival_date": "...", "qty": 10}],
  "with_recommendation":    {"capital_tied_nok": 5342, "lost_sales_nok": 0,     "waste_nok": 0, "lost_units": 0, "waste_units": 0},
  "without_recommendation": {"capital_tied_nok": 1713, "lost_sales_nok": 49235, "waste_nok": 0, "lost_units": 330.4, "waste_units": 0},
  "overstock": false, "capital_releasable_nok": 0,
  "nok_impact": 49235,                    // lost sales avoided + waste avoided + capital releasable
  "inputs": {                             // the "why"
    "on_hand_units": 0, "on_order_units": 70, "open_orders": [...], "batches": [{"expiry_date": "...", "units": 11}],
    "unit_cost_nok": 74, "price_nok": 149, "shelf_life_days": 14, "pack_size": 10,
    "lead_time_days_avg": 2, "lead_time_days_sd": 1, "order_weekdays": [0, 2, 4],
    "trend_factor": 1.091, "last_8_weeks_units": 675, "same_8_weeks_last_year_units": 618,
    "forecast_daily_avg": 19.92, "forecast_weekly_units": [79.6, 69.8, 87.5, 112.0, 185.4, 233.3, 244.7, 103.5],
    "stockout_days_in_history": 30, "promo_uplift": 1.34, "planned_promo_days": ["2026-12-01", "..."],
    "safety_stock_units": 39.8, "business_as_usual_orders": 23, "business_as_usual_units": 760
  },
  "decision": null                        // latest POST /orders for this sku + location + next_order.order_date
}
```

"Without" = business as usual (open orders + the same orders as the same weeks last year);
"with" = the recommended orders. `order_weekdays`: 0 = Monday.

**Approve / reject a recommendation** (writes a row in `proposed_orders`):

```ts
await fetch(`${BACKEND_URL}/orders`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
  body: JSON.stringify({ sku: row.sku, location_id: row.location_id, qty: row.next_order.qty,
                         order_date: row.next_order.order_date, decision: "approved",
                         reason: "Christmas demand", decided_by: "Ana" }),
});
// -> { order: { id, created_at, sku, location_id, qty, order_date, reason, status, source: "dashboard", decided_by } }
```

400 = unknown sku/location; 422 = qty ≤ 0 or bad date/decision.

---

## Ready-to-paste Lovable prompt

Replace the two placeholders first.

````text
Build a single-page web app called "Agent Workbench" (React + TypeScript + Tailwind + shadcn/ui).
It is a chat UI for an existing AI agent backend. Do NOT use Supabase or any database in the
frontend; everything goes through the HTTP API below. Do not invent endpoints or fields.

CONFIG (put in src/config.ts):
  export const BACKEND_URL = "{{BACKEND_URL}}";
  export const API_KEY = "{{APP_API_KEY}}";
Every request except GET /health must send the header "X-API-Key: API_KEY".

LAYOUT
- Top bar: app name, a backend status pill, and a mode toggle "Single agent" / "Orchestrator".
- Left (about 60%): the chat. Right (about 40%): a "Live trace" panel for the current run.
- Under the chat input: a paperclip upload button and a list of uploaded files.

BACKEND STATUS
- On load, call GET {BACKEND_URL}/health (it can take up to 60 s because the server sleeps).
  Show "Waking up backend…" with a spinner until it answers, then a green pill
  "Online · {llm_model}" if status is "ok" and database is "ok", otherwise a red pill.

SENDING A MESSAGE
- POST {BACKEND_URL}/agent/stream with JSON:
  { "task": <user message>, "mode": "agent" or "orchestrator",
    "output_schema": { "type": "object", "required": ["answer"],
      "properties": { "answer": { "type": "string", "description": "The answer in Markdown" } } } }
- The response is a Server-Sent Events stream. Read it with fetch + response.body.getReader()
  (NOT EventSource). Events are separated by a blank line; each has "event: <name>" and
  "data: <one line of JSON>". Use exactly this parser:

  async function streamAgent(body, onEvent, signal) {
    const res = await fetch(`${BACKEND_URL}/agent/stream`, { method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify(body), signal });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const reader = res.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        let event = "message", data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (data) onEvent(event, JSON.parse(data));
      }
    }
  }

- Show a "Stop" button while streaming (AbortController).

STREAM EVENTS
- "run_started": data { run_id, agent }. Remember run_id; clear the trace panel.
- "step": data is one trace entry with fields step, type, agent. Append it to the live trace:
    type "llm": "🧠 Thinking" + the names in data.tool_calls[].name (or "writing answer" if empty) + data.ms ms.
    type "tool": "🔧 {data.tool}". If data.input.sql exists show it in a code block; if
      data.input.query exists show it as a search query; if data.input.question exists show it
      as "asked: …". Show data.chunk_ids as small chips if present. Show data.error in red if
      not null. data.output is a JSON string (max 2000 chars): show it collapsed, expandable.
    type "delegation_started": "➡️ Asking {data.specialist}: {data.question}".
    type "approval": "✅/❌ {data.decision} by human: {data.reason}".
    type "final_answer": if data.valid is false show "Answer rejected, retrying" + data.errors.
  Entries whose data.agent is "data_analyst" or "document_researcher" are specialist steps:
  indent them and show a coloured agent badge, so the trace looks like a delegation tree.
- "approval_required": data { run_id, tool, input }. The run is PAUSED. Show an amber card in
  the chat: "The agent wants to run {tool}". For tool "save_report" show input.title and
  input.content (Markdown). Add an optional "Reason" text field and two buttons:
    Approve → POST {BACKEND_URL}/runs/{run_id}/approve   body { "reason": <text> }
    Reject  → POST {BACKEND_URL}/runs/{run_id}/reject    body { "reason": <text> }
  (both with headers Content-Type: application/json and X-API-Key). After a click, disable the
  buttons and show the decision. A 404 means the run is no longer waiting: disable the buttons
  and say "This request expired".
- "done": data { run_id, answer, stopped_reason, trace }. data.answer is an object:
  render data.answer.answer as Markdown in an assistant bubble. If stopped_reason is
  "max_steps" add a small warning "The agent stopped before finishing".
  Then load the sources (below).
- "error": data { error }. Show it as an error bubble.

CLICKABLE SOURCES
- data.answer.answer contains inline markers [1], [2]... right after each claim; [n] is the
  1-based position in data.answer.sources, and each marker appears exactly once (one marker =
  one claim; "[1][2]" = one claim with two sources). Render each marker as a clickable
  superscript that opens that source's side sheet (below).
- data.answer.sources is a list. Each item is EITHER a document source
  { claim, document_name, page_number, chunk_id } OR a database source { claim, table, sql }.
- After "done", call GET {BACKEND_URL}/runs/{run_id}/sources. It returns
  { sources: [ ... ] } in the same order, each with "index" and "kind":
    kind "document": + text (original page text), error
    kind "database": + columns, rows (list of objects), row_count, truncated, error
- Under the answer, show numbered source chips: "📄 {document_name} · p.{page_number}" or
  "📊 {table}". Hovering a chip shows its claim.
- Clicking a chip opens a side sheet:
    document: title "{document_name} — page {page_number}", the claim, then the page text in a
      quote block with the claim's key numbers highlighted if they appear in the text.
    database: title "Table {table}", the claim, a data table of rows with columns as headers,
      "showing {row_count} rows" (+ "truncated" if true), and the SQL in a collapsible code block.
  If error is not null, show the error instead.

FILE UPLOAD
- The paperclip accepts .csv, .xlsx, .xls and .pdf (max 20 MB).
- .pdf → POST {BACKEND_URL}/upload/pdf ; others → POST {BACKEND_URL}/upload/table.
  Send multipart FormData with field "file". Send only the X-API-Key header (do NOT set
  Content-Type yourself).
- Show a progress spinner, then a toast:
    pdf: "Indexed {document_name}: {pages} pages, {chunks} chunks"
    table: "Loaded table {tables[0].table} ({tables[0].rows} rows)" (one line per table)
  and add the file to the uploaded-files list. On error show the response "detail" text.

ERRORS
- 401 → "Wrong API key". Other non-2xx → show the "detail" field from the JSON body.

STYLE
- Clean, professional, light theme, dark navy accents; readable at a projector.
- Example prompts as clickable suggestions on the empty chat:
  "Which regions met the Q1 2026 margin target, and why did the others miss it?"
  "Total revenue per product per month"
  "Summarise the uploaded report and save it as a report"
````

---

## Ready-to-paste prompt: inline citations + claim review (UI change)

For an app already built from the prompt above. Paste as a follow-up message in Lovable:

````text
Add inline citations and per-claim review to the assistant answers. Use only the API below.

1. CLAIM SPANS AND MARKERS
- The answer text (data.answer.answer from the "done" event) contains markers like [1], [2]
  right after each claim. [n] is 1-based: [1] = data.answer.sources[0]. Each marker appears
  exactly once: one marker = one claim. "[1][2]" right after one claim = one claim with two sources.
- Split the answer text into plain text and claim spans with EXACTLY this function:

// Split answer text into plain text and claim spans.
// A claim span runs from the end of the previous marker (or the start of the
// sentence / line, whichever is later) up to its marker(s). "[1][2]" = one claim, two sources.
function splitClaims(text) {
  const parts = [];                                   // {type: "text"|"claim", text, markers?}
  const group = /(?:\[\d+\])+/g;                      // one or more adjacent markers
  let last = 0, m;
  while ((m = group.exec(text))) {
    const before = text.slice(last, m.index);
    // the claim starts after the last sentence end or line break in `before`
    let start = 0;
    for (const b of before.matchAll(/[.!?](?=\s)\s*|\n/g)) start = b.index + b[0].length;
    // keep joiners, list bullets and spaces outside the highlight
    const lead = before.slice(start).match(/^[\s,;:]*(?:(?:and|but|while)\s+)?(?:[-*•]\s+|\d+\.\s+)?/i)[0];
    const claimStart = start + lead.length;
    if (claimStart > 0) parts.push({ type: "text", text: before.slice(0, claimStart) });
    const markers = m[0].match(/\d+/g).map(Number);
    parts.push({ type: "claim", text: before.slice(claimStart), markers });   // keeps the space before the marker
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ type: "text", text: text.slice(last) });
  return parts;
}

- Render the parts in order: "text" parts as normal text; "claim" parts as a clickable span
  (subtle dotted underline) followed by one small superscript badge per number in markers.
  Apply this to the text of each paragraph and list item (keep Markdown for headings, bold and
  lists; use react-markdown with custom p/li renderers that call splitClaims on their text).
- Clicking a claim span or one of its badges opens the existing source side sheet for that
  marker's source (for "[1][2]" the badge decides which; the span opens the first).

2. REVIEW STATE AND HIGHLIGHTING
- After "done", and after every review, call GET {BACKEND_URL}/runs/{run_id}/sources (with the
  X-API-Key header). It returns { summary: {total, accepted, rejected, unreviewed}, sources: [...] }.
  Each source has: marker (1-based, the [n] in the text), kind, claim, the evidence (text for
  documents; columns + rows + sql for database), error, and review (null, or
  {decision: "accepted" | "rejected", reviewer, comment, created_at, ...}).
- Highlight each CLAIM SPAN (not the whole sentence) by the review of its marker(s):
    accepted -> green background, rejected -> red background, no review -> no highlight.
  For a span with several markers: red if any is rejected, green only if all are accepted.
  A sentence with two claims can therefore show one green and one red span.
- Badges get the same colour as their own marker's review.

3. ACCEPT / REJECT IN THE SOURCE SHEET
- At the top of the sheet show the claim being reviewed ("Claim [3]: <claim text>").
- At the bottom add: an optional "Comment" textarea, a "Reviewer" text field (remember its value
  in localStorage), and two buttons "Accept" (green) and "Reject" (red).
- Clicking sends POST {BACKEND_URL}/runs/{run_id}/claims/{marker}/review with headers
  Content-Type: application/json and X-API-Key, body
  { "decision": "accepted" or "rejected", "reviewer": <text or omit>, "comment": <text or omit> }.
  {marker} is the marker number exactly as shown in the text ([3] -> /claims/3/review).
- On success, show a toast "Claim [marker] accepted/rejected", then reload /sources to refresh
  the highlights and the progress bar. If the claim already has a review, show it in the sheet
  ("Rejected by Ana: wrong quarter") and let the user change it (a new review replaces it).

4. PROGRESS BAR
- Above each assistant answer that has sources, show a thin progress bar and the text
  "{summary.accepted} of {summary.total} claims verified", plus "· {summary.rejected} rejected"
  in red if rejected > 0. Green fill = accepted share, red segment = rejected share.
````


## Ready-to-paste prompt: Dashboard tab (inventory case)

Paste into the existing Lovable project. It reuses the existing config (BACKEND_URL, API key).

````text
Add a second tab "Dashboard" next to the chat (keep everything that exists; reuse BACKEND_URL,
API_KEY and the fetch helper from src/config.ts; every request sends header X-API-Key). No Supabase.
Do not invent endpoints or fields.

DATA
- GET {BACKEND_URL}/insights/summary -> { today, kpis, plan_totals }
  kpis has four entries, each { value, definition, inputs }:
  total_stock_value_nok, waste_last_90_days_nok, stockout_rate_last_90_days (a FRACTION: 0.0357 = 3.6 %),
  capital_in_overstock_nok (inputs.capital_releasable_nok = the part above 6 weeks of cover).
  plan_totals = { horizon_days, with_recommendation, without_recommendation, overstock_items,
  capital_releasable_nok }; both scenarios are { capital_tied_nok, lost_sales_nok, waste_nok }.
- GET {BACKEND_URL}/insights/recommendations?limit=30 -> { today, horizon_days, recommendations, overstock }
  Each row: sku, product_name, category, location_id, location_name, supplier_name,
  action ("order" | "reduce" | "ok"), days_of_cover (number or null), stockout_risk_date (date or null),
  expected_waste_nok, next_order ({order_date, arrival_date, qty} or null), recommended_orders [...],
  with_recommendation and without_recommendation ({capital_tied_nok, lost_sales_nok, waste_nok}),
  overstock (bool), capital_releasable_nok, nok_impact, inputs (object: the numbers behind the
  recommendation), decision (null or {status: "approved"|"rejected", decided_by, created_at}).
- POST {BACKEND_URL}/orders with JSON { sku, location_id, qty, order_date, decision: "approved" | "rejected",
  reason?, decided_by? } -> { order }. 400 = unknown sku/location.
- The backend may be asleep: the first call can take up to 60 s. Show skeletons, and a "waking up
  the backend…" note after 5 s. Show errors inline with a Retry button.

LAYOUT
1. Header: "Inventory — today {today}, plan for the next {horizon_days} days", and a Refresh button.
2. KPI cards (4): Stock value, Waste last 90 days, Stockout rate (value × 100, one decimal, "%"),
   Capital in overstock (subtitle "NOK {capital_releasable_nok} could be released"). Format NOK as
   "NOK 393 306" (space thousands separator, no decimals). Each card has an info icon; its tooltip
   shows `definition` and the `inputs` as key: value lines.
3. "With vs without the recommendations" strip from plan_totals: three small comparisons
   (capital tied up, lost sales, waste): without → with, and the difference in green when lower.
4. Recommendations table (rows from recommendations): Product (product_name + sku, small),
   Location, Action badge (order = blue, reduce = amber, ok = grey), Days of cover (1 decimal, "—" if null),
   Stockout risk (date, red if within 7 days of today), Next order ("{qty} on {order_date}, arrives {arrival_date}"
   or "—"), Lost sales avoided (without.lost_sales_nok − with.lost_sales_nok), Waste avoided
   (without.waste_nok − with.waste_nok), NOK impact (bold), and a Decision column:
   - if next_order is null: "—"
   - if decision is set: a badge "Approved"/"Rejected" (+ decided_by), with a small "change" link
   - else: Approve and Reject buttons. Reject opens a small input for an optional reason.
     Both POST /orders with qty and order_date from next_order, decided_by from a "Your name" input in
     the tab header (remembered in localStorage). After success, refetch recommendations.
   Sortable by NOK impact (default), days of cover and stockout risk. Filter chips by location.
5. Row click expands a "Why" panel: forecast_weekly_units as a small bar chart (week 1..n),
   trend_factor ("+9.1 % vs last year"), last_8_weeks_units vs same_8_weeks_last_year_units,
   on_hand_units, on_order_units and open_orders, batches with expiry dates, lead_time_days_avg ± sd,
   order_weekdays as weekday names (0 = Monday), shelf_life_days, pack_size, safety_stock_units,
   promo_uplift and planned_promo_days, business_as_usual_orders/units, and every recommended order
   in recommended_orders. Then the with/without numbers side by side.
6. Overstock list (rows from overstock), sorted by capital_releasable_nok: Product, Location,
   Days of cover ("no expected demand" if null), Stock value (inputs.on_hand_units × inputs.unit_cost_nok),
   Capital releasable (bold). Small note: "More than 6 weeks of cover. Releasable = stock above 6 weeks."

In the chat tab, sources can now also be a third kind with a `tool` key: {claim, tool, arguments}.
Show it as "Calculated by {tool}" with the arguments; in GET /runs/{id}/sources such a source has
`kind: "tool"` and `result` (show as collapsible JSON). Approval requests can now also be for the tool
create_purchase_order (input: sku, location_id, qty, order_date, reason): show these fields in the
approval card.

Check: the Dashboard shows four KPI cards with numbers, about 30 recommendation rows with smoked
salmon (Røkt laks) near the top, and an overstock list with balsamico and flaky salt. Approving a row
turns its buttons into an "Approved" badge that stays after Refresh.
````
