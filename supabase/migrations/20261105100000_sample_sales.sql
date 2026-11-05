-- Small made-up dataset for testing the query_database tool.
-- Safe to drop before the hackathon: drop table public.sample_sales;
create table if not exists public.sample_sales (
    id         serial primary key,
    month      date    not null,
    region     text    not null,
    product    text    not null,
    units      integer not null,
    revenue    numeric(12, 2) not null,
    cost       numeric(12, 2) not null
);

comment on table public.sample_sales is 'Demo data: monthly sales per region and product (NOK).';

insert into public.sample_sales (month, region, product, units, revenue, cost) values
    ('2026-01-01', 'Oslo',      'Audit Pro',    12, 240000, 150000),
    ('2026-01-01', 'Oslo',      'Tax Assist',   30, 135000,  60000),
    ('2026-01-01', 'Bergen',    'Audit Pro',     7, 140000,  91000),
    ('2026-01-01', 'Bergen',    'Tax Assist',   18,  81000,  38000),
    ('2026-01-01', 'Trondheim', 'Audit Pro',     5, 100000,  70000),
    ('2026-01-01', 'Trondheim', 'Tax Assist',   11,  49500,  24000),
    ('2026-02-01', 'Oslo',      'Audit Pro',    14, 280000, 168000),
    ('2026-02-01', 'Oslo',      'Tax Assist',   41, 184500,  80000),
    ('2026-02-01', 'Bergen',    'Audit Pro',     6, 120000,  84000),
    ('2026-02-01', 'Bergen',    'Tax Assist',   22,  99000,  45000),
    ('2026-02-01', 'Trondheim', 'Audit Pro',     8, 160000, 104000),
    ('2026-02-01', 'Trondheim', 'Tax Assist',   13,  58500,  27000),
    ('2026-03-01', 'Oslo',      'Audit Pro',    11, 220000, 143000),
    ('2026-03-01', 'Oslo',      'Tax Assist',   55, 247500, 101000),
    ('2026-03-01', 'Bergen',    'Audit Pro',     9, 180000, 112000),
    ('2026-03-01', 'Bergen',    'Tax Assist',   20,  90000,  43000),
    ('2026-03-01', 'Trondheim', 'Audit Pro',     4,  80000,  62000),
    ('2026-03-01', 'Trondheim', 'Tax Assist',   16,  72000,  31000),
    ('2026-04-01', 'Oslo',      'Audit Pro',    15, 300000, 177000),
    ('2026-04-01', 'Oslo',      'Tax Assist',   38, 171000,  76000),
    ('2026-04-01', 'Bergen',    'Audit Pro',    10, 200000, 118000),
    ('2026-04-01', 'Bergen',    'Tax Assist',   25, 112500,  50000),
    ('2026-04-01', 'Trondheim', 'Audit Pro',     6, 120000,  81000),
    ('2026-04-01', 'Trondheim', 'Tax Assist',   19,  85500,  36000);
