"""Structured final answers via a `submit_answer` tool.

The caller's JSON schema becomes the tool's input schema, so the model "fills
in a form" to finish. This works the same on Anthropic and OpenAI-compatible
APIs, unlike each provider's own JSON mode.
"""
from jsonschema import Draft202012Validator

from app.llm import Tool

SUBMIT_TOOL_NAME = "submit_answer"


def check_output_schema(schema: dict) -> None:
    """Raise ValueError if the schema can't be used as a tool input schema."""
    if schema.get("type") != "object":
        raise ValueError("output_schema must have \"type\": \"object\" at the top level")
    Draft202012Validator.check_schema(schema)  # raises SchemaError if malformed


def make_submit_tool(schema: dict) -> Tool:
    return Tool(
        name=SUBMIT_TOOL_NAME,
        description=("Submit your final answer. Call this exactly once, when you are done, "
                     "with arguments that match the schema. Do not answer in plain text."),
        input_schema=schema,
    )


def validation_errors(schema: dict, answer: dict) -> list[str]:
    """Empty list = valid. Otherwise readable messages to send back to the model."""
    validator = Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in validator.iter_errors(answer)]
