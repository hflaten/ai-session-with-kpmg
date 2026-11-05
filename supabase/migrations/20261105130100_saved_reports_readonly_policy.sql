-- With RLS on and no policy, agent_readonly saw zero rows in saved_reports.
-- Allow it to read every row (it still can't write: the role is read-only).
create policy agent_readonly_can_read on public.saved_reports
    for select to agent_readonly using (true);
