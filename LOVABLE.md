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
| GET | `/health` | – | `{status, llm_provider, llm_model, database}` |
| POST | `/agent/stream` | JSON `RunRequest` (below) | **SSE stream** — the main endpoint for a chat UI |
| POST | `/agent/run` | JSON `RunRequest` | `{run_id, answer, stopped_reason, trace}` when finished (no live steps) |
| GET | `/agent/stream` | `?task=&mode=&api_key=` | SSE stream for `EventSource` (no `output_schema`) |
| GET | `/runs/{run_id}` | – | the stored run: `{id, task, status, answer, trace, agent_name, parent_run_id, …}` |
| GET | `/runs/{run_id}/sources` | – | the answer's sources **resolved**: page text / table rows (section 5) |
| GET | `/runs/{run_id}/tree` | – | delegation tree `{id, agent_name, task, status, duration_ms, children: [...]}` |
| GET | `/approvals` | – | `{pending: [{run_id, tool, input, waiting_seconds}]}` |
| POST | `/runs/{run_id}/approve` | `{reason?: string}` | `{run_id, decision: "approved"}` |
| POST | `/runs/{run_id}/reject` | `{reason?: string}` | `{run_id, decision: "rejected"}` |
| POST | `/upload/table` | multipart: `file` (.csv/.xlsx/.xls), `column_descriptions?` (JSON string) | `{tables: [{table, rows, comment, columns}], unused_column_descriptions}` |
| POST | `/upload/pdf` | multipart: `file` (.pdf) | `{document_name, pages, chunks}` |

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
| `tool` | `tool`, `input`, `output` (string, max 2000 chars), `error`, `ms`; `chunk_ids` for `search_documents`; `specialist`, `child_run_id`, `child_trace` for delegations | tool name + input (show `input.sql` as code, `input.query` as text) |
| `approval` | `tool`, `decision` (`approved` \| `rejected` \| `timeout`), `reason`, `waited_ms` | the human decision |
| `final_answer` | `input`, `valid`, `errors` | if `valid` is false: "answer rejected, retrying" + `errors` |
| `delegation_started` | `specialist`, `child_run_id`, `question` | orchestrator mode: "Asking data_analyst: …"; that specialist's own steps follow (with their own `agent` and `run_id`) |

Indent steps whose `agent` is a specialist under the orchestrator.

## 4. Answers and the `sources` format

With an `output_schema`, `answer` looks like this (your fields + `sources`):

```json
{
  "answer": "Only Oslo met the 45% target (46.3%). Bergen (41.8%) and Trondheim (38.9%) missed it...",
  "sources": [
    {"claim": "The target is at least 45% gross margin for every office",
     "document_name": "nordvik_q1_2026_report.pdf", "page_number": 1,
     "chunk_id": "nordvik_q1_2026_report.pdf:p1:c0"},
    {"claim": "Actual Q1 2026 margins by office",
     "table": "sample_sales",
     "sql": "select region, round(100*(sum(revenue)-sum(cost))/sum(revenue),2) as margin from sample_sales ..."}
  ]
}
```

Two kinds of source — tell them apart by which key exists:

- **Document**: has `chunk_id` → `{claim, document_name, page_number, chunk_id}`
- **Database**: has `sql` → `{claim, table, sql}`

The backend has already checked that every cited chunk was really found by a search in this
run (same document and page), and every SQL really ran. One claim can have several entries.

## 5. Clickable sources: `GET /runs/{run_id}/sources`

Call this after `done`. It returns the same sources, in the same order (`index`), with the evidence:

```json
{
  "run_id": "b9f8fc9e-...",
  "sources": [
    {"index": 0, "kind": "document", "claim": "...", "document_name": "nordvik_q1_2026_report.pdf",
     "page_number": 1, "chunk_id": "nordvik_q1_2026_report.pdf:p1:c0",
     "text": "Nordvik Advisory - Q1 2026 Management Report ...", "error": null},
    {"index": 1, "kind": "database", "claim": "...", "table": "sample_sales", "sql": "select ...",
     "columns": ["region", "revenue", "cost", "margin_pct"],
     "rows": [{"region": "Bergen", "revenue": 710000.0, "cost": 413000.0, "margin_pct": 41.83}],
     "row_count": 3, "truncated": false, "error": null}
  ]
}
```

- `document` → show `text` (the original page text) with "document_name — page N".
- `database` → show `rows` as a table with `columns` as headers, plus the `sql` (collapsible).
- `error` is non-null if the evidence is gone (e.g. data was reset) — show it instead.

## 6. Human-in-the-loop: approve / reject

Only one tool needs approval today: **`save_report`** (`input: {title, content}`), used when the
user asks to save/store/share something.

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
