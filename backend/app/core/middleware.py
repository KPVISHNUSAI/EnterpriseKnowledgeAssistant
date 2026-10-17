"""Pure ASGI middlewares (no BaseHTTPMiddleware, so SSE responses stream unbuffered)."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from app.core.config import Settings
from app.core.errors import error_body
from app.core.logging import org_id_var, request_id_var, user_id_var
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = logging.getLogger("app.request")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_UPLOAD_PATH = re.compile(r"^/api/v1/documents/(upload|[0-9a-fA-F-]{36}/file)$")


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return str(value.decode("latin-1"))
    return None


async def _send_json(send: Send, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RequestContextMiddleware:
    """Assigns a request id, logs one line per request and records HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        tokens = [request_id_var.set(request_id), user_id_var.set(None), org_id_var.set(None)]
        status_holder = {"status": 500}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            route_path = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "")
            status = status_holder["status"]
            HTTP_REQUESTS.labels(method, route_path, str(status)).inc()
            HTTP_LATENCY.labels(method, route_path).observe(elapsed)
            if route_path not in ("/api/v1/health", "/metrics"):
                state = scope.get("state") or {}
                logger.info(
                    "request.completed",
                    extra={
                        "method": method,
                        "route": route_path,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 1),
                        "user_id": state.get("user_id"),
                        "org_id": state.get("organization_id"),
                    },
                )
            for var, token in zip((request_id_var, user_id_var, org_id_var), tokens, strict=True):
                var.reset(token)


class BodySizeLimitMiddleware:
    """Rejects oversized bodies before they are buffered or spooled to disk.

    Upload routes get MAX_UPLOAD_MB (+ multipart overhead); everything else gets
    MAX_REQUEST_BODY_KB. Chunked bodies without Content-Length are counted as they stream.
    """

    _MULTIPART_OVERHEAD = 64 * 1024

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.upload_limit = settings.max_upload_bytes + self._MULTIPART_OVERHEAD
        self.default_limit = settings.max_request_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.upload_limit if _UPLOAD_PATH.match(scope["path"]) else self.default_limit
        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _send_json(send, 413, error_body("payload_too_large", "Request body too large"))
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if not response_started:
                await _send_json(
                    send, 413, error_body("payload_too_large", "Request body too large")
                )


class _BodyTooLargeError(Exception):
    pass


class OriginCheckMiddleware:
    """Blocks cross-site state-changing requests from browsers.

    Requests that carry an Origin header must come from an allowed origin. Requests
    without one (curl, server-to-server) pass here and still need a valid CSRF token
    when they use cookie authentication.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.allowed = set(settings.allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") in _UNSAFE_METHODS:
            origin = _header(scope, b"origin")
            if origin is not None and origin.rstrip("/") not in self.allowed:
                await _send_json(send, 403, error_body("origin_not_allowed", "Origin not allowed"))
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_docs = scope["path"].startswith("/api/docs") or scope["path"].endswith("openapi.json")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                ]
                if not is_docs:
                    defaults.append((b"cache-control", b"no-store"))
                    defaults.append(
                        (b"content-security-policy", b"default-src 'none'; frame-ancestors 'self'")
                    )
                headers.extend((k, v) for k, v in defaults if k not in present)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
