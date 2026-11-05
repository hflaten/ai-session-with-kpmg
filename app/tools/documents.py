"""search_documents: semantic search over uploaded PDFs (pgvector).

Uses the main connection with a fixed, parameterised query: the agent only
chooses the search text, never the SQL. (agent_readonly can't read
document_chunks, so query_database can't dump embeddings.)
"""
from app import db
from app.ingest.embeddings import embed, to_pgvector
from app.llm import Tool

MAX_TOP_K = 10


def search_documents(query: str, top_k: int = 5, document_name: str | None = None) -> dict:
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    vector = to_pgvector(embed([query])[0])
    with db.connect() as conn:
        rows = conn.execute(
            """select chunk_id, document_name, page_number, text,
                      round((1 - (embedding <=> %(v)s::extensions.vector))::numeric, 3)::float as similarity
               from document_chunks
               where %(doc)s::text is null or document_name = %(doc)s
               order by embedding <=> %(v)s::extensions.vector
               limit %(k)s""",
            {"v": vector, "doc": document_name, "k": top_k},
        ).fetchall()
    return {"results": rows}


def list_documents() -> list[dict]:
    """[{document_name, pages, chunks}] - shown to the agent in its system prompt."""
    with db.connect() as conn:
        return conn.execute(
            """select document_name, max(page_number) as pages, count(*) as chunks
               from document_chunks group by document_name order by document_name"""
        ).fetchall()


def describe_documents() -> str:
    docs = list_documents()
    if not docs:
        return "No documents have been uploaded yet."
    lines = [f"- {d['document_name']} ({d['pages']} pages, {d['chunks']} chunks)" for d in docs]
    return "Documents you can search with search_documents:\n" + "\n".join(lines)


search_documents_tool = Tool(
    name="search_documents",
    description=(
        "Semantic search over the uploaded PDF documents. Returns the most relevant text chunks, "
        "each with chunk_id, document_name, page_number, text and similarity (0-1). "
        "Write the query as a short description of the information you need. "
        "Cite the chunk_id, document_name and page_number of any chunk you use."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K, "description": "Number of chunks (default 5)"},
            "document_name": {"type": "string", "description": "Optional: only search this document"},
        },
        "required": ["query"],
    },
    run=search_documents,
    # Extra fields for this tool's trace entry: which chunks the search returned
    trace_fields=lambda output: {"chunk_ids": [r["chunk_id"] for r in output["results"]]},
)
