"""PDF -> page text -> chunks -> embeddings -> document_chunks table.

Chunks never cross a page boundary, so every chunk has one exact page_number.
"""
import io
import re

from pypdf import PdfReader

from app import db
from app.ingest.embeddings import embed, to_pgvector

CHUNK_CHARS = 3200     # ~800 tokens (roughly 4 characters per token)
OVERLAP_CHARS = 300    # repeat the end of the previous chunk so sentences aren't cut off


def extract_pages(pdf_bytes: bytes) -> list[str]:
    """Text of each page, in order. Index 0 = page 1."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [re.sub(r"[ \t]+", " ", page.extract_text() or "").strip() for page in reader.pages]


def split_page(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Split one page's text into overlapping pieces, cutting at whitespace."""
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):                       # don't cut a word in half
            space = text.rfind(" ", start + size // 2, end)
            end = space if space != -1 else end
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        # next chunk starts `overlap` characters back, moved forward to the start of a word
        next_start = end - overlap
        space = text.find(" ", next_start, end)
        start = max(space + 1 if space != -1 else next_start, start + 1)
    return [c for c in chunks if c]


def make_chunks(document_name: str, pages: list[str]) -> list[dict]:
    chunks, index = [], 0
    for page_number, text in enumerate(pages, start=1):
        for piece in split_page(text):
            chunks.append({
                "chunk_id": f"{document_name}:p{page_number}:c{index}",
                "document_name": document_name,
                "page_number": page_number,
                "chunk_index": index,
                "text": piece,
            })
            index += 1
    return chunks


def ingest_pdf(document_name: str, pdf_bytes: bytes) -> dict:
    pages = extract_pages(pdf_bytes)
    chunks = make_chunks(document_name, pages)
    if not chunks:
        raise ValueError("No text found in this PDF. Scanned PDFs need OCR, which this backend doesn't do.")

    vectors = embed([c["text"] for c in chunks])

    with db.connect() as conn:  # one transaction: replace any older version of this document
        conn.execute("delete from document_chunks where document_name = %s", (document_name,))
        with conn.cursor() as cur:
            cur.executemany(
                """insert into document_chunks
                       (chunk_id, document_name, page_number, chunk_index, text, embedding)
                   values (%s, %s, %s, %s, %s, %s::extensions.vector)""",
                [(c["chunk_id"], c["document_name"], c["page_number"], c["chunk_index"], c["text"],
                  to_pgvector(v)) for c, v in zip(chunks, vectors)],
            )
    return {"document_name": document_name, "pages": len(pages), "chunks": len(chunks)}
