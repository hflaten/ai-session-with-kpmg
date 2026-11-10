# ai-session-with-kpmg

Task-agnostic AI agent backend for a KPMG hackathon case. A Lovable frontend
calls this FastAPI backend over HTTP; the backend runs LLM agents with tools
over data stored in Supabase (Postgres + pgvector).

## Architecture

```
Lovable frontend ──HTTP/SSE──▶ FastAPI (Render) ──▶ LLM (Anthropic | OpenAI-compatible)
                                    │
                                    └──▶ Supabase Postgres (+ pgvector)
                                          ├─ main role (backend writes: runs, uploads)
                                          └─ agent_readonly role (query_database tool)
```

| Layer | Files | Idea |
|---|---|---|
| Config | `app/config.py` | All settings from env vars. `mask()` for printing secrets. |
| DB | `app/db.py` | `connect(readonly=False)`. Read-only URL only for the SQL tool. |
| LLM adapters | `app/llm/` | One `call_llm()`; tools defined once (name, description, input schema); adapters translate to Anthropic / OpenAI format. `LLM_PROVIDER` picks one. |
| Agent loop | `app/agent/loop.py` | `run_agent(system_prompt, tools, task)`: call LLM → run tools → repeat, max 10 steps. Returns answer + trace. |
| Run service | `app/agent/service.py` | `execute()`: logs the run, builds prompt + tools, wires approvals. Used by `/agent/run` and `/agent/stream`. |
| Tools | `app/tools/` | `query_database` (read-only SQL), `search_documents` (pgvector), `recommend_orders` (deterministic order plan), `save_report` + `create_purchase_order` (need approval). |
| Order planner | `app/inventory/` | **No LLM**, pandas + plain Python (`planner.py` = the maths, `data.py` = loads the case tables as `agent_readonly`). Per sku × location: demand history with lost sales added back on stockout days (in-stock days ±10 days × weekday index); forecast = same day last year (−364 d) × trend (last 8 weeks / same 8 weeks last year, clipped 0.5–2.5), last year's promotion days ÷ uplift, planned promotions × uplift; stores closed Sundays + Norwegian holidays. Day-by-day FIFO stock simulation (deliveries, expiry → waste, unmet demand → lost sales) in three scenarios: no new orders (days of cover, stockout risk date, waste from current batches), **without** = business as usual (open orders + the same orders as the same weeks last year), **with** = recommended orders (on supplier order weekdays, not holidays: units short until the next possible delivery + safety stock z=1.65, capped at what sells within shelf life, whole packs). Overstock = > 6 weeks of cover; releasable = stock above 6 weeks. `nok_impact` = lost sales avoided + waste avoided + releasable capital. "Today" = day after the last `sales_daily` date. Cached 5 min (`app/tools/inventory.py`), cleared on upload. |
| Business context | `app/business_context.md` | Case background added to every system prompt (HTML comments stripped). Now: Høyden README without the "fasit" section. Replace/empty it for a new case. |
| Structured output | `app/agent/structured.py` | Final answer is a `submit_answer` tool call whose schema is the requested JSON schema + a required `sources` list. Citations are checked against what the agent actually found this run. Works on both providers. |
| Streaming | `app/routes/` | SSE, one event per agent step. Every run logged to `agent_runs`. |
| Ingest | `app/ingest/` | CSV/Excel → own table via pandas, with table + column comments; PDF → per-page chunks → embeddings → `document_chunks`. Report-style sheets are cleaned first by `table_layout.clean_sheet()`: finds the real header row (top 15 rows), combines two-row headers (group label fills right), drops empty rows/columns, skips rows labelled Total/Sum, moves footnotes to the table comment, turns stacked or side-by-side tables into a `section` column, converts mostly-numeric columns ("45.9%" → 0.459, "-" → empty). Tidy CSVs (header in row 1, no totals/footnotes/repeated headers/"%" names) skip `clean_sheet` (`read_tidy_csv`, much less memory: the free Render instance has 512 MB and crashed on a 15k-row file). Rows are written with Postgres `COPY` (`write_table`), with explicit column types, in one transaction with the comments. |
| Human-in-the-loop | `app/agent/approvals.py` | Tools flagged `requires_approval` (`save_report`, `create_purchase_order`) pause the run until `/runs/{id}/approve` or `/reject` (10 min timeout = rejected). The request is registered *before* it is announced, so an instant click is never lost. Held in memory → single worker. |
| Orchestrator | `app/agent/orchestrator.py` | `mode: "orchestrator"`. Specialists run in parallel (the loop runs several plain tool calls from one response in threads, recording them in call order; trace entries have `parallel: true`). No direct data access: its tools `ask_data_analyst` (query_database + recommend_orders, sees the tables) / `ask_inventory_planner` (recommend_orders only, business context only) each run `run_agent` for a specialist (own prompt, own tools, only its own context). The orchestrator itself has the approval-gated write tools `create_purchase_order` and `save_report`. (`document_researcher` was replaced by `inventory_planner` for the Høyden case; agent mode still has `search_documents`.) The delegation entry nests the specialist's trace (`child_trace`); `agent_runs.parent_run_id` links the runs; `GET /runs/{id}/tree` rebuilds the tree. Specialists return `chunks_found` + `sql_run` + `tools_run`, so the orchestrator's citations are checked too. |

### Database (supabase/migrations)

- `agent_runs`: id, created_at, parent_run_id, agent_name, task, provider, model,
  status (running | awaiting_approval | completed | failed), answer, trace, error, duration_ms.
- `sample_sales`: 24 rows of made-up demo data (dropped by the 2026-11-06 reset; the live DB tests now create their own `zz_test_sales`).
- `saved_reports`: id, created_at, run_id, title, content. Written only by `save_report` after a human
  approves. RLS on, with a read policy for `agent_readonly`. Kept by the reset script.
- `claim_reviews`: id, created_at, run_id (cascade), source_index (0-based) + generated `marker` (1-based), decision
  (accepted | rejected), reviewer, comment, plus a **snapshot** of the claim: claim, source_kind, document_name,
  page_number, chunk_id, table_name, sql_query. Newest row per claim is the current decision. Backend only; emptied by the reset script.
- `tool_calls`: call_id (pk), created_at, run_id (cascade), tool, arguments, output (full jsonb). Backend only.
  Emptied with `--include-runs`.
- `proposed_orders`: id, created_at, run_id (set null), sku, location_id, qty (> 0), order_date, reason,
  status (approved | rejected), source (agent | dashboard), decided_by. Written by `create_purchase_order`
  after approval, or by `POST /orders` from the dashboard. RLS on, read policy for `agent_readonly`. Emptied by the reset script.
- `document_chunks`: chunk_id (`file:p<page>:c<index>`), document_name, page_number, chunk_index,
  text, embedding `vector(1536)` with an HNSW cosine index. Not readable by `agent_readonly`;
  `search_documents` reads it with a fixed query. Re-uploading a file name replaces its chunks.
- Uploaded tables: one per CSV / Excel sheet, named after the file (snake_case). Re-upload replaces.
  Types are tidied: "True"/"False" → boolean, whole numbers → bigint, all-midnight timestamps → date;
  "0,2,4" stays text (only `1,234`-style commas are thousands separators).
- Case data (Høyden Matmarked, KPMG case 4): `data/hoyden/*.csv` (gitignored), loaded with
  `.venv/bin/python -m scripts.load_hoyden <BACKEND_URL>` through `/upload/table`, with column descriptions
  written from its README. Tables: locations, suppliers, products, calendar, promotions, sales_daily,
  sales_weekly, inventory_weekly, inventory_current, inventory_batches_current, purchase_orders, waste.
- `agent_readonly` role: `SELECT` on `public` (including future tables, e.g. uploads),
  `default_transaction_read_only = on`, `statement_timeout = 10s`, no access to `agent_runs`.
  Its password is not in the migration; set it with `scripts/set_readonly_password.py`.
- The `public` schema is revoked from `anon` / `authenticated`, so the anon key held by
  Lovable cannot read or write tables through Supabase's REST API. Only the backend touches data.

### RAG requirements (step 7) — agreed with the user

- **Each pgvector row stores:** `chunk_id`, `document_name`, `page_number`, `chunk_index`,
  the original chunk `text`, and the `embedding`.
- **`search_documents` returns** `chunk_id`, `document_name`, `page_number` and `text` per hit —
  never text alone.
- **Every search is logged in the agent trace** with the `chunk_id`s it returned.
- **Final structured answers include a `sources` field** listing `document_name`, `page_number`
  and `chunk_id` for every claim.
- **Uploaded tables get comments:** when loading a CSV/Excel file, add a Postgres comment on
  the table and on **every column** describing it, so the agent sees them in its prompt.
- **Tool claims** (inventory case): `{claim, tool, call_id, row: {sku, location_id} | "summary", fields: [dot paths]}`
  for deterministic tools in `CITABLE_TOOLS` (`recommend_orders`). Every tool call gets a `call_id` (`rc-xxxxxx`,
  `app/agent/tool_calls.py`); citable tools see it in their result. Full outputs are kept in memory and stored in
  the `tool_calls` table (service.py `tool_output_saver`), so evidence can always be resolved.
  - Hard check (`citation_errors`): the call_id ran in this run (own call, or a specialist's `tools_run`) and is that tool.
  - Soft checks (`evidence_errors`): the row exists in that call's stored output, the fields exist, and every number and
    date in the source's `claim` **and** in the claim span in the answer text (text before its markers, like
    `splitClaims`) matches a cited value (`app/agent/numbers.py`: rounding to the precision shown, k/m, fraction→%,
    factor→% change, the difference of two cited values; digits inside names like "Røkt laks 300 g" and years ignored).
    Soft failures go back to the model; after 2 rejections the answer is accepted with `warnings` (also on the trace entry).
  - `/sources` resolves tool sources from the stored output: `product`, `location`, `cited_fields`, `why`, `row_data`.
    Reviews snapshot `tool_name`, `tool_arguments` and `tool_evidence` `{call_id, row, fields: [{field, value}]}`.
  - Specialists answer with `output_schema` (checked sources); the orchestrator copies their sources.
- **Database claims are cited too** (user's decision): `{claim, table, sql}`. The sql must be a
  query that ran successfully with query_database in this run, and the table must appear in it.
- **Column comments on upload:** a generated summary (original header, counts, range, examples),
  replaced by the caller's text where `column_descriptions` gives one.
- **Status:** built and approved process followed (schema + sources format approved first).

**Sources format** (one entry per claim–evidence pair):

```json
"sources": [
  {"claim": "...", "document_name": "report.pdf", "page_number": 1, "chunk_id": "report.pdf:p1:c0"},
  {"claim": "...", "table": "sample_sales", "sql": "select ..."}
]
```

**Inline markers** (`marker_errors`): the answer text must cite with `[n]` right after each claim, `n` = 1-based
position in `sources`. Every marker must map to a source, every source must be marked, and **each marker
appears exactly once** (one marker = one claim; two claims on the same evidence = two `sources` entries;
`[1][2]` = one claim, two sources). Each claim is a single sentence or clause with its marker right after it;
`unmarked_fact_errors` rejects any sentence containing a digit (number, %, date) without a marker (headings and
list numbering ignored; spelled-out numbers not detected). Markers are read from all text fields except `sources`.
Failures go back to the model.

**Citation checks** (`citation_errors` in `app/agent/structured.py`): a `chunk_id` must have been
returned by `search_documents` in this run with the same document and page; a `sql` must match
(ignoring whitespace, case, trailing `;`) a successful `query_database` call in this run, and the
`table` must appear in it as a whole word. Failures go back to the model, like schema errors.

Test document: `tests/fixtures/nordvik_q1_2026_report.pdf` (fictional, made by
`scripts/make_test_pdf.py`), designed to combine with `sample_sales`.

## Rules

- Secrets only from env vars. Never print secrets in full (use `mask()`).
- The service role key and DB URLs never leave the backend; no endpoint returns them.
- All schema changes are migration files in `supabase/migrations`, applied with
  `supabase db push --remote` (without `--remote` the CLI targets a local Docker stack).
- Simple, readable code — the user has to explain it.
- Test each step; commit and push after each working step.

## Endpoints (for the Lovable frontend)

Frontend integration guide + ready-to-paste Lovable prompt: **`LOVABLE.md`**.

**Auth:** every endpoint except `/health` needs header `X-API-Key: <APP_API_KEY>`
(`app/auth.py`). For `EventSource`, which can't set headers, use `?api_key=...` instead.
This only stops casual abuse — the token is visible in the frontend code. Rotate it
(and the LLM keys) after the hackathon. If `APP_API_KEY` is unset the API returns 503 (fails closed).

**Schema in the prompt:** at the start of each run, the tables the agent can read are appended
to the system prompt (`describe_schema()` in `app/tools/database.py`), including Postgres
**table and column comments**, so it skips the lookup step and knows what columns mean.
It is read fresh every run, deliberately **without caching**, so new uploads and edited
comments show up immediately. The list of uploaded documents (`describe_documents()`) is added the same way. Add comments with `comment on table/column ... is '...'` in a migration.

| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/health` | – | `{status, llm_provider, llm_model, database, commit}` (`commit` = deployed git SHA on Render) |
| POST | `/agent/run` | `{task, system_prompt?, output_schema?, mode?: "agent" \| "orchestrator"}` | `{run_id, answer, stopped_reason, trace}` |
| POST | `/agent/stream` | same JSON body | SSE stream (below) — read with `fetch` + `response.body.getReader()` |
| GET | `/agent/stream` | `?task=...&system_prompt=...&mode=...` | SSE stream — works with `new EventSource(url)` (no output_schema) |
| GET | `/runs/{run_id}` | – | the logged row from `agent_runs` |
| GET | `/runs/{run_id}/tree` | – | `{id, agent_name, task, status, duration_ms, children: [...]}` |
| GET | `/runs/{run_id}/sources` | – | `{summary: {total, accepted, rejected, unreviewed}, sources}`: each source resolved (chunk `text`, or `columns`/`rows` from re-running the cited SQL as `agent_readonly`), with `marker` and latest `review` (`app/sources.py`, `app/reviews.py`) |
| GET | `/approvals` | – | `{pending: [{run_id, tool, input, waiting_seconds}]}` |
| POST | `/runs/{run_id}/approve` | `{reason?}` | `{run_id, decision: "approved"}`; 404 if the run isn't waiting |
| POST | `/runs/{run_id}/reject` | `{reason?}` | `{run_id, decision: "rejected"}`; 404 if the run isn't waiting |
| POST | `/runs/{run_id}/claims/{marker}/review` | `{decision: accepted \| rejected, reviewer?, comment?}` | `{run_id, marker, review}`; marker = the `[n]` in the text (1-based); 404 bad run/marker |
| POST | `/upload/table` | multipart: `file` (.csv/.xlsx), `column_descriptions?` (JSON string) | `{tables: [{table, rows, comment, columns}], unused_column_descriptions}` |
| POST | `/upload/pdf` | multipart: `file` (.pdf, max 20 MB) | `{document_name, pages, chunks}` |
| GET | `/insights/summary` | `?horizon_days=56` | 4 KPIs `{value, definition, inputs}`: stock value, waste last 90 days, stockout rate (fraction), capital in overstock; + `plan_totals` with vs without |
| GET | `/insights/recommendations` | `?limit=30&location_id&sku&horizon_days` | top rows by `nok_impact` (each with `inputs` = the why, and latest `decision`) + `overstock` list |
| POST | `/orders` | `{sku, location_id, qty, order_date, decision, reason?, decided_by?}` | `{order}` (dashboard approve/reject → `proposed_orders`) |
| GET | `/orders` | `?limit=100` | `{orders}` newest first |

SSE events, in order: `run_started` `{run_id}` → one `step` per trace entry
(`type`: `llm` | `tool` | `approval` | `final_answer`) → `done` `{run_id, answer, stopped_reason, trace}`,
or `error` `{error}` if the run crashed. When a flagged tool is called, an `approval_required`
event `{run_id, tool, input}` arrives and the stream pauses until `/runs/{run_id}/approve` or `/reject`;
then a `step` with `type: "approval"` `{decision, reason, waited_ms}` follows.

Every step has an `agent` field: `agent`, `orchestrator`, `data_analyst` or `inventory_planner`.
In orchestrator mode, a `step` with `type: "delegation_started"` `{specialist, child_run_id, question}`
is followed by that specialist's own steps (with `agent` and `run_id` of the child run), then the
orchestrator's `tool` entry for the delegation (with `child_trace`).

`answer` is a string, or an object matching `output_schema` when one is given
(it must have `"type": "object"` at the top level and must not define `sources` itself —
`sources` is always added).

**Answer style** (`ANSWER_STYLE` in service.py, added to every prompt): product and location names, not codes;
at most ~10 claims unless asked for more.

**Anthropic note:** `claude-sonnet-5-5` rejects a forced `tool_choice`; the adapter retries without it
(the loop has already asked for `submit_answer` in the conversation). `max_tokens` is 16000.

**OpenAI note:** `OPENAI_REASONING_EFFORT` is only sent when set. `gpt-6.1-terra` needs `none`
for tool calls on Chat Completions.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in values (Claude filled the Supabase ones)
git config core.hooksPath .githooks   # once per clone: enables the secret-scan hooks
.venv/bin/pytest -q
.venv/bin/uvicorn app.main:app --reload
curl localhost:8000/health
```

Schema changes:

```bash
supabase migration new <name>          # write SQL in the new file
SUPABASE_DB_PASSWORD=... supabase db push --remote
.venv/bin/python scripts/set_readonly_password.py   # only if the role was (re)created
```

## Secret scan (git hooks)

`.githooks/pre-commit` and `.githooks/pre-push` run `scripts/check_secrets.py`, which blocks the
commit/push if `.env` is staged or if any secret value from `.env` (names ending in KEY, PASSWORD,
SECRET, TOKEN, DB_URL) appears in the staged diff or in the commits being pushed. It prints variable
names, never values. Enable once per clone with `git config core.hooksPath .githooks`.
The pre-push check also catches commits made with `git commit --no-verify`.

## Live checks

```bash
.venv/bin/python -u -m scripts.live_check https://ai-session-with-kpmg.onrender.com          # all checks
.venv/bin/python -u -m scripts.live_check http://localhost:8000 health stream approve reject # some
```

Runs health, a full run, stream timing, approve/reject flows and the orchestrator against a running
backend, printing a timeline. Note: `approve` saves a real row in `saved_reports`.

## Reset data before the hackathon

```bash
.venv/bin/python -m scripts.reset_data          # dry run: lists what would be deleted
.venv/bin/python -m scripts.reset_data --yes    # drops all uploaded/demo tables (incl. sample_sales),
                                                # empties document_chunks, saved_reports, claim_reviews,
                                                # proposed_orders; keeps agent_runs
.venv/bin/python -m scripts.reset_data --include-runs --yes   # same, and also empties agent_runs
```

Cannot be undone, and `supabase db push` will not recreate `sample_sales` (its migration stays applied).

## Deploy (Render)

1. Render dashboard → **New → Blueprint** → connect GitHub and select `hflaten/ai-session-with-kpmg`.
   It reads `render.yaml`: free plan, Frankfurt, Python 3.13.1, health check `/health`, 1 worker.
2. Paste the 7 secret env vars (same values as `.env`): `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
   `OPENAI_API_KEY`, `OPENAI_MODEL`, `SUPABASE_DB_URL`, `SUPABASE_READONLY_DB_URL`, `APP_API_KEY`.
   The other 5 (`LLM_PROVIDER`, `OPENAI_BASE_URL`, `OPENAI_REASONING_EFFORT`, `EMBEDDING_MODEL`,
   `ALLOWED_ORIGINS`) plus `PYTHON_VERSION` are set by `render.yaml`.
3. Test: `curl https://ai-session-with-kpmg.onrender.com/health` → `"status":"ok","database":"ok"`.
4. If `/health` shows `"llm_model":"(not set)"` / `"database":"unreachable"` and other endpoints
   return 503, the secret env vars are missing: add them and choose **Save, rebuild, and deploy**.

**Live URL:** https://ai-session-with-kpmg.onrender.com — verified 2026-11-05: `/health` ok,
structured `/agent/run` with both tools and cited sources (10 s), POST and GET `/agent/stream`
arrive live through Render's proxy (no buffering), runs logged in `agent_runs`.

```bash
curl https://ai-session-with-kpmg.onrender.com/health
curl -X POST https://ai-session-with-kpmg.onrender.com/agent/run \
  -H "X-API-Key: $APP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"task": "Total revenue per region in Q1 2026?"}'
```

Deliberately **not** on Render: Supabase URL, anon key, service role key, DB password, project ref.
The backend never reads them (it only uses the two connection strings), so the service role key
can't leak from the server. They're only needed locally (Supabase CLI, scripts).

Notes: the database URLs use the Supabase **session pooler** (`aws-1-eu-central-1.pooler.supabase.com`)
because the direct DB host is IPv6-only and Render can't reach it. The free plan sleeps when idle;
the first request after a sleep takes ~30–60 s, and any run waiting for approval is lost.

## Progress

- [x] 0a Repo (private, pushed)  - [x] 0b Supabase project, link, init migration
- [x] 1 FastAPI skeleton         - [x] 2 LLM adapters  - [x] 3 Agent loop
- [x] 4 query_database           - [x] 5 Structured output - [x] 6 SSE + run logging
- [x] 7 Uploads + RAG - [x] 8 Human-in-the-loop
- [x] 9 Orchestrator             - [x] 10 Deploy
- [x] Case 4 (Høyden, 2026-11-06): reset, data loaded, recommend_orders, create_purchase_order, dashboard endpoints
