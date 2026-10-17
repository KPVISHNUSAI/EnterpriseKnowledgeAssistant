"""Prometheus metrics. Exposed at /metrics on the API and on WORKER_METRICS_PORT by the worker."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)

HTTP_REQUESTS = Counter("eka_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "eka_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
RETRIEVAL_LATENCY = Histogram(
    "eka_retrieval_duration_seconds",
    "Retrieval stage latency",
    ["stage"],
    buckets=LATENCY_BUCKETS,
)
GENERATION_LATENCY = Histogram(
    "eka_generation_duration_seconds",
    "LLM answer generation latency",
    ["provider", "model"],
    buckets=LATENCY_BUCKETS,
)
LLM_TOKENS = Counter(
    "eka_llm_tokens_total", "LLM tokens consumed", ["provider", "model", "direction"]
)
LLM_COST = Counter("eka_llm_cost_usd_total", "Estimated LLM cost (USD)", ["provider", "model"])
LLM_ERRORS = Counter("eka_llm_errors_total", "LLM provider errors", ["provider", "kind"])
EMBEDDING_CACHE = Counter("eka_embedding_cache_total", "Embedding cache lookups", ["result"])
INGESTION_JOBS = Counter("eka_ingestion_jobs_total", "Ingestion job outcomes", ["status"])
INGESTION_LATENCY = Histogram(
    "eka_ingestion_duration_seconds",
    "Ingestion job duration",
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 900),
)
