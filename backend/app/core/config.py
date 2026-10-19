"""Centralized, validated configuration.

Every environment-specific value (URLs, model names, bucket names, limits, secrets)
comes from environment variables or a `.env` file. Nothing environment-specific is
hardcoded elsewhere in the codebase: modules call `get_settings()`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application -------------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Enterprise Knowledge Assistant"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    secret_key: SecretStr = Field(min_length=32)
    public_base_url: str = "http://localhost:3000"
    allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    trust_proxy_headers: bool = False  # honour X-Forwarded-For (only behind a trusted LB)

    # --- Database ----------------------------------------------------------------
    database_url: str
    migration_database_url: str | None = None
    db_app_role: str = "eka_app"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_statement_timeout_ms: int = 30_000

    # --- Redis / background worker ---------------------------------------------------
    redis_url: str = "redis://localhost:6381/0"
    celery_broker_url: str | None = None
    ingestion_max_attempts: int = 3
    ingestion_lease_seconds: int = 900
    ingestion_retry_base_seconds: int = 30
    ingestion_stale_queued_seconds: int = 300
    worker_metrics_port: int = 9101

    # --- Object storage ----------------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_path: Path = BACKEND_DIR / "var" / "storage"
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_prefix: str = ""
    s3_presign_ttl_seconds: int = 300
    s3_sse: Literal["AES256", "aws:kms", "none"] = "AES256"
    s3_kms_key_id: str | None = None

    # --- Upload and request limits -------------------------------------------------------
    max_upload_mb: int = 50
    max_request_body_kb: int = 1024
    max_pdf_pages: int = 2000
    max_pdf_uncompressed_mb: int = 200  # inflated stream data one PDF may decode
    max_docx_uncompressed_mb: int = 200

    # --- Authentication / sessions -------------------------------------------------------
    session_cookie_name: str = "eka_session"
    csrf_cookie_name: str = "eka_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    session_ttl_hours: int = 12
    session_idle_timeout_minutes: int = 120
    cookie_secure: bool = False
    cookie_domain: str | None = None
    allow_public_registration: bool = True
    invitation_ttl_hours: int = 168
    password_min_length: int = 12
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 65536
    argon2_parallelism: int = 4

    # --- Rate limits ---------------------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 10
    rate_limit_register_per_hour: int = 20
    rate_limit_chat_per_minute: int = 20
    rate_limit_search_per_minute: int = 60
    rate_limit_upload_per_minute: int = 20

    # --- Embeddings ----------------------------------------------------------------------
    embedding_provider: Literal["sentence_transformers", "openai_compatible", "hashing"] = (
        "sentence_transformers"
    )
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Must equal the vector(N) column created by the migrations (see docs/ARCHITECTURE.md).
    embedding_dimensions: int = 384
    embedding_batch_size: int = 32
    embedding_device: str = "auto"
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedding_document_prefix: str = ""
    embedding_api_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = 30.0
    embedding_max_retries: int = 3
    embedding_cache_ttl_seconds: int = 7 * 24 * 3600

    # --- Reranker ------------------------------------------------------------------------
    reranker_provider: Literal["cross_encoder", "none"] = "cross_encoder"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    reranker_device: str = "auto"
    reranker_batch_size: int = 32
    reranker_max_length: int = 512

    # --- LLM -----------------------------------------------------------------------------
    llm_provider: Literal["anthropic", "openai_compatible", "extractive"] = "extractive"
    llm_model: str = "claude-opus-5-5"
    # Optional cheaper model for follow-up query rewriting (defaults to llm_model).
    llm_query_rewrite_model: str | None = None
    llm_max_output_tokens: int = 16_000
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    # Sampling temperature, applied by the openai_compatible provider only (current
    # Claude models reject sampling parameters).
    llm_temperature: float | None = 0.1
    llm_input_cost_per_mtok: float = 0.0
    llm_output_cost_per_mtok: float = 0.0
    llm_rewrite_input_cost_per_mtok: float | None = None
    llm_rewrite_output_cost_per_mtok: float | None = None
    anthropic_api_key: SecretStr | None = None
    anthropic_platform: Literal["anthropic", "bedrock"] = "anthropic"
    anthropic_effort: Effort = "medium"
    anthropic_rewrite_effort: Effort = "low"
    anthropic_fallbacks: Literal["default", "off"] = "default"
    aws_region: str | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None

    # --- Retrieval -----------------------------------------------------------------------
    retrieval_mode: Literal["vector", "keyword", "hybrid"] = "hybrid"
    rerank_enabled: bool = True
    retrieval_vector_top_k: int = 40
    retrieval_keyword_top_k: int = 40
    retrieval_rrf_k: int = 60
    retrieval_fused_top_k: int = 30
    retrieval_final_top_k: int = 8
    retrieval_min_rerank_score: float | None = None
    context_max_tokens: int = 6000
    hnsw_ef_search: int = 100
    fts_config: str = "english"
    query_rewrite_enabled: bool = True
    max_query_chars: int = 2000
    chat_history_messages: int = 6
    search_max_results: int = 50

    # --- Chunking ------------------------------------------------------------------------
    chunk_target_tokens: int = 350
    chunk_max_tokens: int = 500
    chunk_overlap_tokens: int = 50

    # --- Retention -----------------------------------------------------------------------
    conversation_retention_days: int = 365
    audit_retention_days: int = 730
    usage_retention_days: int = 400

    # --- Observability -------------------------------------------------------------------
    metrics_enabled: bool = True
    metrics_token: SecretStr | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
        return value

    @field_validator("fts_config", "db_app_role")
    @classmethod
    def _sql_identifier(cls, value: str) -> str:
        # Interpolated into DDL, so restrict to a plain identifier.
        if not _IDENTIFIER.match(value):
            raise ValueError("must be a lowercase SQL identifier")
        return value

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.chunk_overlap_tokens >= self.chunk_target_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_TARGET_TOKENS")
        if self.chunk_target_tokens > self.chunk_max_tokens:
            raise ValueError("CHUNK_TARGET_TOKENS must not exceed CHUNK_MAX_TOKENS")
        if self.storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        if self.app_env == "production":
            weak = self.secret_key.get_secret_value().lower()
            if any(marker in weak for marker in ("change", "replace", "example", "dev")):
                raise ValueError("SECRET_KEY looks like a placeholder; generate a real secret")
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true in production")
            if self.embedding_provider == "hashing":
                raise ValueError("EMBEDDING_PROVIDER=hashing is for tests only")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_request_body_kb * 1024

    @property
    def query_rewrite_model(self) -> str:
        return self.llm_query_rewrite_model or self.llm_model


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
