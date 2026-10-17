"""Token usage and estimated cost per assistant turn: usage_records rows for the admin
usage report and Prometheus counters. Prices come from settings; nothing here sees text."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.metrics import LLM_COST, LLM_TOKENS
from app.models import UsageKind, UsageRecord
from app.rag.types import AnswerResult, TokenUsage


def estimate_cost(input_tokens: int, output_tokens: int, *, rewrite: bool = False) -> float:
    """USD from the per-million-token prices; rewrites use their own prices when set."""
    s = get_settings()
    input_price = s.llm_input_cost_per_mtok
    output_price = s.llm_output_cost_per_mtok
    if rewrite:
        if s.llm_rewrite_input_cost_per_mtok is not None:
            input_price = s.llm_rewrite_input_cost_per_mtok
        if s.llm_rewrite_output_cost_per_mtok is not None:
            output_price = s.llm_rewrite_output_cost_per_mtok
    return round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)


def record(
    db: Session,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    result: AnswerResult,
    message_id: uuid.UUID,
) -> None:
    """One chat_completion row per assistant turn (zero tokens without an LLM call) and one
    query_rewrite row per rewrite LLM call, added to the caller's transaction."""
    timings = result.timings_ms
    model = result.model or result.provider
    rows = [
        _row(
            organization_id,
            user_id,
            UsageKind.CHAT_COMPLETION,
            result.provider,
            model,
            result.usage,
            # The admin usage report reads exactly these keys.
            {
                "retrieval_ms": float(timings.get("retrieval", 0.0)),
                "generation_ms": float(timings.get("generation", 0.0)),
                "total_ms": float(timings.get("total", 0.0)),
            },
            message_id,
        )
    ]
    if result.rewrite_usage is not None:
        rows.append(
            _row(
                organization_id,
                user_id,
                UsageKind.QUERY_REWRITE,
                result.provider,
                result.rewrite_model or model,
                result.rewrite_usage,
                {"total_ms": float(timings.get("rewrite", 0.0))},
                message_id,
            )
        )
    db.add_all(rows)


def _row(
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    kind: UsageKind,
    provider: str,
    model: str,
    usage: TokenUsage,
    timings: dict[str, float],
    message_id: uuid.UUID,
) -> UsageRecord:
    LLM_TOKENS.labels(provider, model, "input").inc(usage.input_tokens)
    LLM_TOKENS.labels(provider, model, "output").inc(usage.output_tokens)
    LLM_COST.labels(provider, model).inc(usage.cost_usd)
    return UsageRecord(
        organization_id=organization_id,
        user_id=user_id,
        kind=kind,
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=Decimal(str(usage.cost_usd)),  # via str: no binary-float residue
        latency_ms=round(timings["total_ms"]),
        timings=timings,
        message_id=message_id,
    )
