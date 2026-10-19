"""Application errors and the single error envelope returned by every endpoint:

{"error": {"code": "not_found", "message": "...", "details": ..., "request_id": "..."}}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400
    code = "bad_request"
    message = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
        background: BackgroundTask | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details
        self.headers = headers
        self.background = background  # runs after the error response has been sent
        super().__init__(self.message)


class UnauthorizedError(AppError):
    status_code, code, message = 401, "unauthorized", "Authentication required"


class ForbiddenError(AppError):
    status_code, code, message = 403, "forbidden", "You do not have permission to do this"


class NotFoundError(AppError):
    status_code, code, message = 404, "not_found", "Not found"


class ConflictError(AppError):
    status_code, code, message = 409, "conflict", "Conflict"


class PayloadTooLargeError(AppError):
    status_code, code, message = 413, "payload_too_large", "Request body too large"


class UnsupportedMediaTypeError(AppError):
    status_code, code, message = 415, "unsupported_media_type", "Unsupported file type"


class ValidationFailedError(AppError):
    status_code, code, message = 422, "validation_error", "Validation failed"


class RateLimitedError(AppError):
    status_code, code, message = 429, "rate_limited", "Too many requests, slow down"


class ServiceUnavailableError(AppError):
    status_code, code, message = 503, "service_unavailable", "Service temporarily unavailable"


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
            "request_id": request_id_var.get(),
        }
    }


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, exc.details),
            status_code=exc.status_code,
            headers=exc.headers,
            background=exc.background,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Echoing the rejected input could leak passwords, so only report location + reason.
        details = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body("validation_error", "Request validation failed", details),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ")
        return JSONResponse(
            error_body(code, message),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("request.unhandled_error", extra={"error_type": type(exc).__name__})
        return JSONResponse(error_body("internal_error", "Internal server error"), status_code=500)
