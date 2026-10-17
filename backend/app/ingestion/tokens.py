"""Token estimate without a tokenizer dependency.

Chunk sizes are budgets for the embedding model's input window (512 tokens for the BGE
family). Text is split the way WordPiece/BPE pre-tokenizers split it - runs of letters or
digits, single punctuation marks, single CJK characters - and long or alphanumeric runs
count as several sub-word pieces. Measured against the BAAI/bge-small-en-v1.5 tokenizer
(tests/test_embeddings_model.py): 0-45% high on everyday English, within a few percent on
source code, about 10% high on tables and identifiers, but up to 10% low on jargon-dense
English and about 30% low on long German compounds. Prose chunks stop at
CHUNK_TARGET_TOKENS (350 by default), which leaves room for that in a 512-token window;
lower the chunk settings for corpora dominated by such text.
"""

from __future__ import annotations

import re
from math import ceil

_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
_PIECE = re.compile(rf"[{_CJK}]|[^\W_{_CJK}]+|\S")


def count_tokens(text: str) -> int:
    total = 0
    for match in _PIECE.finditer(text):
        piece = match.group()
        size = len(piece)
        if size == 1:
            total += 1
        elif piece.isalpha():
            total += 1 if size <= 6 else ceil(size / 4)
        elif piece.isdigit():
            total += ceil(size / 2)
        else:  # letters mixed with digits: identifiers, hashes, versions
            total += ceil(size / 1.2)
    return total
