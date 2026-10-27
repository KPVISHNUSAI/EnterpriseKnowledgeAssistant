"""The grounded answer pipeline, independent of HTTP (the chat service and the evaluation
suite both drive it):

    normalize -> rewrite follow-up -> retrieve -> build context -> generate -> validate

Generation streams (LLM or the offline extractive generator) behind one STATUS-line
parser, so the UI sees tokens early and never sees the status marker. With no passages
there is no model call at all: the answer is a fixed abstention. A message that is only
small talk ("hi", "thanks", "what can you do?") skips retrieval too and gets a fixed reply.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Generator, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.metrics import GENERATION_LATENCY
from app.providers.embeddings import EmbeddingProvider
from app.providers.llm import (
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMTextDelta,
)
from app.providers.rerankers import Reranker
from app.rag.citations import final_status, validate
from app.rag.claims import Verdict, check_claims
from app.rag.context import build_context
from app.rag.extractive import INSUFFICIENT_MESSAGE, stream_extractive
from app.rag.prompts import SYSTEM_PROMPT, build_messages, source_label
from app.rag.query import normalize_query, small_talk_reply
from app.rag.retrieval import retrieve
from app.rag.rewrite import Rewrite, rewrite_query
from app.rag.types import (
    AnswerCompleted,
    AnswerDelta,
    AnswerResult,
    AnswerStatus,
    Candidate,
    ChatTurn,
    ContextPassage,
    PipelineEvent,
    RetrievalCompleted,
    RetrievalOptions,
    RetrievalResult,
    SearchScope,
    TokenUsage,
)
from app.services.usage import estimate_cost

logger = logging.getLogger(__name__)

REFUSED_MESSAGE = "The assistant declined to answer this request. Try rephrasing the question."
ERROR_MESSAGE = (
    "The answer could not be generated because the language model did not respond. "
    "Please try again."
)
RATE_LIMITED_MESSAGE = (
    "The language model's usage limit was reached, so no answer was generated. "
    "Wait a minute, then ask again."
)


def failure_message(kind: str) -> str:
    """What the user is told when generation failed with an LLMError of this kind."""
    return RATE_LIMITED_MESSAGE if kind == LLMRateLimitError.kind else ERROR_MESSAGE


_STATUS_LINE = re.compile(
    r"^[\s*#_]*STATUS\s*:\s*(ANSWERED|PARTIAL|INSUFFICIENT)\b.*$", re.IGNORECASE
)
_STATUS_PROBE_CHARS = 40  # longer than any status line: stop holding text back
# Answers that make claims, so their statements are checked (the evaluation's "judged").
_CLAIMING = (AnswerStatus.ANSWERED, AnswerStatus.PARTIAL, AnswerStatus.UNVERIFIED)


def answer_question(
    db: Session,
    scope: SearchScope,
    question: str,
    *,
    history: Sequence[ChatTurn],
    options: RetrievalOptions,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    llm: LLMProvider | None,
) -> AnswerResult:
    events = stream_answer(
        db,
        scope,
        question,
        history=history,
        options=options,
        embedder=embedder,
        reranker=reranker,
        llm=llm,
    )
    return next(event.result for event in events if isinstance(event, AnswerCompleted))


def stream_answer(
    db: Session,
    scope: SearchScope,
    question: str,
    *,
    history: Sequence[ChatTurn],
    options: RetrievalOptions,
    embedder: EmbeddingProvider,
    reranker: Reranker | None,
    llm: LLMProvider | None,
    cancel: threading.Event | None = None,
) -> Generator[PipelineEvent, None, None]:
    """RetrievalCompleted once, AnswerDelta zero or more times, AnswerCompleted once.
    `cancel` (set when the client is gone) stops generation between deltas; the result
    then holds what was produced, flagged `metadata["cancelled"]`."""
    settings = get_settings()
    started = time.perf_counter()
    timings: dict[str, float] = {}
    query = normalize_query(question, max_chars=settings.max_query_chars)
    small_talk = small_talk_reply(query)

    rewrite = Rewrite(query)
    if small_talk is not None:
        retrieval = RetrievalResult(query, query, [], 0, options, reranked=False)
        passages: list[ContextPassage] = []
    else:
        if history and settings.query_rewrite_enabled:
            with _timed(timings, "rewrite"):
                rewrite = rewrite_query(query, history, llm=llm)
            if rewrite.usage is not None:
                rewrite.usage.cost_usd = estimate_cost(
                    rewrite.usage.input_tokens, rewrite.usage.output_tokens, rewrite=True
                )
        with _timed(timings, "retrieval"):
            retrieval = retrieve(
                db,
                scope,
                query,
                options=options,
                embedder=embedder,
                reranker=reranker,
                search_query=rewrite.query,
            )
            passages = build_context(retrieval.candidates, max_tokens=settings.context_max_tokens)
    yield RetrievalCompleted(retrieval=retrieval, passages=passages)

    result = AnswerResult(
        question=query,
        answer=INSUFFICIENT_MESSAGE,
        status=AnswerStatus.INSUFFICIENT_EVIDENCE,
        retrieval=retrieval,
        passages=passages,
        citations=[],
        provider=llm.provider_name if llm is not None else "extractive",
        model=llm.model_name if llm is not None else None,
        rewrite_usage=rewrite.usage,
        rewrite_model=rewrite.model,
        timings_ms=timings,
    )
    if small_talk is not None:
        # Not a claim about the documents: nothing to cite, so no receipt either.
        result.answer, result.status = small_talk, AnswerStatus.ANSWERED
        result.metadata["conversational"] = True
        yield AnswerDelta(small_talk)
    elif not passages:
        yield AnswerDelta(INSUFFICIENT_MESSAGE)
    else:
        outcome = _Outcome()
        with _timed(timings, "generation"):
            if llm is None:
                chunks = stream_extractive(query, passages)
            else:
                request = LLMRequest(
                    system=SYSTEM_PROMPT,
                    messages=build_messages(query, passages, history),
                    purpose="answer",
                )
                chunks = _llm_chunks(llm, request, outcome)
            yield from _relay(chunks, outcome, cancel)
        _finish(result, outcome, passages)
        if result.status in _CLAIMING:
            # The model also sees each source's title, section and page, so "page 3 of the
            # manual says ..." is checked against them too.
            texts = {p.source_id: f"{source_label(p.candidate)}\n{p.text}" for p in passages}
            pages = {p.source_id: _pages(p.candidate) for p in passages}
            result.claims = check_claims(result.answer, query, texts, pages=pages)
        GENERATION_LATENCY.labels(result.provider, result.model or result.provider).observe(
            timings["generation"] / 1000
        )
    timings["total"] = _elapsed_ms(started)
    logger.info(
        "answer.completed",
        extra={
            "status": result.status.value,
            "rewritten": rewrite.query != query,
            "passages": len(passages),
            "citations": len(result.citations),
            "invalid_citations": len(result.invalid_citations),
            "unverified_quotes": len(result.unverified_quotes),
            "claims": len(result.claims),
            "unsupported_claims": sum(c.verdict is not Verdict.SUPPORTED for c in result.claims),
            "provider": result.provider,
            "model": result.model,
            "flags": sorted(result.metadata),
            "timings_ms": timings,
        },
    )
    yield AnswerCompleted(result=result)


@dataclass
class _Outcome:
    """What generation produced, filled in while the chunks stream."""

    text: str = ""  # visible text (status line removed)
    declared: str | None = None  # ANSWERED | PARTIAL | INSUFFICIENT
    stop_reason: str | None = None
    model: str | None = None
    usage: TokenUsage | None = None
    error: LLMError | None = None
    cancelled: bool = False


def _llm_chunks(
    llm: LLMProvider, request: LLMRequest, outcome: _Outcome
) -> Generator[str, None, None]:
    """Raw text deltas from the provider. A provider failure ends the stream and is
    recorded on the outcome; closing this generator closes the provider stream."""
    stream = llm.stream(request)
    try:
        for event in stream:
            if isinstance(event, LLMTextDelta):
                yield event.text
            else:
                response = event.response
                outcome.model = response.model
                outcome.stop_reason = response.stop_reason
                outcome.usage = TokenUsage(
                    response.usage.input_tokens, response.usage.output_tokens
                )
    except LLMError as exc:
        logger.warning(
            "answer.llm_failed",
            extra={"kind": exc.kind, "retryable": exc.retryable, "purpose": request.purpose},
        )
        outcome.error = exc
    finally:
        # Providers return generators (the contract: closing releases the HTTP stream);
        # the protocol only promises an Iterator, hence the lookup.
        close = getattr(stream, "close", None)
        if close is not None:
            close()


def _relay(
    chunks: Generator[str, None, None], outcome: _Outcome, cancel: threading.Event | None
) -> Generator[AnswerDelta, None, None]:
    """Feed raw chunks through the status-line parser and yield the visible deltas.
    Stops between chunks once `cancel` is set, closing the chunk source."""
    parser = _StatusLine()
    with closing(chunks):
        if cancel is not None and cancel.is_set():
            outcome.cancelled = True
            return
        for chunk in chunks:
            visible = parser.feed(chunk)
            if visible:
                outcome.text += visible
                yield AnswerDelta(visible)
            if cancel is not None and cancel.is_set():
                outcome.cancelled = True
                break
    tail = parser.flush()
    if tail:
        outcome.text += tail
        yield AnswerDelta(tail)
    outcome.declared = parser.status


def _finish(result: AnswerResult, outcome: _Outcome, passages: list[ContextPassage]) -> None:
    """Validate citations and settle the status; refusals, provider errors, truncation
    and cancellation override or annotate it."""
    text, declared = outcome.text.strip(), outcome.declared
    if declared is None:
        declared, text = _late_status(text)
    validated = validate(text, passages)
    result.answer = validated.text
    result.citations = validated.citations
    result.invalid_citations = validated.invalid_citations
    result.unverified_quotes = validated.unverified_quotes
    result.status = final_status(declared, validated.citations)
    if outcome.model is not None:
        result.model = outcome.model
    if outcome.usage is not None:
        result.usage = outcome.usage
        result.usage.cost_usd = estimate_cost(
            outcome.usage.input_tokens, outcome.usage.output_tokens
        )
    if not validated.text and not outcome.cancelled and outcome.error is None:
        result.answer = INSUFFICIENT_MESSAGE  # the model said nothing usable
        result.status = AnswerStatus.INSUFFICIENT_EVIDENCE
    if outcome.stop_reason == "refusal":
        result.answer = REFUSED_MESSAGE
        result.status = AnswerStatus.REFUSED
        result.citations = []
    if outcome.stop_reason == "max_tokens":
        result.metadata["truncated"] = True
    if outcome.cancelled:
        result.metadata["cancelled"] = True
    if outcome.error is not None:
        error = outcome.error
        result.status = AnswerStatus.ERROR
        result.answer = validated.text or failure_message(error.kind)
        result.metadata["error"] = {"kind": error.kind, "retryable": error.retryable}


class _StatusLine:
    """Strips the leading "STATUS: X" line from streamed text. Text is held back only
    while it still looks like the start of such a line, so answers are never delayed by
    more than one line."""

    def __init__(self) -> None:
        self.status: str | None = None
        self._held = ""
        self._decided = False
        self._skip_blank = False  # drop the blank line after the status line

    def feed(self, text: str) -> str:
        if self._decided:
            return self._visible(text)
        self._held += text
        head = self._held.lstrip()
        if "\n" in head:
            line, rest = head.split("\n", 1)
            return self._decide(line, rest)
        if len(head) > _STATUS_PROBE_CHARS or not _could_be_status(head):
            self._decided = True
            return self._held
        return ""

    def flush(self) -> str:
        """Text still held when the stream ends (a status line alone, or a one-line
        answer without a newline)."""
        if self._decided:
            return ""
        return self._decide(self._held.strip(), "")

    def _decide(self, line: str, rest: str) -> str:
        self._decided = True
        match = _STATUS_LINE.match(line)
        if match is None:
            return self._held
        self.status = match.group(1).upper()
        self._skip_blank = True
        return self._visible(rest)

    def _visible(self, text: str) -> str:
        if self._skip_blank:
            text = text.lstrip()
            self._skip_blank = not text
        return text


def _pages(candidate: Candidate) -> range:
    if candidate.page_number is None:
        return range(0)
    return range(candidate.page_number, (candidate.page_end or candidate.page_number) + 1)


def _late_status(text: str) -> tuple[str | None, str]:
    """A status line written after the answer instead of first (small models do this):
    its status, and the text without that line. Streamed deltas already showed the line;
    the final message, which replaces them, does not."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _STATUS_LINE.match(line)
        if match is not None:
            rest = "\n".join(lines[:index] + lines[index + 1 :]).strip()
            return match.group(1).upper(), rest
    return None, text


def _could_be_status(head: str) -> bool:
    probe = head.lstrip("*#_ ").upper()
    return probe.startswith("STATUS") or "STATUS".startswith(probe)


@contextmanager
def _timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = _elapsed_ms(started)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
