"""Human-in-the-loop: a run waits here until someone approves or rejects.

    decision, reason = wait_for_decision(run_id, tool, input)   # blocks the run's thread
    decide(run_id, approved=True, reason="looks good")          # called by POST /runs/{id}/approve

Pending approvals live in memory (a dict + threading.Event), so the server
must run a single worker process, and a restart loses waiting runs.
One run can only wait for one action at a time, so run_id identifies it.
"""
import threading
import time
from dataclasses import dataclass, field

APPROVAL_TIMEOUT_SECONDS = 600  # 10 minutes, then the action counts as rejected ("timeout")


@dataclass
class PendingApproval:
    run_id: str
    tool: str
    input: dict
    created_at: float = field(default_factory=time.time)
    decided: threading.Event = field(default_factory=threading.Event)
    decision: str = "timeout"   # approved | rejected | timeout
    reason: str = ""


_pending: dict[str, PendingApproval] = {}
_lock = threading.Lock()


def request(run_id: str, tool: str, input: dict) -> PendingApproval:
    """Register that this run is waiting. Call this BEFORE telling anyone (the stream),
    so a client that approves instantly finds the request already here."""
    pending = PendingApproval(run_id=run_id, tool=tool, input=input)
    with _lock:
        _pending[run_id] = pending
    return pending


def wait(pending: PendingApproval, timeout: float = APPROVAL_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Block until decide() is called (or was already called) or the timeout passes."""
    try:
        if not pending.decided.wait(timeout):
            return "timeout", f"No decision within {int(timeout)} seconds"
        return pending.decision, pending.reason
    finally:
        with _lock:
            if _pending.get(pending.run_id) is pending:
                del _pending[pending.run_id]


def wait_for_decision(run_id: str, tool: str, input: dict,
                      timeout: float = APPROVAL_TIMEOUT_SECONDS) -> tuple[str, str]:
    """request() + wait() in one call."""
    return wait(request(run_id, tool, input), timeout)


def decide(run_id: str, approved: bool, reason: str = "") -> bool:
    """Approve or reject the action a run is waiting on. False if nothing is waiting."""
    with _lock:
        pending = _pending.get(run_id)
    if pending is None or pending.decided.is_set():
        return False
    pending.decision = "approved" if approved else "rejected"
    pending.reason = reason
    pending.decided.set()
    return True


def list_pending() -> list[dict]:
    with _lock:
        items = list(_pending.values())
    return [{"run_id": p.run_id, "tool": p.tool, "input": p.input,
             "waiting_seconds": int(time.time() - p.created_at)} for p in items]
