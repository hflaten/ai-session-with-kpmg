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
| Tools | `app/tools/` | `query_database` (read-only SQL), `search_documents` (pgvector). |
| Structured output | `app/agent/structured.py` | Final answer is a `submit_answer` tool call whose schema is the requested JSON schema + a required `sources` list. Citations are checked against what the agent actually found this run. Works on both providers. |
| Streaming | `app/routes/` | SSE, one event per agent step. Every run logged to `agent_runs`. |
| Ingest | `app/ingest/` | CSV/Excel → own table via pandas, with table + column comments; PDF → per-page chunks → embeddings → `document_chunks`. |
| Human-in-the-loop | `app/agent/approvals.py` | Tools flagged `requires_approval` pause the run until `/runs/{id}/approve` or `/reject`. Held in memory → single worker. |
| Orchestrator | `app/agent/orchestrator.py` | Orchestrator's tools call `run_agent` for two specialists. Trace nests specialist traces; `agent_runs.parent_run_id` links them. |

### Database (supabase/migrations)

- `agent_runs`: id, created_at, parent_run_id, agent_name, task, provider, model,
  status (running | awaiting_approval | completed | failed), answer, trace, error, duration_ms.
- `sample_sales`: 24 rows of made-up demo data for testing query_database (drop before real use).
- `document_chunks`: chunk_id (`file:p<page>:c<index>`), document_name, page_number, chunk_index,
  text, embedding `vector(1536)` with an HNSW cosine index. Not readable by `agent_readonly`;
  `search_documents` reads it with a fixed query. Re-uploading a file name replaces its chunks.
- Uploaded tables: one per CSV / Excel sheet, named after the file (snake_case). Re-upload replaces.
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
| GET | `/health` | – | `{status, llm_provider, llm_model, database}` |
| POST | `/agent/run` | `{task, system_prompt?, output_schema?}` | `{run_id, answer, stopped_reason, trace}` |
| POST | `/agent/stream` | same JSON body | SSE stream (below) — read with `fetch` + `response.body.getReader()` |
| GET | `/agent/stream` | `?task=...&system_prompt=...` | SSE stream — works with `new EventSource(url)` (no output_schema) |
| GET | `/runs/{run_id}` | – | the logged row from `agent_runs` |
| POST | `/upload/table` | multipart: `file` (.csv/.xlsx), `column_descriptions?` (JSON string) | `{tables: [{table, rows, comment, columns}], unused_column_descriptions}` |
| POST | `/upload/pdf` | multipart: `file` (.pdf, max 20 MB) | `{document_name, pages, chunks}` |

SSE events, in order: `run_started` `{run_id}` → one `step` per trace entry
(`type`: `llm` | `tool` | `final_answer`) → `done` `{run_id, answer, stopped_reason, trace}`,
or `error` `{error}` if the run crashed.

`answer` is a string, or an object matching `output_schema` when one is given
(it must have `"type": "object"` at the top level and must not define `sources` itself —
`sources` is always added).

**OpenAI note:** `OPENAI_REASONING_EFFORT` is only sent when set. `gpt-6.1-terra` needs `none`
for tool calls on Chat Completions.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill in values (Claude filled the Supabase ones)
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

## Deploy (Render)

1. Render dashboard → New → Blueprint → select `hflaten/ai-session-with-kpmg`; it reads `render.yaml`.
2. Paste the env vars marked `sync: false` (same values as `.env`).
3. Test: `curl https://<service>.onrender.com/health` → `"status":"ok","database":"ok"`.

Notes: the database URLs use the Supabase **session pooler** (`aws-1-eu-central-1.pooler.supabase.com`)
because the direct DB host is IPv6-only and Render can't reach it. The free plan sleeps when idle;
the first request after a sleep takes ~30–60 s, and any run waiting for approval is lost.

## Progress

- [x] 0a Repo (private, pushed)  - [x] 0b Supabase project, link, init migration
- [x] 1 FastAPI skeleton         - [x] 2 LLM adapters  - [x] 3 Agent loop
- [x] 4 query_database           - [x] 5 Structured output - [x] 6 SSE + run logging
- [x] 7 Uploads + RAG - [ ] 8 Human-in-the-loop
- [ ] 9 Orchestrator             - [ ] 10 Deploy
