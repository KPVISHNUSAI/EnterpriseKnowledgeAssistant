# Retrieval

How a question becomes ranked, permission-checked passages: the pipeline behind
`POST /api/v1/search` and the chat answer pipeline. Code lives in `backend/app/rag/`
(`query.py`, `retrieval.py`, `context.py`, `highlight.py`) and
`backend/app/providers/rerankers/cross_encoder.py`.

```
question ─► normalize_query ─► SearchScope (principal + optional filters)
         ─► vector search  (embed_query, pgvector HNSW, cosine)      ┐  readable-documents
         ─► keyword search (tsvector @@ OR-tsquery, ts_rank_cd)     ┘  subquery inside SQL
         ─► Reciprocal Rank Fusion (fused_top_k)
         ─► cross-encoder rerank (optional, min score) ─► final_top_k candidates
         ─► search API: highlights + explanations    | chat: build_context ─► S1..Sn
```

## Entry points

| Function | Contract |
|---|---|
| `normalize_query(text, *, max_chars)` | NFKC; drops control, invisible-format (zero-width, soft hyphen, bidi) and lone-surrogate characters; collapses whitespace; trims to `max_chars` at a word boundary (a single over-long word is cut). Case, punctuation and stop words are kept. Raises `ValidationFailedError(code="empty_query")` (422) when nothing is left. |
| `retrieve(db, scope, query, *, options, embedder, reranker, search_query=None)` | `query` is the normalized question (`RetrievalResult.query`); `search_query` (a rewritten follow-up) is what is embedded, searched and reranked against, defaulting to `query`. Returns at most `options.final_top_k` candidates in final order. Runs in the caller's transaction and never commits. |
| `build_context(candidates, *, max_tokens)` | Turns candidates into `ContextPassage`s labelled `S1..Sn` (see [Context construction](#context-construction)). |
| `add_highlights(db, candidates, search_query)` | Search API only: sets `Candidate.highlights`. |

The session must already be tenant-bound (`set_tenant` in requests, `session_scope(org_id)`
elsewhere).

## Permission enforcement

* Both retrievers restrict chunks with
  `document_chunks.document_id IN (SELECT documents.id WHERE readable_documents_clause(principal) AND <filters>)`
  **inside** the ranking statement, before `ORDER BY`/`LIMIT`. Text the caller cannot read
  is never fetched, so it never reaches the reranker, the LLM or a highlight.
* `SearchScope.workspace_ids` / `document_ids` / `mime_types` are added to that subquery,
  so they can only narrow: an id the caller cannot read (another tenant's, a restricted
  document, a workspace they are not in) matches nothing. Nothing raises and nothing
  reveals that the id exists. `None` means "no filter"; an empty tuple matches nothing.
  The search API maps empty request lists to `None`.
* Row-level security is a second, independent layer: the predicate includes the
  principal's organization, and RLS hides other tenants' rows even if a session were
  bound to the wrong tenant. `tests/test_retrieval.py` checks both layers.

## Vector search

* `embedder.embed_query(search_query)`, then `embedding <=> :query` (cosine distance)
  `ORDER BY distance LIMIT vector_top_k`; `vector_similarity = 1 - distance`.
* Only chunks with `embedding_model = embedder.model_name` are compared. Chunks from a
  previous model are skipped until the document is re-indexed.
* Before the query, two transaction-local settings are applied:
  `set_config('hnsw.ef_search', HNSW_EF_SEARCH, true)` and
  `set_config('hnsw.iterative_scan', 'strict_order', true)`. pgvector ≥ 0.8 iterative
  scans keep walking the HNSW graph until `LIMIT` rows pass the permission filter. Without
  them, a filtered HNSW scan stops after `ef_search` candidates and can return too few
  rows when the nearest chunks belong to documents the caller cannot read. Both settings
  reset at commit or rollback.
* The statement is a single-table `ORDER BY <distance> LIMIT k` (metadata is joined
  afterwards), the shape the HNSW index can serve. With real statistics the planner
  picks the HNSW index for broad access and an exact plan (btree on
  `(organization_id, document_id)` + sort) for narrow scopes such as one document; both
  are correct. `test_vector_query_uses_the_hnsw_index_and_iterative_scans` EXPLAINs the
  real statement and fails if the shape stops being index-servable.
* An `EmbeddingError` becomes `ServiceUnavailableError(code="embedding_unavailable")` (503).

## Keyword search

* The search text is parsed with `to_tsvector(FTS_CONFIG, …)`: stemmed lexemes with stop
  words removed, in query order. Each lexeme is quoted (quotes and backslashes escaped)
  and the lexemes are joined with `|`. That gives **OR semantics** (partial matches still
  rank), and no user text is ever interpreted as tsquery operators. A query of only stop
  words has no keyword hits.
* `search_vector @@ query` (GIN index), ranked by `ts_rank_cd(search_vector, query, 1|32)`:
  normalized by chunk length and scaled into 0..1. `search_vector` weights the heading
  path `A` and the content `B`, so heading matches rank higher. Ties break by chunk id.
* `matched_terms` = the query lexemes present in the chunk, in query order. They are
  stems (`polici` for "policies"), because that is what matched.
* The text search configuration is always `settings.fts_config` (a validated identifier
  bound as a parameter), never user input. It must match the configuration the migration
  baked into `search_vector`.

## Fusion and reranking

* **RRF**: `rrf_score = Σ 1 / (rrf_k + rank)` over the lists that found the chunk (1-based
  ranks). A chunk found by both retrievers keeps both sets of evidence. Ties keep
  vector-first order. Single-mode results get `rrf_score` from their one list. The top
  `fused_top_k` are kept; `total_candidates` counts the unique chunks before that cut.
* **Rerank** runs when `options.rerank` is true, a reranker exists and there are
  candidates. Each fused candidate is scored as `"{heading_path}\n\n{content}"` (or just
  the content) against `search_query`, sorted by `rerank_score` (stable), dropped below
  `min_rerank_score` when it is set, then cut to `final_top_k`. `RetrievalResult.reranked`
  says whether reranking actually ran. It is false when reranking is disabled or no
  reranker is configured. It is also false when the reranker raises: the fused order is
  kept and `retrieval.rerank_failed` is logged, because reranking only refines the order.
* `CrossEncoderReranker` wraps `sentence_transformers.CrossEncoder` (`RERANKER_MODEL`). It
  loads lazily on first use (once per process, behind a lock) on `RERANKER_DEVICE`
  (`auto` = cuda, then mps, then cpu). It returns **sigmoid** scores in 0..1, so
  `RETRIEVAL_MIN_RERANK_SCORE` is an interpretable probability-like cut. Inference is
  serialized per process, because Hugging Face fast tokenizers are not thread-safe.

## Context construction

`build_context(candidates, max_tokens=CONTEXT_MAX_TOKENS)`, used by chat:

1. **Merge**: consecutive chunks (`chunk_index` n, n+1) of the same document become one
   passage when chunk n+1 repeats the end of chunk n. The chunker records how many leading
   characters repeat in `metadata.overlap_chars` (only within a section, so sections never
   merge), and the merge uses that count only when the text confirms it. The overlap is
   included once, so `text` is exactly what the chunker would have put in one larger chunk
   (tests/test_context.py checks this against the real chunker). `chunk_ids` lists every
   merged chunk in document order, and `candidate` is the best-ranked member (its metadata
   is used for citations). Text shared by chance, without a declared overlap, is never
   spliced.
2. **De-duplicate**: a passage is dropped when ≥ 90% of its word trigrams already appear in
   a kept, higher-ranked passage. That covers exact copies (the same file in two
   workspaces), near copies (one word changed, different case or spacing) and text
   already inside a merged passage.
3. **Budget**: passages are taken in rank order while the token total fits `max_tokens`.
   A passage that does not fit is skipped, and smaller lower-ranked ones are still tried.
   Merged token counts subtract each overlap's share of the chunk's `token_count`.
4. **Labels**: the passages are `S1..Sn` in final order. Text is never escaped or
   reformatted; the prompt layer fences it as untrusted data.

## Search API (`POST /api/v1/search`)

* `RetrievalOptions.from_settings(settings, mode, rerank, final_top_k=min(top_k, SEARCH_MAX_RESULTS))`.
  The per-method and fused limits are raised to at least `top_k`, so the requested number
  of results can be met.
* `score` is `rerank_score` when reranked, otherwise `rrf_score`. `explanation` carries
  each method's rank and score, `matched_terms`, `rrf_score`, `rerank_score` and
  `final_rank`.
* `highlights` are `(text, match)` segments that concatenate to the chunk content exactly.
  They come from `ts_headline(…, 'HighlightAll=true, StartSel=\x02, StopSel=\x03')`,
  split on the control-character delimiters (cleaned chunk text never contains them).
  Only keyword matches are highlighted; vector-only matches are one unhighlighted
  segment. Any chunk where `ts_headline` does not reproduce the text falls back to one
  unhighlighted segment. That happens with words longer than 2047 characters.
* Requesting `rerank: true` without a configured reranker is not an error (`reranked: false`).

## Observability

* `timings_ms` per stage: `embed` and `vector` (vector and hybrid modes), `keyword`
  (keyword and hybrid), `fuse`, `rerank` (when attempted) and `total`. The search API adds
  `highlight`, which is not included in `total`. Each stage is also observed in the
  `eka_retrieval_duration_seconds{stage=…}` histogram.
* One `retrieval.completed` log line per call with `mode`, `vector_hits`, `keyword_hits`,
  `candidates`, `returned`, `reranked` and `timings_ms`. It never contains query text or
  passages.

## Parameters

| Setting | Default | Effect |
|---|---|---|
| `RETRIEVAL_MODE` | `hybrid` | Default mode (`vector`, `keyword`, `hybrid`). |
| `RERANK_ENABLED` | `true` | Default for `options.rerank`. |
| `RERANKER_PROVIDER` | `cross_encoder` | `none` disables reranking entirely. |
| `RERANKER_MODEL` | `cross-encoder/ms-marco-MiniLM-L6-v2` | Any sentence-transformers cross-encoder. |
| `RERANKER_DEVICE` | `auto` | `auto`, `cpu`, `cuda`, `cuda:1`, `mps`, … |
| `RERANKER_BATCH_SIZE` / `RERANKER_MAX_LENGTH` | 32 / 512 | Inference batch size; query+passage token limit. |
| `RETRIEVAL_VECTOR_TOP_K` / `RETRIEVAL_KEYWORD_TOP_K` | 40 / 40 | Candidates per retriever. |
| `RETRIEVAL_RRF_K` | 60 | RRF damping constant: larger flattens the rank curve. |
| `RETRIEVAL_FUSED_TOP_K` | 30 | Candidates kept after fusion (= reranker input size). |
| `RETRIEVAL_FINAL_TOP_K` | 8 | Candidates returned to chat (and its context builder). |
| `RETRIEVAL_MIN_RERANK_SCORE` | unset | Drop reranked candidates below this sigmoid score. |
| `CONTEXT_MAX_TOKENS` | 6000 | Context budget for `build_context` in chat. |
| `HNSW_EF_SEARCH` | 100 | HNSW candidate list size (recall vs latency). |
| `FTS_CONFIG` | `english` | Text search configuration (must match the migration). |
| `MAX_QUERY_CHARS` | 2000 | `normalize_query` trim length. |
| `SEARCH_MAX_RESULTS` | 50 | Cap on `top_k` for the search API. |

## Tuning

* **Recall vs latency**: raise `HNSW_EF_SEARCH` (e.g. 200) when relevant chunks are
  missing from vector results. Raising `RETRIEVAL_FUSED_TOP_K` gives the reranker more to
  work with, but rerank cost grows linearly with it and usually dominates retrieval
  latency. Measured with MiniLM-L6 on 30 passages of ~300 words each: ~230 ms on Apple
  MPS and ~900 ms on an Apple-silicon CPU. A GPU, a smaller `RERANKER_MAX_LENGTH` or a
  smaller `RETRIEVAL_FUSED_TOP_K` cuts it.
* **Very selective access in very large tenants**: an iterative scan stops after
  pgvector's `hnsw.max_scan_tuples` (default 20000) tuples. A user who can read a tiny
  fraction of a huge tenant can then get fewer than `vector_top_k` vector hits. For narrow
  scopes the planner usually picks the exact plan instead. If that is not enough, raise
  `hnsw.max_scan_tuples` for the database role
  (`ALTER ROLE eka_app SET hnsw.max_scan_tuples = 100000`), or rely on keyword search in
  hybrid mode. Keep table statistics fresh (autovacuum/ANALYZE): the plan choice depends
  on them.
* **Calibrating `RETRIEVAL_MIN_RERANK_SCORE`**: start unset and inspect `rerank_score` in
  search explanations for known-good and known-bad queries. With MiniLM-L6, clearly
  relevant passages typically score above 0.9 and unrelated ones below 0.1. Keyword-stuffed
  passages can also score high, so a threshold filters noise but does not replace ranking.
* **RRF**: `RETRIEVAL_RRF_K=60` is the standard choice. Lower values let top-ranked hits of
  one retriever dominate the fusion.
* **Model loading**: the reranker loads on the first request that reranks. In containers
  without internet access set `HF_HUB_OFFLINE=1` with the model in the Hugging Face cache,
  otherwise start-up waits on Hub checks (seconds instead of a fraction of a second).
* **Changing `FTS_CONFIG`** requires a migration that regenerates `search_vector`. The query
  side must parse with the same configuration as the index.
