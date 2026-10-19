"""Embedding providers and the Redis embedding cache (no model downloads, no network:
HTTP goes through httpx.MockTransport and sentence-transformers is faked)."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import socket
import struct
import sys
import threading
import time
import types
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import redis
from prometheus_client import REGISTRY
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.core.redis import get_redis
from app.providers.embeddings import EmbeddingError, HashingEmbedder, cache, openai_compatible
from app.providers.embeddings.openai_compatible import OpenAICompatibleEmbedder
from app.providers.embeddings.sentence_transformers import (
    SentenceTransformerEmbedder,
    resolve_device,
)
from app.providers.registry import get_embedder

# --- Cache --------------------------------------------------------------------------------


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def cache_total(result: str) -> float:
    return REGISTRY.get_sample_value("eka_embedding_cache_total", {"result": result}) or 0.0


class CountingEmbedder(HashingEmbedder):
    """Hashing vectors under a model name unique to the test (the Redis database is
    shared with concurrent test runs)."""

    def __init__(self, dimensions: int = 8) -> None:
        super().__init__(dimensions, model_name=f"test-model-{uuid.uuid4().hex}")
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return super().embed_documents(texts)


@pytest.fixture
def embedder() -> Iterator[CountingEmbedder]:
    instance = CountingEmbedder()
    yield instance
    keys = list(get_redis().scan_iter(f"emb:v2:*:{instance.model_name}:*"))
    if keys:
        get_redis().delete(*keys)


ORG = uuid.uuid4()  # cache entries are per organization; keys include the model name too


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_cache_misses_then_hits(embedder):
    texts = ["alpha", "beta", "gamma"]
    hashes = [sha(text) for text in texts]
    hit, miss = cache_total("hit"), cache_total("miss")

    vectors, hits = cache.embed_with_cache(ORG, embedder, texts, hashes, batch_size=2)
    assert hits == 0
    assert embedder.calls == [["alpha", "beta"], ["gamma"]]
    assert vectors == HashingEmbedder(8).embed_documents(texts)

    cached, hits = cache.embed_with_cache(ORG, embedder, texts, hashes, batch_size=2)
    assert hits == 3 and len(embedder.calls) == 2  # nothing embedded again
    for fresh, stored in zip(vectors, cached, strict=True):
        assert stored == pytest.approx(fresh, abs=1e-6)  # stored as float32

    _, hits = cache.embed_with_cache(
        ORG, embedder, [*texts, "delta"], [*hashes, sha("delta")], batch_size=2
    )
    assert hits == 3 and embedder.calls[-1] == ["delta"]
    assert cache_total("hit") - hit == 6
    assert cache_total("miss") - miss == 4


def test_entries_are_private_to_their_organization(embedder):
    """SEC-01: another tenant embedding byte-identical text must not be served (or be
    able to time) the first tenant's entries, nor drop them by invalidating."""
    texts = ["Board minutes: the merger closes in March."]
    hashes = [sha(text) for text in texts]
    other = uuid.uuid4()
    vectors, _ = cache.embed_with_cache(ORG, embedder, texts, hashes, batch_size=8)

    _, hits = cache.embed_with_cache(other, embedder, texts, hashes, batch_size=8)
    assert hits == 0 and len(embedder.calls) == 2  # embedded again for the other tenant
    (own_key,) = cache._keys(ORG, embedder.model_name, hashes)
    (other_key,) = cache._keys(other, embedder.model_name, hashes)
    assert own_key != other_key and str(ORG) in own_key

    cache.store(other, embedder.model_name, {hashes[0]: [0.5] * 8})  # the other tenant's write
    (own,) = cache.lookup(ORG, embedder.model_name, hashes, 8)
    assert own == pytest.approx(vectors[0], abs=1e-6)


def test_invalidate_leaves_other_organizations_alone():
    model = get_embedder().model_name
    hashes = [uuid.uuid4().hex for _ in range(2)]
    first, second = uuid.uuid4(), uuid.uuid4()
    for org in (first, second):
        cache.store(org, model, {content_hash: [0.25] * 4 for content_hash in hashes})
    cache.invalidate(first, hashes)
    assert cache.lookup(first, model, hashes, 4) == [None, None]
    assert None not in cache.lookup(second, model, hashes, 4)
    cache.invalidate(second, hashes)


def test_cached_values_are_float32_with_the_configured_ttl(embedder):
    content_hash = sha("ttl")
    cache.store(ORG, embedder.model_name, {content_hash: [0.5] * 8})
    (key,) = cache._keys(ORG, embedder.model_name, [content_hash])
    raw = get_redis().get(key)
    assert raw == struct.pack("<8f", *[0.5] * 8)
    ttl = get_redis().ttl(key)
    assert 0 < ttl <= get_settings().embedding_cache_ttl_seconds


def test_cached_value_of_the_wrong_size_is_a_miss(embedder):
    content_hash = sha("short")
    (key,) = cache._keys(ORG, embedder.model_name, [content_hash])
    get_redis().set(key, b"\x00\x00\x80?", ex=60)  # one float for an 8-dimensional model
    assert cache.lookup(ORG, embedder.model_name, [content_hash], 8) == [None]


def test_document_prefix_is_part_of_the_key(embedder, monkeypatch):
    content_hash = sha("prefixed")
    cache.store(ORG, embedder.model_name, {content_hash: [1.0] * 8})
    monkeypatch.setattr(get_settings(), "embedding_document_prefix", "passage: ")
    assert cache.lookup(ORG, embedder.model_name, [content_hash], 8) == [None]


def test_ttl_zero_disables_the_cache(embedder, monkeypatch):
    monkeypatch.setattr(get_settings(), "embedding_cache_ttl_seconds", 0)
    for _ in range(2):
        _, hits = cache.embed_with_cache(ORG, embedder, ["x"], [sha("x")], batch_size=8)
        assert hits == 0
    assert len(embedder.calls) == 2
    assert not list(get_redis().scan_iter(f"emb:v2:*:{embedder.model_name}:*"))


def test_unavailable_redis_fails_open(embedder, monkeypatch, caplog):
    down = redis.Redis(host="127.0.0.1", port=closed_port(), socket_connect_timeout=0.5)
    monkeypatch.setattr(cache, "get_redis", lambda: down)
    errors = cache_total("error")
    with caplog.at_level(logging.WARNING, logger="app.providers.embeddings.cache"):
        vectors, hits = cache.embed_with_cache(
            ORG, embedder, ["a", "b"], [sha("a"), sha("b")], batch_size=8
        )
        cache.invalidate(ORG, [sha("a")])
    assert hits == 0 and len(vectors) == 2
    assert cache_total("error") - errors == 2
    assert caplog.messages.count("embedding_cache.unavailable") == 3  # lookup, store, invalidate


def test_invalidate_drops_the_configured_models_vectors():
    model = get_embedder().model_name
    hashes = [uuid.uuid4().hex for _ in range(3)]
    cache.store(ORG, model, {content_hash: [0.25] * 4 for content_hash in hashes})
    assert None not in cache.lookup(ORG, model, hashes, 4)
    cache.invalidate(ORG, [hashes[0], hashes[1], hashes[0]])
    assert [vector is None for vector in cache.lookup(ORG, model, hashes, 4)] == [True, True, False]
    cache.invalidate(ORG, hashes)
    cache.invalidate(ORG, [])  # no-op


def test_provider_returning_too_few_vectors_is_rejected(embedder, monkeypatch):
    monkeypatch.setattr(embedder, "embed_documents", lambda texts: [[1.0] * 8])
    with pytest.raises(EmbeddingError) as info:
        cache.embed_with_cache(ORG, embedder, ["a", "b"], [sha("a"), sha("b")], batch_size=8)
    assert not info.value.retryable


# --- OpenAI-compatible HTTP provider --------------------------------------------------------

API_KEY = "sk-test-secret-key-0000"


class Server:
    """Records requests and replies with queued responses (or exceptions)."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def inputs(self) -> list[list[str]]:
        return [json.loads(request.content)["input"] for request in self.requests]


def vectors_response(vectors: list[list[float]], *, reverse: bool = False) -> httpx.Response:
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    return httpx.Response(200, json={"object": "list", "data": data[::-1] if reverse else data})


def http_embedder(server: Server, **overrides: Any) -> OpenAICompatibleEmbedder:
    options: dict[str, Any] = {
        "base_url": "http://embeddings.test/v1/",
        "model_name": "test-embed",
        "dimensions": 3,
        "api_key": SecretStr(API_KEY),
        "max_retries": 2,
        "batch_size": 2,
    }
    options.update(overrides)
    return OpenAICompatibleEmbedder(
        client=httpx.Client(transport=httpx.MockTransport(server)), **options
    )


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    delays: list[float] = []
    monkeypatch.setattr(openai_compatible.time, "sleep", delays.append)
    return delays


def test_http_embeddings_request_order_and_normalization(sleeps):
    server = Server(vectors_response([[3, 4, 0], [0, 0, 2]], reverse=True))
    vectors = http_embedder(server).embed_documents(["first", "second"])
    assert vectors == [[0.6, 0.8, 0.0], [0.0, 0.0, 1.0]]  # sorted by index, unit length
    (request,) = server.requests
    assert str(request.url) == "http://embeddings.test/v1/embeddings"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {"model": "test-embed", "input": ["first", "second"]}
    assert sleeps == []


def test_http_embeddings_are_batched_and_prefixed():
    server = Server(vectors_response([[1, 0, 0], [0, 1, 0]]), vectors_response([[0, 0, 1]]))
    embedder = http_embedder(server, document_prefix="passage: ", query_prefix="query: ")
    vectors = embedder.embed_documents(["a", "b", "c"])
    assert vectors == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    server.replies = [vectors_response([[0, 1, 0]])]
    assert embedder.embed_query("q") == [0, 1, 0]
    assert server.inputs() == [["passage: a", "passage: b"], ["passage: c"], ["query: q"]]


def test_http_embeddings_without_api_key_send_no_authorization():
    server = Server(vectors_response([[1, 0, 0]]))
    http_embedder(server, api_key=None).embed_documents(["a"])
    assert "authorization" not in server.requests[0].headers


def test_rate_limit_honours_retry_after(sleeps):
    server = Server(
        httpx.Response(429, headers={"Retry-After": "2"}), vectors_response([[1, 0, 0]])
    )
    assert http_embedder(server).embed_documents(["a"]) == [[1, 0, 0]]
    assert sleeps == [2.0] and len(server.requests) == 2


def test_server_errors_and_connection_failures_are_retried_with_backoff(sleeps):
    server = Server(
        httpx.Response(503), httpx.ConnectError("refused"), vectors_response([[0, 2, 0]])
    )
    assert http_embedder(server).embed_documents(["a"]) == [[0, 1, 0]]
    assert len(sleeps) == 2
    assert 0.5 <= sleeps[0] <= 0.625 and 1.0 <= sleeps[1] <= 1.25  # exponential + jitter


@pytest.mark.parametrize(
    "reply", [httpx.Response(500), httpx.ReadTimeout("slow")], ids=["http-500", "timeout"]
)
def test_exhausted_retries_raise_a_retryable_error(sleeps, reply):
    server = Server(reply)
    with pytest.raises(EmbeddingError) as info:
        http_embedder(server).embed_documents(["a"])
    assert info.value.retryable
    assert len(server.requests) == 3 and len(sleeps) == 2  # max_retries=2


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (httpx.Response(401, json={"error": "bad key"}), "HTTP 401"),
        (httpx.Response(400), "HTTP 400"),
        (vectors_response([[1, 0]]), "2-dimensional"),
        (vectors_response([[1, 0, 0], [0, 1, 0]]), "2 vectors for 1"),
        (vectors_response([[0, 0, 0]]), "invalid vector"),
        (httpx.Response(200, json={"unexpected": True}), "unexpected response"),
        (httpx.Response(200, text="<html>proxy error</html>"), "unexpected response"),
    ],
    ids=["unauthorized", "bad-request", "dimension", "count", "zero", "shape", "not-json"],
)
def test_client_and_response_errors_are_not_retried(sleeps, reply, fragment):
    server = Server(reply)
    with pytest.raises(EmbeddingError) as info:
        http_embedder(server).embed_documents(["a"])
    assert not info.value.retryable
    assert fragment in str(info.value)
    assert len(server.requests) == 1 and sleeps == []


def test_retry_after_parsing():
    def parse(value: str) -> float | None:
        return openai_compatible._retry_after(httpx.Response(429, headers={"Retry-After": value}))

    assert parse("3") == 3.0
    assert parse("600") == 60.0  # capped
    assert parse("garbage") is None
    soon = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= (parse(soon) or 0) <= 30
    past = format_datetime(datetime.now(UTC) - timedelta(minutes=5), usegmt=True)
    assert parse(past) == 0.0


def test_api_key_is_never_logged_or_raised(sleeps, caplog):
    server = Server(httpx.Response(503), httpx.Response(401))
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError) as info:
        http_embedder(server).embed_documents(["a"])
    formatter = JsonFormatter()
    logged = "\n".join(formatter.format(record) for record in caplog.records)
    assert "embeddings.request_retry" in logged
    assert API_KEY not in logged and API_KEY not in str(info.value)
    assert API_KEY not in repr(http_embedder(server).__dict__.get("_api_key"))


def test_http_embedder_from_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "embedding_api_base_url", None)
    with pytest.raises(EmbeddingError) as info:
        OpenAICompatibleEmbedder.from_settings(settings)
    assert not info.value.retryable
    monkeypatch.setattr(settings, "embedding_api_base_url", "http://embeddings.test/v1")
    embedder = OpenAICompatibleEmbedder.from_settings(settings)
    assert embedder.model_name == settings.embedding_model
    assert embedder.dimensions == settings.embedding_dimensions


# --- sentence-transformers (faked) --------------------------------------------------------


class FakeModel:
    def __init__(self, dimension: int = 3, fail: Exception | None = None) -> None:
        self.dimension = dimension
        self.fail = fail
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def get_embedding_dimension(self) -> int:
        return self.dimension

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        if self.fail:
            raise self.fail
        self.calls.append((list(texts), kwargs))
        return [[1.0, 0.0, 0.0] for _ in texts]


def fake_library(monkeypatch, model: FakeModel | Exception) -> list[dict[str, Any]]:
    """Install a stand-in `sentence_transformers` module; returns constructor calls."""
    created: list[dict[str, Any]] = []

    def construct(name: str, device: str) -> FakeModel:
        created.append({"name": name, "device": device})
        if isinstance(model, Exception):
            raise model
        return model

    module = types.SimpleNamespace(SentenceTransformer=construct)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return created


def local_embedder(**overrides: Any) -> SentenceTransformerEmbedder:
    options: dict[str, Any] = {
        "dimensions": 3,
        "device": "cpu",
        "query_prefix": "query: ",
        "document_prefix": "doc: ",
    }
    options.update(overrides)
    return SentenceTransformerEmbedder("org/fake-model", **options)


def test_resolve_device(monkeypatch):
    assert resolve_device("cpu") == "cpu"
    assert resolve_device("cuda:1") == "cuda:1"
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert resolve_device("auto") == "cuda"
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: True)
    assert resolve_device("auto") == "mps"
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    assert resolve_device("auto") == "cpu"


def test_model_loads_lazily_once_across_threads_and_applies_prefixes(monkeypatch, caplog):
    model = FakeModel()
    created = fake_library(monkeypatch, model)
    embedder = local_embedder(batch_size=16)
    assert embedder.embed_documents([]) == [] and created == []  # nothing loaded yet

    original = embedder._create

    def slow_create() -> Any:
        time.sleep(0.05)  # widen the race window
        return original()

    monkeypatch.setattr(embedder, "_create", slow_create)
    with caplog.at_level(logging.INFO, logger="app.providers.embeddings.sentence_transformers"):
        threads = [
            threading.Thread(target=embedder.embed_documents, args=([f"t{i}"],)) for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert created == [{"name": "org/fake-model", "device": "cpu"}]
    assert sorted(texts[0] for texts, _ in model.calls) == sorted(f"doc: t{i}" for i in range(8))
    assert caplog.messages.count("embeddings.model_loaded") == 1

    assert embedder.embed_query("where") == [1.0, 0.0, 0.0]
    texts, options = model.calls[-1]
    assert texts == ["query: where"]
    assert options["normalize_embeddings"] is True and options["batch_size"] == 16


def test_model_dimension_mismatch_is_a_configuration_error(monkeypatch):
    fake_library(monkeypatch, FakeModel(dimension=768))
    with pytest.raises(EmbeddingError) as info:
        local_embedder().embed_query("x")
    assert not info.value.retryable and "768" in str(info.value)


def test_missing_library_is_a_configuration_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(EmbeddingError) as info:
        local_embedder().embed_query("x")
    assert not info.value.retryable


def test_load_and_encode_failures_are_retryable(monkeypatch):
    fake_library(monkeypatch, OSError("disk full"))
    with pytest.raises(EmbeddingError) as info:
        local_embedder().embed_query("x")
    assert info.value.retryable

    fake_library(monkeypatch, FakeModel(fail=RuntimeError("out of memory")))
    with pytest.raises(EmbeddingError) as info:
        local_embedder().embed_documents(["x"])
    assert info.value.retryable


def test_sentence_transformer_from_settings():
    settings = get_settings()
    embedder = SentenceTransformerEmbedder.from_settings(settings)
    assert (embedder.model_name, embedder.dimensions) == (
        settings.embedding_model,
        settings.embedding_dimensions,
    )
    assert embedder._model is None  # loading waits for the first request


def test_normalized_vectors_have_unit_length():
    server = Server(vectors_response([[1, 2, 2]]))
    (vector,) = http_embedder(server).embed_documents(["a"])
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0)
