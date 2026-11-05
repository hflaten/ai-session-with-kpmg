"""Text -> embedding vectors (OpenAI-compatible API, EMBEDDING_MODEL).

Anthropic has no embeddings API, so this always uses OPENAI_API_KEY /
OPENAI_BASE_URL, whatever LLM_PROVIDER is.
"""
import openai

from app.config import settings

BATCH_SIZE = 100
_client: openai.OpenAI | None = None


def _get_client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    return _client


def embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        resp = _get_client().embeddings.create(model=settings.embedding_model, input=texts[i:i + BATCH_SIZE])
        vectors.extend(d.embedding for d in resp.data)
    return vectors


def to_pgvector(vector: list[float]) -> str:
    """pgvector accepts the text form '[0.1,0.2,...]'; cast it with ::extensions.vector."""
    return "[" + ",".join(f"{x:.7f}" for x in vector) + "]"
