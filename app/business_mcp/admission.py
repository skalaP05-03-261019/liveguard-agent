"""Bound in-flight HTTP requests through the final response body, including streams."""
from fastapi.responses import JSONResponse
from starlette.datastructures import State
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestAdmission:
    def __init__(self, app: ASGIApp, *, state: State, capacity: int) -> None:
        self.app, self.state, self.capacity = app, state, capacity
        self.active = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            return await self.app(scope, receive, send)
        if (not getattr(self.state, "started", False)
                or getattr(self.state, "draining", False) or self.active >= self.capacity):
            response = JSONResponse(
                status_code=503,
                content={"error": {"code": "TEMPORARY_UNAVAILABLE", "retryable": True}},
                headers={"Retry-After": "1"},
            )
            return await response(scope, receive, send)
        # No await between the check and increment: one ASGI event loop owns this counter.
        self.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.active -= 1
