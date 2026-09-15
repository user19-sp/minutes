"""Request middleware: trace-id propagation, structured access logs, metrics,
and a hard body-size ceiling.
"""

from __future__ import annotations

import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from backend.app.config import settings
from backend.app.observability.logging import get_logger, trace_id_ctx, user_id_ctx
from backend.app.observability.metrics import (
    http_request_duration_seconds,
    http_requests_total,
)

log = get_logger("http")

TRACE_HEADER = "X-Trace-Id"


def _route_template(request: Request) -> str:
    """Use the route pattern, not the raw path, so /jobs/{id} is one metric series
    instead of one series per job (unbounded cardinality)."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns every request a trace id, logs it, and records latency."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = request.headers.get(TRACE_HEADER) or str(uuid.uuid4())
        trace_token = trace_id_ctx.set(trace_id)
        user_token = user_id_ctx.set(None)
        request.state.trace_id = trace_id

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            path = _route_template(request)
            if path != "/metrics":  # don't let scraping pollute its own metrics
                http_requests_total.labels(
                    method=request.method, path=path, status=str(status_code)
                ).inc()
                http_request_duration_seconds.labels(method=request.method, path=path).observe(
                    elapsed
                )
                log.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    route=path,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                    client=request.client.host if request.client else None,
                )
            user_id_ctx.reset(user_token)
            trace_id_ctx.reset(trace_token)


class BodySizeLimitMiddleware:
    """Reject oversized uploads at the edge, before anything is buffered to disk.

    Declared Content-Length is checked first; a chunked request with no declared
    length is metered as it streams so a lying client cannot get past the ceiling.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes or settings.allowed_upload_bytes

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass

        received = 0
        too_large = False

        async def metered_receive():  # type: ignore[no-untyped-def]
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, metered_receive, send)

    async def _reject(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        log.warning("upload_rejected_too_large", path=scope.get("path"), limit=self.max_bytes)
        response = JSONResponse(
            status_code=413,
            content={"detail": f"Upload exceeds the {settings.max_upload_mb} MB limit."},
        )
        await response(scope, receive, send)
