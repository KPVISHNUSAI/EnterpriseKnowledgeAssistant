"""The configured sentence-transformers model, for real (slow: loads EMBEDDING_MODEL,
default BAAI/bge-small-en-v1.5, from the local Hugging Face cache; run with
`pytest -m slow`). Also calibrates app/ingestion/tokens.py against its tokenizer."""

from __future__ import annotations

import inspect
import math
from collections.abc import Sequence

import pytest

from app.core.config import get_settings
from app.ingestion import chunking
from app.ingestion.chunking import chunk_blocks, embedding_text
from app.ingestion.tokens import count_tokens
from app.ingestion.types import Block, BlockKind
from app.providers.embeddings.sentence_transformers import SentenceTransformerEmbedder

pytestmark = pytest.mark.slow

PROSE = (
    "Enterprise Knowledge Assistant answers questions from an organization's documents. "
    "Employees upload handbooks, policies and runbooks; the worker extracts the text, splits "
    "it into passages and indexes them for hybrid search. Our quarterly revenue grew by "
    "12.5% to $4,210,000 compared with the previous year, driven by internationalization "
    "efforts and considerably improved customer retention. Answers cite their sources, and "
    "documents a user may not read are never retrieved for them."
)
TABLE = (
    "| Region | Q1 2025 | Q2 2025 | Growth |\n| --- | --- | --- | --- |\n"
    "| North America | 1,204,332 | 1,350,120 | 12.1% |\n| EMEA | 980,001 | 1,010,500 | 3.1% |\n"
    "| APAC | 450,220 | 610,999 | 35.7% |"
)
IDENTIFIERS = (
    "Request 3f9a2c7e-11b4-4d2a-9c3e-0a1b2c3d4e5f failed with code E1043 at "
    "2025-10-17T23:55:01Z on host ip-10-0-3-17; sha256 "
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
)
JARGON = (
    "Pharmacokinetic studies of the immunomodulatory glycoprotein demonstrated "
    "hepatotoxicity and nephrotoxicity in pediatric cohorts; electroencephalography revealed "
    "paroxysmal epileptiform discharges."
)
CODE = inspect.getsource(chunking)


@pytest.fixture(scope="module")
def embedder() -> SentenceTransformerEmbedder:
    pytest.importorskip("sentence_transformers")
    return SentenceTransformerEmbedder.from_settings(get_settings())


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # inputs are unit vectors


def real_tokens(embedder: SentenceTransformerEmbedder, text: str) -> int:
    tokenizer = embedder._load().tokenizer
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])


def test_vectors_have_the_configured_dimension_and_unit_length(embedder):
    vectors = embedder.embed_documents([PROSE, TABLE, "short"])
    assert len(vectors) == 3
    for vector in vectors:
        assert len(vector) == get_settings().embedding_dimensions
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-4)


def test_queries_get_the_instruction_prefix(embedder):
    prefix = get_settings().embedding_query_prefix
    assert prefix  # BGE models expect an instruction on the query side
    question = "How many vacation days do employees get?"
    query = embedder.embed_query(question)
    assert cosine(query, embedder.embed_documents([prefix + question])[0]) > 0.9999
    assert cosine(query, embedder.embed_documents([question])[0]) < 0.999


def test_similar_meaning_scores_higher(embedder):
    query = embedder.embed_query("How many vacation days do employees get?")
    vacation, lunch = embedder.embed_documents(
        [
            "Every employee receives 25 days of paid annual leave per calendar year.",
            "The cafeteria serves lunch every day between noon and two.",
        ]
    )
    assert cosine(query, vacation) > cosine(query, lunch) + 0.1


@pytest.mark.parametrize(
    ("text", "low", "high"),
    [
        (PROSE, 1.0, 1.5),
        (TABLE, 1.0, 1.25),
        (IDENTIFIERS, 1.0, 1.25),
        (CODE, 0.95, 1.2),
        (JARGON, 0.85, 1.1),  # rare words split into more pieces than common ones
    ],
    ids=["prose", "table", "identifiers", "code", "jargon"],
)
def test_token_estimate_matches_the_documented_calibration(embedder, text, low, high):
    real = real_tokens(embedder, text)
    assert low * real <= count_tokens(text) <= high * real


def test_full_size_chunks_fit_the_model_window(embedder):
    settings = get_settings()
    blocks = [
        Block(BlockKind.PARAGRAPH, " ".join([PROSE] * 12), ("Overview",)),
        Block(BlockKind.CODE, f"```python\n{CODE}\n```", ("Overview", "Implementation")),
        Block(BlockKind.TABLE, TABLE + "\n" + "\n".join(TABLE.split("\n")[2:] * 40), ("Data",)),
    ]
    chunks = chunk_blocks(
        blocks,
        target_tokens=settings.chunk_target_tokens,
        max_tokens=settings.chunk_max_tokens,
        overlap_tokens=settings.chunk_overlap_tokens,
    )
    assert len(chunks) > 5
    window = embedder._load().max_seq_length
    for chunk in chunks:
        text = embedding_text("Employee Handbook", chunk)
        assert real_tokens(embedder, text) + 2 <= window  # + [CLS] and [SEP]
