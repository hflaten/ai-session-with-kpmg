-- Column comments are shown to the agent in its system prompt (describe_schema).
comment on column public.sample_sales.id      is 'Row id, no business meaning';
comment on column public.sample_sales.month   is 'First day of the month the sales belong to (one row per region, product and month)';
comment on column public.sample_sales.region  is 'Sales office: Oslo, Bergen or Trondheim';
comment on column public.sample_sales.product is 'Product sold: Audit Pro or Tax Assist';
comment on column public.sample_sales.units   is 'Number of engagements sold that month';
comment on column public.sample_sales.revenue is 'Revenue in NOK, excluding VAT';
comment on column public.sample_sales.cost    is 'Direct delivery cost in NOK; profit = revenue - cost';
