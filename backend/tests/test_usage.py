"""Usage records and cost estimation (app/services/usage.py)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from prometheus_client import REGISTRY
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import set_tenant
from app.models import UsageKind, UsageRecord
from app.rag.types import (
    AnswerResult,
    AnswerStatus,
    RetrievalOptions,
    RetrievalResult,
    TokenUsage,
)
from app.services import usage


def test_estimate_cost_uses_the_configured_prices(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_input_cost_per_mtok", 3.0)
    monkeypatch.setattr(settings, "llm_output_cost_per_mtok", 15.0)
    monkeypatch.setattr(settings, "llm_rewrite_input_cost_per_mtok", 0.8)
    monkeypatch.setattr(settings, "llm_rewrite_output_cost_per_mtok", None)
    assert usage.estimate_cost(1_000_000, 100_000) == 4.5
    assert usage.estimate_cost(1_000_000, 100_000, rewrite=True) == 2.3  # output price falls back
    assert usage.estimate_cost(0, 0) == 0.0
    assert usage.estimate_cost(1, 1) == 0.000018  # rounded to 6 decimals


def _result(**overrides) -> AnswerResult:
    options = RetrievalOptions.from_settings(get_settings())
    retrieval = RetrievalResult(
        query="q",
        search_query="q",
        candidates=[],
        total_candidates=0,
        options=options,
        reranked=False,
    )
    fields = {
        "question": "q",
        "answer": "a",
        "status": AnswerStatus.ANSWERED,
        "retrieval": retrieval,
        "passages": [],
        "citations": [],
        "provider": "fake",
        "model": "fake-model",
        "usage": TokenUsage(120, 30, 0.00042),
        "timings_ms": {"rewrite": 12.5, "retrieval": 40.25, "generation": 900.0, "total": 952.75},
    }
    return AnswerResult(**{**fields, **overrides})


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_record_writes_one_row_per_turn_and_per_rewrite(db, tenant):
    tokens_before = _sample(
        "eka_llm_tokens_total", provider="fake", model="fake-model", direction="input"
    )
    cost_before = _sample("eka_llm_cost_usd_total", provider="fake", model="fake-model")
    set_tenant(db, tenant.org.id)
    message_id = uuid.uuid4()
    usage.record(
        db,
        organization_id=tenant.org.id,
        user_id=tenant.viewer.id,
        result=_result(rewrite_usage=TokenUsage(10, 5, 0.000055), rewrite_model="fake-mini"),
        message_id=message_id,
    )
    db.commit()

    rows = {row.kind: row for row in db.scalars(select(UsageRecord)).all()}
    assert set(rows) == {UsageKind.CHAT_COMPLETION, UsageKind.QUERY_REWRITE}
    chat = rows[UsageKind.CHAT_COMPLETION]
    assert (chat.provider, chat.model, chat.input_tokens, chat.output_tokens) == (
        "fake",
        "fake-model",
        120,
        30,
    )
    assert chat.cost_usd == Decimal("0.000420")
    assert chat.timings == {"retrieval_ms": 40.25, "generation_ms": 900.0, "total_ms": 952.75}
    assert chat.latency_ms == 953
    assert (chat.message_id, chat.user_id, chat.organization_id) == (
        message_id,
        tenant.viewer.id,
        tenant.org.id,
    )
    rewrite = rows[UsageKind.QUERY_REWRITE]
    assert (rewrite.model, rewrite.input_tokens, rewrite.output_tokens) == ("fake-mini", 10, 5)
    assert rewrite.timings == {"total_ms": 12.5} and rewrite.latency_ms == 12
    assert rewrite.cost_usd == Decimal("0.000055")

    tokens_after = _sample(
        "eka_llm_tokens_total", provider="fake", model="fake-model", direction="input"
    )
    cost_after = _sample("eka_llm_cost_usd_total", provider="fake", model="fake-model")
    assert tokens_after - tokens_before == 120
    assert round(cost_after - cost_before, 6) == 0.00042


def test_extractive_turns_are_recorded_with_zero_tokens(db, tenant):
    set_tenant(db, tenant.org.id)
    result = _result(
        provider="extractive", model=None, usage=TokenUsage(), timings_ms={"total": 5.0}
    )
    usage.record(
        db,
        organization_id=tenant.org.id,
        user_id=tenant.viewer.id,
        result=result,
        message_id=uuid.uuid4(),
    )
    db.commit()
    (row,) = db.scalars(select(UsageRecord)).all()
    assert (row.kind, row.provider, row.model) == (
        UsageKind.CHAT_COMPLETION,
        "extractive",
        "extractive",
    )
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (0, 0, Decimal(0))
    assert row.timings == {"retrieval_ms": 0.0, "generation_ms": 0.0, "total_ms": 5.0}
