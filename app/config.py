"""All configuration comes from environment variables.

Locally they are read from .env; on Render they are set in the dashboard.
Nothing secret is ever hard-coded or printed in full - use mask().
"""
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()  # no-op if there is no .env file (e.g. on Render)


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def mask(secret: str) -> str:
    """Show only the first 4 characters of a secret, e.g. 'sk-a…'."""
    return f"{secret[:4]}…" if secret else "(empty)"


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    openai_api_key: str
    openai_model: str
    openai_base_url: str
    embedding_model: str
    supabase_url: str
    supabase_service_role_key: str  # backend only - never returned by any endpoint
    db_url: str
    readonly_db_url: str
    allowed_origins: list[str]

    @property
    def llm_model(self) -> str:
        return self.anthropic_model if self.llm_provider == "anthropic" else self.openai_model


def load_settings() -> Settings:
    return Settings(
        llm_provider=_get("LLM_PROVIDER", "anthropic").lower(),
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        anthropic_model=_get("ANTHROPIC_MODEL"),
        openai_api_key=_get("OPENAI_API_KEY"),
        openai_model=_get("OPENAI_MODEL"),
        openai_base_url=_get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        embedding_model=_get("EMBEDDING_MODEL", "text-embedding-3-small"),
        supabase_url=_get("SUPABASE_URL"),
        supabase_service_role_key=_get("SUPABASE_SERVICE_ROLE_KEY"),
        db_url=_get("SUPABASE_DB_URL"),
        readonly_db_url=_get("SUPABASE_READONLY_DB_URL"),
        allowed_origins=[o.strip() for o in _get("ALLOWED_ORIGINS").split(",") if o.strip()],
    )


settings = load_settings()


def cors_origin_regex(origins: list[str]) -> str:
    """Turn 'https://*.lovable.app' style entries into one regex.

    FastAPI's allow_origins only does exact matches, so wildcards need a regex.
    '*' matches one subdomain label (letters, digits, dashes).
    """
    parts = [re.escape(o).replace(r"\*", r"[a-zA-Z0-9-]+") for o in origins]
    return "^(" + "|".join(parts) + ")$" if parts else "^$"
