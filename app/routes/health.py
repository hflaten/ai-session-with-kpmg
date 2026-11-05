from fastapi import APIRouter

from app import db
from app.config import settings

router = APIRouter()


@router.get("/health")
def health():
    """Liveness check for Render and for you. Never returns secrets."""
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model or "(not set)",
        "database": "ok" if db.ping() else "unreachable",
    }
