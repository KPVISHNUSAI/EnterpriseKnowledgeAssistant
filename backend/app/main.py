"""ASGI application factory.

uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8081
"""

from __future__ import annotations

import hmac
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import __version__
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.errors import error_body, install_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    OriginCheckMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.providers.registry import get_embedder, get_reranker

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Enterprise Knowledge Assistant: permission-aware document search and grounded,
cited question answering over an organization's documents.

**Authentication.** `POST /api/v1/auth/login` sets an HttpOnly session cookie and a
readable `eka_csrf` cookie. Send the CSRF cookie's value in the `X-CSRF-Token` header on
every POST/PUT/PATCH/DELETE. Errors always use the envelope
`{"error": {"code", "message", "details", "request_id"}}`.
"""


def _warm_up_models() -> None:
    """Load the embedding and reranker models before the first search needs them: a cold
    load takes seconds (the very first start also downloads them)."""
    try:
        get_embedder().embed_query("warm-up")
        reranker = get_reranker()
        if reranker is not None:
            reranker.score("warm-up", ["warm-up"])
    except Exception:  # the first search loads them again and reports the error
        logger.warning("models.warm_up_failed", exc_info=True)
    else:
        logger.info("models.warmed_up")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # In a thread, so sign-in and health checks are served while the models load; a
    # search that arrives meanwhile waits on the provider's load lock instead of loading twice.
    threading.Thread(target=_warm_up_models, name="model-warm-up", daemon=True).start()
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=API_DESCRIPTION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=_lifespan,
    )
    install_exception_handlers(app)
    app.include_router(api_router, prefix="/api/v1")

    if settings.metrics_enabled:
        token = settings.metrics_token.get_secret_value() if settings.metrics_token else None

        def metrics(request: Request) -> Response:
            if token is not None and not hmac.compare_digest(
                request.headers.get("authorization", ""), f"Bearer {token}"
            ):
                return JSONResponse(error_body("unauthorized", "Invalid token"), status_code=401)
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        app.add_api_route("/metrics", metrics, methods=["GET"], include_in_schema=False)

    # Starlette applies the last-added middleware outermost:
    # RequestContext > SecurityHeaders > CORS > BodySizeLimit > OriginCheck > routes.
    app.add_middleware(OriginCheckMiddleware, settings=settings)
    app.add_middleware(BodySizeLimitMiddleware, settings=settings)
    # Only needed when the browser calls the API cross-origin; the frontend normally
    # proxies /api through its own origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", settings.csrf_header_name, "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    logger.info(
        "app.started",
        extra={
            "env": settings.app_env,
            "storage": settings.storage_backend,
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "reranker": settings.reranker_provider,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model if settings.llm_provider != "extractive" else None,
            "retrieval_mode": settings.retrieval_mode,
        },
    )
    return app
