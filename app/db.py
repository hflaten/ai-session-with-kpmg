"""Database connections.

Two connection strings:
- SUPABASE_DB_URL:          full access, used by the backend itself (logging runs, uploads)
- SUPABASE_READONLY_DB_URL: the agent_readonly role, used only by the query_database tool

A fresh connection per call keeps things simple; fine for hackathon traffic.
"""
import psycopg
from psycopg.rows import dict_row

from app.config import settings


def connect(readonly: bool = False) -> psycopg.Connection:
    url = settings.readonly_db_url if readonly else settings.db_url
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)


def ping() -> bool:
    """True if the main database answers `select 1`."""
    try:
        with connect() as conn:
            conn.execute("select 1")
        return True
    except Exception:
        return False
