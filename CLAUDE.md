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
| Structured output | `app/agent/` | Final answer is a `submit_answer` tool call whose schema is the requested JSON schema. Works on both providers. |
| Streaming | `app/routes/` | SSE, one event per agent step. Every run logged to `agent_runs`. |
| Ingest | `app/ingest/` | CSV/Excel → own table via pandas; PDF → chunks → embeddings → pgvector. |
| Human-in-the-loop | `app/agent/approvals.py` | Tools flagged `requires_approval` pause the run until `/runs/{id}/approve` or `/reject`. Held in memory → single worker. |
| Orchestrator | `app/agent/orchestrator.py` | Orchestrator's tools call `run_agent` for two specialists. Trace nests specialist traces; `agent_runs.parent_run_id` links them. |

### Database (supabase/migrations)

- `agent_runs`: id, created_at, parent_run_id, agent_name, task, provider, model,
  status (running | awaiting_approval | completed | failed), answer, trace, error, duration_ms.
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
- **Process:** before implementing step 7, stop and show the user the table schema and the
  `sources` format for approval. After building it, run a test query and show an answer with its sources.

## Rules

- Secrets only from env vars. Never print secrets in full (use `mask()`).
- The service role key and DB URLs never leave the backend; no endpoint returns them.
- All schema changes are migration files in `supabase/migrations`, applied with
  `supabase db push --remote` (without `--remote` the CLI targets a local Docker stack).
- Simple, readable code — the user has to explain it.
- Test each step; commit and push after each working step.

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
- [x] 1 FastAPI skeleton         - [~] 2 LLM adapters (live check pending keys)  - [ ] 3 Agent loop
- [ ] 4 query_database           - [ ] 5 Structured output - [ ] 6 SSE + run logging
- [ ] 7 Uploads + RAG (approval checkpoint first) - [ ] 8 Human-in-the-loop
- [ ] 9 Orchestrator             - [ ] 10 Deploy
