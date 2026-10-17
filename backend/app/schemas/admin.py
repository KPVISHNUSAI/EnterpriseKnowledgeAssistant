from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class AuditEventOut(BaseModel):
    id: uuid.UUID
    action: str
    actor_user_id: uuid.UUID | None = None
    actor_email: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    metadata: dict[str, Any]
    ip_address: str | None = None
    request_id: str | None = None
    created_at: datetime


class UsageTotals(BaseModel):
    documents: int
    documents_by_status: dict[str, int]
    chunks: int
    storage_bytes: int
    conversations: int
    messages: int
    chat_requests: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class UsageDay(BaseModel):
    date: date
    chat_requests: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class LatencyStats(BaseModel):
    samples: int
    retrieval_p50_ms: float | None = None
    retrieval_p95_ms: float | None = None
    generation_p50_ms: float | None = None
    generation_p95_ms: float | None = None


class IngestionHealth(BaseModel):
    queued: int
    running: int
    failed_last_24h: int
    succeeded_last_24h: int


class UsageResponse(BaseModel):
    period_days: int
    totals: UsageTotals
    daily: list[UsageDay]
    latency: LatencyStats
    ingestion: IngestionHealth
