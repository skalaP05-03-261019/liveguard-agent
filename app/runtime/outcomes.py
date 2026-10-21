"""Small safe public status vocabulary shared by execution and offline recovery."""
import re

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "NEEDS_REVIEW"})


def safe_error_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else None


def checkpoint_outcome(snapshot):
    if snapshot.interrupts:
        return "WAITING_REVIEW", None
    status = snapshot.values.get("task_outcome", "UNKNOWN")
    if status not in TERMINAL | {"UNKNOWN"}:
        status = "UNKNOWN"
    error = snapshot.values.get("error")
    code = safe_error_code(error.get("code")) if isinstance(error, dict) else None
    return status, code
