"""File uploads.

POST /upload/table  CSV/Excel -> Postgres table(s) with table + column comments
POST /upload/pdf    PDF -> chunks + embeddings in document_chunks
"""
import json

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.ingest.pdf import ingest_pdf
from app.ingest.tabular import ingest_table
from app.tools.inventory import clear_cache

router = APIRouter(prefix="/upload")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


async def _read(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 20 MB")
    if not data:
        raise HTTPException(400, "File is empty")
    return data


@router.post("/table")
async def upload_table(file: UploadFile = File(...), column_descriptions: str | None = Form(default=None)):
    """column_descriptions: optional JSON object, e.g. {"Revenue (NOK)": "Revenue excl. VAT"}.
    Keys can be the original header or the snake_case column name."""
    descriptions = None
    if column_descriptions:
        try:
            descriptions = json.loads(column_descriptions)
            assert isinstance(descriptions, dict)
        except (ValueError, AssertionError):
            raise HTTPException(400, "column_descriptions must be a JSON object")
    data = await _read(file)
    try:
        result = ingest_table(file.filename or "upload.csv", data, descriptions)
    except ValueError as e:
        raise HTTPException(400, str(e))
    clear_cache()   # the order plan reads uploaded tables
    return result


@router.post("/pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a .pdf file")
    data = await _read(file)
    try:
        return ingest_pdf(file.filename, data)
    except ValueError as e:
        raise HTTPException(422, str(e))
