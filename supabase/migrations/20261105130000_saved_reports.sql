-- Written by the save_report tool, which needs human approval (step 8).
create table if not exists public.saved_reports (
    id          uuid primary key default gen_random_uuid(),
    created_at  timestamptz not null default now(),
    run_id      uuid references public.agent_runs(id) on delete set null,
    title       text not null,
    content     text not null
);
comment on table public.saved_reports is 'Reports the agent saved after a human approved it (save_report tool).';
comment on column public.saved_reports.run_id  is 'The agent run that saved it';
comment on column public.saved_reports.title   is 'Short title written by the agent';
comment on column public.saved_reports.content is 'Report text written by the agent';

-- No REST/anon access (like every table here). agent_readonly can read it via the
-- default privileges, so the agent can look up earlier reports with query_database.
alter table public.saved_reports enable row level security;
revoke all on public.saved_reports from anon, authenticated;
