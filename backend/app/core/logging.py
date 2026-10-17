"""Structured JSON logging with request/job correlation and secret redaction.

Convention: the log message is a dotted event name (``ingestion.completed``) and the
details go in ``extra={...}``. Never pass document contents, prompts or credentials.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
org_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("org_id", default=None)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "user_id": user_id_var,
    "org_id": org_id_var,
    "job_id": job_id_var,
}

_SENSITIVE_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
)

# Attributes every LogRecord has; anything else came from `extra=`.
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def _redact(value: Any, key: str = "") -> Any:
    if key and any(fragment in key.lower() for fragment in _SENSITIVE_FRAGMENTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name, var in _CONTEXT_VARS.items():
            value = var.get()
            if value:
                payload[name] = value
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact(value, key)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly single-line format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: _redact(v, k)
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        context = {name: var.get() for name, var in _CONTEXT_VARS.items() if var.get()}
        line = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if context or extras:
            line += " " + json.dumps({**context, **extras}, default=str)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn's access log duplicates our request log line.
    logging.getLogger("uvicorn.access").disabled = True
    # HTTP clients and the LLM SDKs log request bodies (prompts, documents) at DEBUG.
    for noisy in (
        "httpx",
        "httpx2",
        "httpcore",
        "anthropic",
        "botocore",
        "urllib3",
        "sentence_transformers",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
