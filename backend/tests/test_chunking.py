"""Chunking: size limits, sentence-aligned overlap, verbatim code and tables, sections,
pages, and the text sent to the embedding model."""

from __future__ import annotations

from itertools import pairwise

import pytest

from app.ingestion.chunking import HEADER_MAX_CHARS, chunk_blocks, embedding_text
from app.ingestion.tokens import count_tokens
from app.ingestion.types import Block, BlockKind, Chunk

P, C, T, L, H = (
    BlockKind.PARAGRAPH,
    BlockKind.CODE,
    BlockKind.TABLE,
    BlockKind.LIST,
    BlockKind.HEADING,
)
TARGET, MAX, OVERLAP = 60, 100, 15


def chunk(
    blocks: list[Block], target: int = TARGET, max_tokens: int = MAX, overlap: int = OVERLAP
) -> list[Chunk]:
    return chunk_blocks(blocks, target_tokens=target, max_tokens=max_tokens, overlap_tokens=overlap)


def sentences(count: int, topic: str = "topic") -> str:
    """`count` sentences of 12 estimated tokens each."""
    return " ".join(
        f"Sentence number {i} talks about {topic} {i} in some detail." for i in range(count)
    )


def new_text(chunk_: Chunk) -> str:
    """The chunk's content after the part repeated from the previous chunk."""
    return chunk_.content[chunk_.metadata.get("overlap_chars", 0) :]


def squash(text: str) -> str:
    return "".join(text.split())


def code_block(lines: int) -> str:
    body = "\n".join(f"    total_{i} = compute(value_{i}, factor={i})" for i in range(lines))
    return f"```python\ndef run():\n{body}\n```"


def test_token_estimate():
    assert count_tokens("") == 0
    assert count_tokens("hello world") == 2  # short words are one token each
    assert count_tokens("internationalization") == 5  # long words: about four letters a token
    assert count_tokens("2025") == 2
    assert count_tokens("v2beta3") == 6  # identifiers are expensive
    assert count_tokens("a, b.") == 4  # punctuation counts
    assert count_tokens(chr(0x65E5) + chr(0x672C) + chr(0x8A9E)) == 3  # one per CJK character
    assert count_tokens(sentences(1)) == 12


def test_chunks_stay_within_the_limits_and_lose_nothing():
    blocks = [
        Block(P, sentences(40)),
        Block(C, code_block(4)),
        Block(P, sentences(10, "costs")),
        Block(T, "| a | b |\n| --- | --- |\n| 1 | 2 |"),
        Block(P, sentences(3, "risks")),
    ]
    chunks = chunk(blocks)
    assert len(chunks) > 5
    for item in chunks:
        assert item.token_count == count_tokens(item.content)
        assert item.token_count <= MAX
        if item.metadata["kinds"] == ["paragraph"]:
            assert item.token_count <= TARGET
    original = squash("".join(block.text for block in blocks))
    assert squash("".join(new_text(item) for item in chunks)) == original


def test_overlap_repeats_whole_trailing_sentences():
    chunks = chunk([Block(P, sentences(30))])
    assert "overlap_chars" not in chunks[0].metadata
    for previous, current in pairwise(chunks):
        overlap = current.content[: current.metadata["overlap_chars"]]
        assert previous.content.endswith(overlap)
        assert overlap.startswith("Sentence number") and overlap.endswith("detail.")
        assert 0 < count_tokens(overlap) <= OVERLAP
        assert current.content[len(overlap)] == " "  # cut at the sentence boundary


def test_no_overlap_when_disabled():
    chunks = chunk([Block(P, sentences(30))], overlap=0)
    assert all("overlap_chars" not in item.metadata for item in chunks)
    assert squash("".join(c.content for c in chunks)) == squash(sentences(30))


def test_sentence_boundaries_skip_abbreviations_and_follow_quotes():
    text = 'First point, e.g. a detail here. He said "Stop." Then he left.'
    assert [c.content for c in chunk([Block(P, text)], target=12, overlap=0)] == [
        "First point, e.g. a detail here.",
        'He said "Stop." Then he left.',
    ]


def test_cjk_sentences_are_kept_whole():
    sentence = "".join(map(chr, (0x3053, 0x308C, 0x306F, 0x6587, 0x3067, 0x3059, 0x3002)))
    text = sentence * 40  # no spaces between sentences
    chunks = chunk([Block(P, text)], overlap=0)
    assert len(chunks) > 1
    assert all(item.content.endswith(chr(0x3002)) for item in chunks)
    assert all(item.token_count <= TARGET for item in chunks)
    assert "".join(c.content for c in chunks) == text


def test_long_sentence_is_split_at_words_and_long_word_at_characters():
    sentence = " ".join(f"word{i % 7}x" for i in range(200))  # no sentence end at all
    word = "x" * 1000
    for text in (sentence, word):
        chunks = chunk([Block(P, text)])
        assert len(chunks) > 1
        assert all(item.token_count <= TARGET for item in chunks)
        assert squash("".join(new_text(item) for item in chunks)) == squash(text)


def test_code_block_that_fits_is_never_split():
    code = code_block(5)
    assert TARGET < count_tokens(code) <= MAX
    chunks = chunk([Block(P, sentences(4)), Block(C, code), Block(P, sentences(2, "after"))])
    assert [code in item.content for item in chunks].count(True) == 1
    with_code = next(item for item in chunks if code in item.content)
    assert with_code.metadata["kinds"] == ["code", "paragraph"]
    # The text after a code block never carries part of the code as overlap.
    assert chunks[-1].content.startswith("Sentence number 0 talks about after")


def test_short_code_block_stays_with_the_text_that_introduces_it():
    intro = "Install the tool with the command below."
    code = code_block(5)
    chunks = chunk([Block(P, intro), Block(C, code)])
    assert [item.content for item in chunks] == [f"{intro}\n\n{code}"]
    assert chunks[0].token_count > TARGET  # allowed: code may extend a chunk up to MAX


def test_oversized_code_block_is_split_on_lines_and_refenced():
    code = code_block(60)
    chunks = chunk([Block(C, code)])
    assert len(chunks) > 3
    body: list[str] = []
    for item in chunks:
        lines = item.content.split("\n")
        assert lines[0] == "```python" and lines[-1] == "```"
        assert item.token_count <= MAX
        body.extend(lines[1:-1])
    assert body == code.split("\n")[1:-1]


def test_oversized_table_repeats_its_header_row():
    header = "| id | name | value |\n| --- | --- | --- |"
    rows = [f"| {i} | item {i} | {i * 7} |" for i in range(60)]
    chunks = chunk([Block(T, "\n".join([header, *rows]))])
    assert len(chunks) > 3
    seen: list[str] = []
    for item in chunks:
        assert item.content.startswith(header + "\n")
        assert item.token_count <= MAX
        seen.extend(item.content.split("\n")[2:])
    assert seen == rows


def test_oversized_line_in_code_is_broken_at_words():
    long_line = " ".join(f"arg{i}" for i in range(300))
    chunks = chunk([Block(C, f"```\n{long_line}\n```")])
    assert all(item.token_count <= MAX for item in chunks)
    assert squash("".join(c.content.replace("```", "") for c in chunks)) == squash(long_line)


def test_sections_are_never_mixed():
    blocks = [
        Block(H, "Guide", ("Guide",)),
        Block(P, "Alpha text.", ("Guide",)),
        Block(H, "Setup", ("Guide", "Setup")),
        Block(P, "Beta text.", ("Guide", "Setup")),
        Block(P, "Gamma text.", ("Appendix",)),
        Block(P, "Delta text.", ()),
    ]
    chunks = chunk(blocks)
    assert [(c.content, c.heading_path, c.section_title, c.heading_path_text) for c in chunks] == [
        ("Alpha text.", ("Guide",), "Guide", "Guide"),
        ("Beta text.", ("Guide", "Setup"), "Setup", "Guide > Setup"),
        ("Gamma text.", ("Appendix",), "Appendix", "Appendix"),
        ("Delta text.", (), None, None),
    ]
    assert all("overlap_chars" not in c.metadata for c in chunks)


def test_page_ranges():
    blocks = [Block(P, sentences(2), (), page) for page in (1, 1, 2, 3)]  # 24 tokens each
    chunks = chunk(blocks, overlap=0)
    assert [(c.page_number, c.page_end) for c in chunks] == [(1, 1), (2, 3)]
    assert [(c.page_number, c.page_end) for c in chunk([Block(P, "No pages.")])] == [(None, None)]


def test_block_that_fits_a_fresh_chunk_is_moved_whole():
    first, second = sentences(3, "first"), sentences(3, "second")  # 36 tokens each
    chunks = chunk([Block(P, first), Block(P, second)])
    assert [new_text(c).strip() for c in chunks] == [first, second]


def test_consecutive_list_blocks_join_with_single_newlines():
    blocks = [Block(L, "- one\n- two"), Block(L, "- three"), Block(P, "After the list.")]
    assert [c.content for c in chunk(blocks)] == ["- one\n- two\n- three\n\nAfter the list."]


def test_chunking_is_deterministic_and_skips_empty_input():
    blocks = [Block(P, sentences(25)), Block(C, code_block(30)), Block(P, sentences(5))]
    assert chunk(blocks) == chunk(blocks)
    assert chunk([]) == []
    assert chunk([Block(H, "Only a heading", ("Only a heading",))]) == []


@pytest.mark.parametrize(
    ("title", "path", "expected"),
    [
        ("Employee Guide", ("Benefits", "Dental"), "Employee Guide > Benefits > Dental\n\nBody"),
        ("Handbook", ("handbook", "Leave"), "Handbook > Leave\n\nBody"),  # title not repeated
        ("  ", ("Leave",), "Leave\n\nBody"),
        ("", (), "Body"),
    ],
)
def test_embedding_text_has_a_contextual_header(title, path, expected):
    item = Chunk(content="Body", token_count=1, heading_path=path, page_number=None, page_end=None)
    assert embedding_text(title, item) == expected


def test_embedding_text_header_is_capped():
    item = Chunk(
        content="Body", token_count=1, heading_path=("x" * 400,), page_number=1, page_end=1
    )
    header, content = embedding_text("Title", item).split("\n\n")
    assert len(header) == HEADER_MAX_CHARS and content == "Body"
