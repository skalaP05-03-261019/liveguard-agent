"""Dependency diagnostics must not serialize protocol bodies or exception inputs."""
import logging


class _SafeDependencyLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = f"{record.levelname} {record.name} dependency_event"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_FILTER = _SafeDependencyLog()


def protect_mcp_logs() -> None:
    # SDK logging happens on named children, not on their parent's filters.
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.split(".", 1)[0] in {"mcp", "httpx", "httpx2", "httpcore", "httpcore2"} or name in {
            "uvicorn.error", "uvicorn.asgi",
        }:
            logging.getLogger(name).addFilter(_FILTER)
