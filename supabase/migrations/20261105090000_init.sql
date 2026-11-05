-- 1. pgvector for document embeddings (used in step 7)
create extension if not exists vector with schema extensions;

-- 2. Every agent run is logged here (steps 6, 8, 9)
create table if not exists public.agent_runs (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz not null default now(),
    parent_run_id uuid references public.agent_runs(id) on delete cascade, -- set for specialist runs
    agent_name    text not null default 'agent',
    task          text not null,
    provider      text,
    model         text,
    status        text not null default 'running',  -- running | awaiting_approval | completed | failed
    answer        jsonb,
    trace         jsonb not null default '[]'::jsonb,
    error         text,
    duration_ms   integer
);
create index if not exists agent_runs_parent_idx on public.agent_runs (parent_run_id);
create index if not exists agent_runs_created_idx on public.agent_runs (created_at desc);

-- 3. Lock the public schema away from Supabase's REST API.
--    The Lovable frontend holds the anon key; it must not be able to read or
--    write our tables directly. Only this backend (via Postgres) touches them.
alter table public.agent_runs enable row level security;
revoke all on all tables in schema public from anon, authenticated;
alter default privileges for role postgres in schema public
    revoke all on tables from anon, authenticated;

-- 4. Read-only role for the agent's query_database tool.
--    No password here (migrations are committed to git); it is set from .env
--    by scripts/set_readonly_password.py. Until then the role cannot log in.
do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'agent_readonly') then
        create role agent_readonly login;
    end if;
end $$;

alter role agent_readonly set default_transaction_read_only = on;  -- can never write
alter role agent_readonly set statement_timeout = '10s';           -- no runaway queries

grant usage on schema public to agent_readonly;
grant select on all tables in schema public to agent_readonly;
-- Tables created later (e.g. uploaded CSVs) are readable automatically
alter default privileges for role postgres in schema public
    grant select on tables to agent_readonly;

-- The agent queries business data, not its own run logs
revoke all on public.agent_runs from agent_readonly;
