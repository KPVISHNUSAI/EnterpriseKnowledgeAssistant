"""Server-Sent Events from a blocking producer.

The RAG pipeline and LLM SDKs are synchronous, so the producer runs in its own thread
and hands encoded events to the event loop through an asyncio.Queue. While the producer
is quiet (e.g. the model is thinking) the bridge sends comment heartbeats, which keep
proxies from timing out and surface client disconnects. When the client goes away the
bridge sets `cancel`; the producer then stops generating and persists the partial answer.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

HEARTBEAT = b": keep-alive\n\n"
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",  # no-transform: proxies must not gzip/buffer
    "X-Accel-Buffering": "no",
}

_DONE = object()


def encode_event(event: str, data: BaseModel | dict[str, Any]) -> bytes:
    payload = (
        data.model_dump_json() if isinstance(data, BaseModel) else json.dumps(data, default=str)
    )
    return f"event: {event}\ndata: {payload}\n\n".encode()


async def stream_from_thread(
    produce: Callable[[threading.Event], Iterator[bytes]],
    *,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[bytes]:
    """Yield what `produce(cancel)` yields, running it in a dedicated thread.

    The producer must check `cancel.is_set()` between expensive steps and return
    promptly once it is set (the client is gone).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    cancel = threading.Event()

    def put(item: object) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # event loop closed (server shutting down)
            cancel.set()

    def run() -> None:
        try:
            for chunk in produce(cancel):
                put(chunk)
                if cancel.is_set():
                    break
        except Exception:
            logger.exception("sse.producer_failed")
            put(
                encode_event(
                    "error",
                    {"code": "internal_error", "message": "The response failed unexpectedly"},
                )
            )
        finally:
            put(_DONE)

    context = contextvars.copy_context()  # keeps request_id on the producer's log lines
    threading.Thread(target=context.run, args=(run,), name="sse-producer", daemon=True).start()
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield HEARTBEAT
                continue
            if item is _DONE:
                return
            assert isinstance(item, bytes)  # noqa: S101 - internal invariant
            yield item
    finally:
        cancel.set()
