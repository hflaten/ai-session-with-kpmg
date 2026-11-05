"""FastAPI entry point. Run locally with:

    .venv/bin/uvicorn app.main:app --reload
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import cors_origin_regex, settings
from app.routes import health

app = FastAPI(title="ai-session-with-kpmg")

# Only the Lovable frontend (and localhost) may call this API from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=cors_origin_regex(settings.allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
