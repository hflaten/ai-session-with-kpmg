-- Each review applies to exactly one claim. Store a snapshot of the claim and its
-- reference with the decision, so the audit record stands on its own even if the
-- run's answer changes or the run is re-generated.
alter table public.claim_reviews
    add column if not exists marker        integer generated always as (source_index + 1) stored,  -- [n] in the text
    add column if not exists claim         text,     -- the claim text as cited
    add column if not exists source_kind   text check (source_kind in ('document', 'database')),
    add column if not exists document_name text,     -- document source
    add column if not exists page_number   integer,
    add column if not exists chunk_id      text,
    add column if not exists table_name    text,     -- database source
    add column if not exists sql_query     text;

-- Fill in the snapshot for reviews made before this migration (from the run's stored answer)
update public.claim_reviews r
set claim         = a.answer -> 'sources' -> r.source_index ->> 'claim',
    source_kind   = case when a.answer -> 'sources' -> r.source_index ? 'chunk_id' then 'document' else 'database' end,
    document_name = a.answer -> 'sources' -> r.source_index ->> 'document_name',
    page_number   = (a.answer -> 'sources' -> r.source_index ->> 'page_number')::int,
    chunk_id      = a.answer -> 'sources' -> r.source_index ->> 'chunk_id',
    table_name    = a.answer -> 'sources' -> r.source_index ->> 'table',
    sql_query     = a.answer -> 'sources' -> r.source_index ->> 'sql'
from public.agent_runs a
where a.id = r.run_id
  and r.claim is null
  and a.answer -> 'sources' -> r.source_index is not null;
