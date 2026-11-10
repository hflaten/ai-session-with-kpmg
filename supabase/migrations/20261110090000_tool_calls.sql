-- Full output of every tool call, so a cited tool result ({tool, call_id, row, fields})
-- can always be resolved later. The trace and the live stream only keep a short preview.
create table if not exists public.tool_calls (
    call_id     text primary key,                     -- e.g. "rc-3f9a1c", shown to the model in the tool result
    created_at  timestamptz not null default now(),
    run_id      uuid not null references public.agent_runs(id) on delete cascade,
    tool        text not null,
    arguments   jsonb not null default '{}'::jsonb,
    output      jsonb                                 -- exactly what the tool returned
);
create index if not exists tool_calls_run_idx on public.tool_calls (run_id, created_at);
comment on table public.tool_calls is 'Full output of each tool call in an agent run (evidence for tool citations). Backend only.';

-- Like agent_runs: backend only (no REST/anon access; query_database can't see it).
alter table public.tool_calls enable row level security;
revoke all on public.tool_calls from anon, authenticated, agent_readonly;

-- Claim reviews of tool sources also snapshot the evidence pointer and the cited values.
alter table public.claim_reviews add column if not exists tool_evidence jsonb;   -- {call_id, row, fields: [{field, value}]}
