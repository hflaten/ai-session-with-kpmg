"""The one function the rest of the app uses to talk to an LLM."""
from app.config import settings
from app.llm import anthropic_adapter, openai_adapter
from app.llm.types import LLMResponse, Tool

ADAPTERS = {
    "anthropic": anthropic_adapter,
    "openai": openai_adapter,
}


def call_llm(system: str, messages: list[dict], tools: list[Tool] | None = None,
             force_tool: str | None = None) -> LLMResponse:
    """Send a conversation to the provider chosen by LLM_PROVIDER.

    force_tool: make the model call this tool (used for structured answers).
    """
    adapter = ADAPTERS.get(settings.llm_provider)
    if adapter is None:
        raise ValueError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'. Use: {', '.join(ADAPTERS)}")
    return adapter.call(system, messages, tools or [], force_tool)
