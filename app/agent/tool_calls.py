"""Every tool call gets a short id that the model sees and can cite: "rc-3f9a1c".

remember() keeps the full output in memory (the orchestrator checks its specialists'
calls this way, in the same process); recall() falls back to the tool_calls table,
where service.py persists every call (so /runs/{id}/sources can always resolve it).
"""
import secrets
from collections import OrderedDict

MAX_IN_MEMORY = 2000
_recent: OrderedDict[str, dict] = OrderedDict()


def new_call_id() -> str:
    return "rc-" + secrets.token_hex(3)


def remember(call_id: str, tool: str, arguments: dict, output) -> None:
    _recent[call_id] = {"call_id": call_id, "tool": tool, "arguments": arguments, "output": output}
    while len(_recent) > MAX_IN_MEMORY:
        _recent.popitem(last=False)


def recall(call_id: str) -> dict | None:
    """{call_id, tool, arguments, output} from memory, else from the database (None if unknown)."""
    if call_id in _recent:
        return _recent[call_id]
    try:
        from app import runs
        return runs.get_tool_call(call_id)
    except Exception:
        return None
