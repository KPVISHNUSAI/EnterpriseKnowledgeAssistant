"""The API as the end-to-end suite starts it:

    uvicorn e2e_app:create_app --factory --app-dir e2e/stack ...   (from the backend directory)

It is app.main.create_app() with one change for timing only. The extractive answer generator
produces its whole text in microseconds, so no test could see the answer arrive in pieces through
the Next.js proxy or interrupt it with Stop. E2E_STREAM_DELAY_MS makes the generator wait that
long before each word group; what it yields (and so every status, citation and sentence) is
untouched.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator, Sequence

from fastapi import FastAPI

from app.main import create_app as create_real_app
from app.rag import answer
from app.rag.types import ContextPassage


def create_app() -> FastAPI:
    delay = int(os.environ["E2E_STREAM_DELAY_MS"]) / 1000
    stream_extractive = answer.stream_extractive

    def paced(question: str, passages: Sequence[ContextPassage]) -> Generator[str, None, None]:
        for chunk in stream_extractive(question, passages):
            time.sleep(delay)
            yield chunk

    answer.stream_extractive = paced
    return create_real_app()
