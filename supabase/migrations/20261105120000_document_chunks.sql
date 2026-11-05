-- PDF chunks + embeddings for search_documents (step 7). Approved schema.
create table if not exists public.document_chunks (
    chunk_id       text primary key,                  -- 'annual-report-2025.pdf:p12:c31'
    document_name  text    not null,                  -- the uploaded file name
    page_number    integer not null,                  -- 1-based, matches what a PDF viewer shows
    chunk_index    integer not null,                  -- 0-based position within the document
    text           text    not null,                  -- the original chunk text, unchanged
    embedding      extensions.vector(1536) not null,  -- text-embedding-3-small
    created_at     timestamptz not null default now(),
    unique (document_name, chunk_index)
);

-- Fast approximate nearest-neighbour search by cosine distance
create index if not exists document_chunks_embedding_idx
    on public.document_chunks using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists document_chunks_document_idx
    on public.document_chunks (document_name);

-- No access via Supabase's REST API (anon key), and query_database can't read it
-- (it would return raw embeddings). search_documents reads it from backend code.
alter table public.document_chunks enable row level security;
revoke all on public.document_chunks from anon, authenticated, agent_readonly;
