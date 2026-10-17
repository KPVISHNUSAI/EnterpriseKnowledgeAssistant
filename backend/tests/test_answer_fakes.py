"""Test doubles shared by the generation/chat tests (no tests in this module).

`FakeLLM` implements the LLMProvider protocol with scripted output and records every
request, so tests can assert exactly what the model was shown.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

from app.providers.llm import (
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMStreamEnd,
    LLMStreamEvent,
    LLMTextDelta,
    LLMUsage,
)
from app.rag.types import Candidate, ContextPassage

LEAVE = (
    "Employees receive 25 days of paid vacation per year. "
    "Vacation requests need approval from a manager."
)
CAFETERIA = "The cafeteria opens at 8am and closes at 3pm."
ANSWER = "STATUS: ANSWERED\n\nEmployees receive 25 days of paid vacation per year [S1]."


def index_handbook(factory: Any, tenant: Any) -> Any:
    """A workspace-visible 'Employee Handbook' with two indexed chunks (S1 = LEAVE)."""
    doc = factory.document(
        tenant.workspace,
        uploaded_by=tenant.editor,
        title="Employee Handbook",
        mime_type="text/markdown",
    )
    factory.chunks(
        doc,
        [{"content": LEAVE, "page_number": 3, "section_title": "Leave"}, CAFETERIA],
    )
    return doc


class FakeLLM:
    """Streams `text` in `chunk`-sized deltas; `generate()` answers rewrites with `rewrite`.

    `error` is raised before the first delta, or after `fail_after` deltas when set.
    `delay` sleeps before each delta (cancellation tests). `closed_early` records whether
    the consumer closed the stream before it finished."""

    provider_name = "fake"
    model_name = "fake-model"

    def __init__(
        self,
        text: str = ANSWER,
        *,
        stop_reason: str = "end_turn",
        rewrite: str | None = None,
        error: LLMError | None = None,
        rewrite_error: LLMError | None = None,
        fail_after: int | None = None,
        usage: tuple[int, int] = (120, 30),
        delay: float = 0.0,
        chunk: int = 6,
    ) -> None:
        self.text = text
        self.stop_reason = stop_reason
        self.rewrite = rewrite
        self.error = error
        self.rewrite_error = rewrite_error
        self.fail_after = fail_after
        self.usage = usage
        self.delay = delay
        self.chunk = chunk
        self.requests: list[LLMRequest] = []
        self.closed_early = False

    @property
    def answer_requests(self) -> list[LLMRequest]:
        return [r for r in self.requests if r.purpose == "answer"]

    @property
    def rewrite_requests(self) -> list[LLMRequest]:
        return [r for r in self.requests if r.purpose == "query_rewrite"]

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if request.purpose == "query_rewrite":
            if self.rewrite_error is not None:
                raise self.rewrite_error
            return LLMResponse(
                text=self.rewrite or "",
                model=request.model or self.model_name,
                usage=LLMUsage(10, 5),
                stop_reason="end_turn",
            )
        if self.error is not None:
            raise self.error
        return self._response()

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        self.requests.append(request)
        finished = False
        try:
            if self.error is not None and self.fail_after is None:
                raise self.error
            for index, start in enumerate(range(0, len(self.text), self.chunk)):
                if self.error is not None and index == self.fail_after:
                    raise self.error
                if self.delay:
                    time.sleep(self.delay)
                yield LLMTextDelta(self.text[start : start + self.chunk])
            yield LLMStreamEnd(self._response())
            finished = True
        finally:
            self.closed_early = not finished

    def _response(self) -> LLMResponse:
        return LLMResponse(
            text=self.text,
            model=self.model_name,
            usage=LLMUsage(*self.usage),
            stop_reason=self.stop_reason,
        )


def passage(
    source_id: str,
    text: str,
    *,
    title: str = "Employee Handbook",
    page: int | None = None,
    page_end: int | None = None,
    section: str | None = None,
    document_id: uuid.UUID | None = None,
) -> ContextPassage:
    """A ContextPassage with a minimal Candidate, for tests that need no database."""
    candidate = Candidate(
        chunk_id=uuid.uuid4(),
        document_id=document_id or uuid.uuid4(),
        document_title=title,
        workspace_id=uuid.uuid4(),
        mime_type="text/plain",
        chunk_index=0,
        content=text,
        content_hash="0" * 64,
        token_count=len(text.split()),
        page_number=page,
        page_end=page_end if page_end is not None else page,
        section_title=section,
        heading_path=section,
        rrf_score=0.03,
    )
    return ContextPassage(
        source_id=source_id,
        candidate=candidate,
        text=text,
        token_count=candidate.token_count,
        chunk_ids=[candidate.chunk_id],
    )
