"""Shared-token check for every endpoint except /health.

The frontend sends the token in the X-API-Key header. This is NOT real
security (the token sits in Lovable's frontend code, readable by anyone);
it only stops casual abuse of a public URL.

`?api_key=` is accepted as a fallback because the browser's EventSource
(used for GET /agent/stream) cannot send custom headers.
"""
import secrets

from fastapi import Header, HTTPException, Query

from app.config import settings


def require_api_key(x_api_key: str | None = Header(default=None),
                    api_key: str | None = Query(default=None)) -> None:
    expected = settings.app_api_key
    if not expected:
        # Fail closed: a missing APP_API_KEY must not mean "open to everyone".
        raise HTTPException(503, "Server misconfigured: APP_API_KEY is not set")
    provided = x_api_key or api_key or ""
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(401, "Missing or invalid X-API-Key")
