-- Human review of each cited source (claim) in a run's answer.
-- One row per review action; the latest row per (run_id, source_index) is the current decision.
create table if not exists public.claim_reviews (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz not null default now(),
    run_id        uuid not null references public.agent_runs(id) on delete cascade,
    source_index  integer not null check (source_index >= 0),   -- 0-based position in answer.sources ([n] = index + 1)
    decision      text not null check (decision in ('accepted', 'rejected')),
    reviewer      text,
    comment       text
);
create index if not exists claim_reviews_latest_idx
    on public.claim_reviews (run_id, source_index, created_at desc);

comment on table public.claim_reviews is 'Human accept/reject decisions on the cited sources of an agent answer.';

-- Like agent_runs: backend only. No REST/anon access, and query_database can't see it.
alter table public.claim_reviews enable row level security;
revoke all on public.claim_reviews from anon, authenticated, agent_readonly;
