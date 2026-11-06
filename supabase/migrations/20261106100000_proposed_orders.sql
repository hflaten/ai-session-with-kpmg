-- Purchase orders a human has decided on: approved via the agent's create_purchase_order
-- tool (which pauses for approval), or approved/rejected on the dashboard.
create table if not exists public.proposed_orders (
    id           uuid primary key default gen_random_uuid(),
    created_at   timestamptz not null default now(),
    run_id       uuid references public.agent_runs(id) on delete set null,   -- agent run that proposed it (null = dashboard)
    sku          text not null,
    location_id  text not null,
    qty          integer not null check (qty > 0),
    order_date   date not null,
    reason       text,
    status       text not null check (status in ('approved', 'rejected')),
    source       text not null check (source in ('agent', 'dashboard')),
    decided_by   text
);
create index if not exists proposed_orders_item_idx on public.proposed_orders (sku, location_id, order_date, created_at desc);

comment on table public.proposed_orders is 'Purchase orders decided by a human: approved in the agent run (create_purchase_order) or approved/rejected on the dashboard. Newest row per sku + location + order_date is the current decision.';
comment on column public.proposed_orders.run_id      is 'Agent run that proposed the order; empty when decided on the dashboard';
comment on column public.proposed_orders.sku         is 'Product id (-> products.sku)';
comment on column public.proposed_orders.location_id is 'Location id (-> locations.location_id)';
comment on column public.proposed_orders.qty         is 'Units to order';
comment on column public.proposed_orders.order_date  is 'Date to place the order with the supplier';
comment on column public.proposed_orders.reason      is 'Why the order is needed';
comment on column public.proposed_orders.status      is 'approved or rejected (by a human)';
comment on column public.proposed_orders.source      is 'agent (create_purchase_order tool) or dashboard';
comment on column public.proposed_orders.decided_by  is 'Who decided, if given';

-- Like saved_reports: no REST/anon access; agent_readonly may read it (query_database).
alter table public.proposed_orders enable row level security;
revoke all on public.proposed_orders from anon, authenticated;
create policy agent_readonly_can_read on public.proposed_orders
    for select to agent_readonly using (true);

-- Claim reviews can now snapshot a third kind of source: a deterministic tool result.
alter table public.claim_reviews drop constraint if exists claim_reviews_source_kind_check;
alter table public.claim_reviews add constraint claim_reviews_source_kind_check
    check (source_kind in ('document', 'database', 'tool'));
alter table public.claim_reviews
    add column if not exists tool_name      text,     -- tool source
    add column if not exists tool_arguments jsonb;
