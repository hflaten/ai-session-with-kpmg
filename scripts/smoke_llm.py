"""Live check of both adapters with a real tool call. Needs keys in .env.

    .venv/bin/python -m scripts.smoke_llm            # both providers
    .venv/bin/python -m scripts.smoke_llm openai     # one provider
"""
import sys
from dataclasses import replace

from app import config
from app.llm import Tool, client

ADD = Tool(
    name="add",
    description="Add two numbers",
    input_schema={"type": "object",
                  "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                  "required": ["a", "b"]},
)

providers = sys.argv[1:] or ["anthropic", "openai"]
for provider in providers:
    # swap the provider for this call only
    client.settings = replace(config.settings, llm_provider=provider)
    try:
        r = client.call_llm("Use the add tool for any arithmetic.",
                            [{"role": "user", "content": "What is 2.5 + 4?"}], [ADD])
        print(f"{provider:9} model={client.settings.llm_model}  tool_calls={[(c.name, c.input) for c in r.tool_calls]}"
              f"  tokens={r.input_tokens}/{r.output_tokens}")
    except Exception as e:
        print(f"{provider:9} FAILED: {type(e).__name__}: {str(e)[:200]}")
