# Ingestion

How an uploaded file becomes searchable chunks, and how the worker keeps that reliable.
Code lives in `backend/app/ingestion/` (parsers, cleaning, chunking, runner, reaper,
retention), `backend/app/providers/embeddings/` and `backend/app/worker/`.

```
eka.ingestion.process(job_id, org_id)
  claim job (lease) ─► fetch original ─► parse ─► clean ─► chunk ─► embed (cache) ─► swap chunks
                       storage           blocks    blocks   chunks   vectors          one transaction
```

The `ingestion_jobs` row is the source of truth. A Celery message only says "look at job
X": duplicate, late or lost messages are harmless, because the claim is atomic and the
reaper re-sends messages that were lost.

## Entry points

| Function / task | Contract |
|---|---|
| `runner.process_job(job_id, organization_id, *, embedder=None, storage=None) -> JobStatus` | Runs one attempt and returns the job's status afterwards. A job that is not claimable (already done, not due, leased by another worker, another organization's) is left alone and its current status is returned (`cancelled` if it does not exist). |
| `eka.ingestion.process(job_id: str, organization_id: str)` | Celery task around `process_job`, queue `ingestion`. Enqueue with `app.worker.queue.enqueue_ingestion` after the job row is committed. |
| `eka.maintenance.requeue_stale_jobs()` | The reaper, run by beat every minute (queue `maintenance`). |
| `eka.maintenance.apply_retention()` | The retention policy, run by beat every 6 hours (queue `maintenance`). |
| `cache.invalidate(organization_id, content_hashes)` | Drops the organization's cached vectors of chunks (the documents service calls it after deleting a document). |

## Formats

| Format | What is extracted | Pages | Permanent errors |
|---|---|---|---|
| PDF (pypdf) | Text per page. Bookmarks (the outline) give section titles: a section starts at its heading line when that line is found on the page, else at the top of the page; pages without a bookmark continue the current section. Running headers/footers are removed and wrapped lines rejoined (see Cleaning). | 1-based | `encrypted_pdf` (a user password is needed; owner-password-only files open, AES-encrypted ones only with the `cryptography` package installed), `too_many_pages` (`MAX_PDF_PAGES`), `file_too_large` (its streams inflate to more than `MAX_PDF_UNCOMPRESSED_MB`, default 200: a decompression-bomb guard, see below), `no_extractable_text` (scanned images: OCR is out of scope), `corrupt_file` |
| DOCX (python-docx) | Body order. `Title` and `Heading N` styles build the heading path; tables become Markdown pipe tables (`\|` escaped, nested tables flattened into their cell); list paragraphs (Word numbering level, or the digit in `List Bullet 2`) become `- ` items indented two spaces per level; code-styled or all-monospace paragraphs become fenced code blocks. | none | `file_too_large` (unpacks to more than `MAX_DOCX_UNCOMPRESSED_MB`, checked before parsing), `corrupt_file` |
| Markdown | YAML/TOML front matter skipped; ATX and Setext headings; ```` ``` ```` and `~~~` fences kept verbatim (an unclosed fence runs to the end); indented code after a blank line; pipe tables; lists with their continuation lines; hard line breaks. Links and emphasis are stripped from heading titles only. | none | `unreadable_text` |
| Plain text | Paragraphs separated by blank lines; a paragraph indented throughout is kept as preformatted text. UTF-8 (with or without BOM), UTF-16 with BOM, else Windows-1252. | none | `unreadable_text` |

Anything else fails with `unsupported_file_type`; a document without indexable text
fails with `empty_document`. The MIME type stored at upload decides the format, the file
extension is the fallback.

**Decompression bombs.** A DOCX is bounded at upload (the declared sizes of its parts). A
PDF is bounded while it is parsed: pypdf inflates every stream through one function and
keeps the result while the document is open, and its own limits (75 MB per stream, two
million parser tokens per content stream, 5000 form XObjects per page) are per stream or
per page, so a few kilobytes of deflated data per page could add up to gigabytes in the
worker. `parsers/pdf.py` charges every inflated byte (page contents, forms, fonts, object
streams) to a per-document budget of `MAX_PDF_UNCOMPRESSED_MB` and fails the job with
`file_too_large` once it is spent. Parsing time is bounded by the Celery time limit (see
Running the worker), not by this budget.

## Cleaning

* Prose (paragraphs, list items, heading titles): control characters, zero-width
  characters, BOMs and soft hyphens removed; Unicode NFKC (ligatures, full-width forms,
  non-breaking spaces); whitespace collapsed within lines, at most one blank line.
  List items keep their indentation (nesting).
* Code blocks and tables: only control characters and trailing whitespace are removed.
  They are never normalized, reflowed or re-indented.
* PDF only:
  * A line among the first or last three non-blank lines of a page whose text (digits
    ignored, so "Page 3 of 9" equals "Page 4 of 9") appears there on at least
    `max(3, pages / 2 + 1)` pages is a running header or footer and is dropped. Documents
    with fewer than three pages are left alone.
  * Wrapped lines are rejoined: a line at least 75% of the document's typical line width
    (90th percentile) continues on the next line, unless the next line starts a list item.
    A trailing hyphen after a letter is removed when the next line starts with a
    lowercase letter (`inter-` + `national`) and kept otherwise (`Hewlett-` + `Packard`).
    Heading lines found through the outline are never joined to their neighbours.

## Chunking

| Setting | Default | Meaning |
|---|---|---|
| `CHUNK_TARGET_TOKENS` | 350 | Size at which a chunk of prose is closed. |
| `CHUNK_MAX_TOKENS` | 500 | Hard limit; only code blocks and tables use the room above the target. |
| `CHUNK_OVERLAP_TOKENS` | 50 | Budget for whole sentences repeated from the end of the previous chunk. |

* A section is a run of blocks with the same heading path. Chunks never span two
  sections, so they never span top-level sections, and overlap never crosses a section.
* Within a section blocks are packed in order. Prose is cut only at sentence ends
  (`.`, `!`, `?`, optionally followed by a closing quote or bracket, then whitespace and
  something other than a lowercase letter, so "e.g. this" stays together; CJK `。！？`)
  or line ends. A "sentence" longer than the target is split at words, a word longer
  than the target at characters.
* A block that does not fit in the current chunk but fits in an empty one moves to the
  next chunk whole instead of being split.
* Code blocks and tables are atomic up to `CHUNK_MAX_TOKENS`, so a short one stays with
  the sentence that introduces it. Larger ones are split on line boundaries: code pieces
  are re-fenced with the original fence and info string, table pieces repeat the header
  and delimiter rows. A single line too long for a piece is broken at words, and its words
  stay on one line within each piece. Overlap never repeats code or tables.
* Each chunk's `metadata` is `{"kinds": [...block kinds...]}` plus `"overlap_chars": n`
  when its first `n` characters repeat the previous chunk.
* Text addressed to AI assistants is flagged block by block before packing
  (`app/ingestion/instructions.py`; see
  [ARCHITECTURE.md](ARCHITECTURE.md#text-addressed-to-ai-assistants)). The flags travel
  with their text into `metadata["flags"]` as `{"start", "end", "rule", "page"?}`, in
  code points of `content`. Overlap repeats them, and a flag split between two chunks is
  in both. An oversized code block or table is checked piece by piece after splitting.
  A section heading that matches a rule sets `metadata["section_flag"]` to the rule on
  every chunk of its section.

**Token estimate.** `app/ingestion/tokens.py` estimates WordPiece/BPE tokens without a
tokenizer: punctuation and CJK characters count one each, letter runs of up to six
characters count one, longer ones a token per four letters, digit runs a token per two
digits, mixed letter/digit runs a token per 1.2 characters. Against the
BAAI/bge-small-en-v1.5 tokenizer (`tests/test_embeddings_model.py`) it reads 0-45% high
on everyday English, within a few percent on source code, about 10% high on tables and
identifiers, up to 10% low on jargon-dense English and about 30% low on long German
compounds. The default prose target leaves room for that in a 512-token window, and the
slow test checks that full-size prose, code and table chunks plus their header fit.
Lower the chunk settings for corpora dominated by long compound words.

**Text sent to the embedding model.** `embedding_text(title, chunk)` prepends a header,
`"{document title} > {heading path}"` (a first heading equal to the title is not
repeated; at most 300 characters), and a blank line. Only the passage itself is stored
in `content`; `content_hash` is the SHA-256 of the full embedded text.

## Embeddings

| `EMBEDDING_PROVIDER` | Notes |
|---|---|
| `sentence_transformers` | Local model (`uv sync --extra local-models`). Loaded on first use, once per process (thread-safe); inference is serialized per process. `EMBEDDING_DEVICE=auto` picks CUDA, then Apple MPS, then CPU. A model whose dimension differs from `EMBEDDING_DIMENSIONS` is a configuration error. |
| `openai_compatible` | `POST {EMBEDDING_API_BASE_URL}/embeddings` with httpx (`EMBEDDING_TIMEOUT_SECONDS`), bearer `EMBEDDING_API_KEY` (never logged). 429, 5xx and connection errors are retried up to `EMBEDDING_MAX_RETRIES` times with exponential backoff (0.5 s doubling, capped at 20 s, plus jitter) or the server's `Retry-After` (capped at 60 s); other 4xx responses, a wrong vector count or dimension and malformed bodies are not retried. Vectors are re-normalized. |
| `hashing` | Deterministic feature hashing for tests and offline development; rejected in production. |

Both real providers prepend `EMBEDDING_QUERY_PREFIX` to queries and
`EMBEDDING_DOCUMENT_PREFIX` to passages (BGE models expect the query instruction), and
return L2-normalized vectors. Documents are embedded in batches of
`EMBEDDING_BATCH_SIZE`.

**Cache.** Redis key
`emb:v2:{organization_id}:{model}:{sha256(EMBEDDING_DOCUMENT_PREFIX)[:12]}:{content_hash}`,
value the vector as little-endian float32 (what pgvector stores), TTL
`EMBEDDING_CACHE_TTL_SECONDS` (default 7 days; 0 disables the cache). Lookups use MGET,
writes a pipeline of SETs, each embedded batch is cached immediately (a retry after a
failure in a later batch does not pay for the earlier ones). Unchanged sections of a
re-indexed or re-uploaded document are served from the cache. An unavailable Redis never
fails ingestion: lookups count as misses (`result="error"` in the metric) and writes are
skipped. Entries are removed when a document is deleted (documents service), when a
re-index replaces chunks (hashes that disappeared), and when a document is deleted while
its job runs.

Entries are private to one organization, so identical text uploaded by two tenants is
embedded twice. A shared namespace would let a tenant learn from ingestion speed (job
`started_at`/`completed_at` are visible) that another tenant indexed byte-identical text,
and one tenant's deletion would drop another tenant's entries. Within one organization
entries are shared across workspaces: an uploader who already has the exact text could
still confirm from ingestion speed that it is indexed in a workspace they cannot read, as
`409 duplicate_document` confirms an identical file within a workspace. Add the workspace
to the key if that matters (identical text in two workspaces is then embedded twice).
Keys of the previous format (`emb:v1:{model}:...`, no organization) are no longer read
and expire with their TTL; `redis-cli --scan --pattern 'emb:v1:*'` lists any that remain.

## Indexing

Indexing replaces the document's chunks in one transaction:

1. Lock the document row, then the job row (`FOR UPDATE`, the order every writer uses).
2. Check the fencing token (the job is still `running` with the claimed `attempts`) and
   the version (`documents.version` still equals `ingestion_jobs.document_version`).
3. Delete the old chunks, insert the new ones (bulk), set the document `indexed` with
   `chunk_count`, `page_count`, `embedding_model`, `indexed_at` and `flagged_chunks`, and
   finish the job with its metrics.
   * Indexing resets `flagged_text_included` to false, so a manager's earlier decision to
     include flagged text does not carry over to new text.
   * When anything is flagged, the run records `document.instructions_flagged` in the
     audit log, with the version, the count and the pages.

Until that commit the previous chunks stay searchable, including when a re-index or a
new version fails (the document is then `failed` with `processing_error`, but its old
chunks remain). Retrieval must therefore not filter on `processing_status`.

| Chunk column | Value |
|---|---|
| `chunk_index` | 0..n-1 in document order |
| `content` | the exact passage text (what users and the LLM see) |
| `content_hash` | SHA-256 hex of the exact text given to the embedding model (header + passage) |
| `token_count` | estimate of `content` (see Token estimate) |
| `embedding`, `embedding_model` | `EMBEDDING_DIMENSIONS` floats, L2-normalized; the embedder's `model_name` |
| `page_number`, `page_end` | first and last 1-based page of the passage; null for formats without pages |
| `section_title`, `heading_path` | innermost heading; full path joined with `" > "` |
| `metadata` | `{"kinds": [...], "overlap_chars": n, "flags": [...], "section_flag": rule}` (see Chunking) |

## Job state machine

```
                 claim: attempts + 1, lease
   ┌──────────► queued ─────────────────────► running ──────► succeeded
   │              ▲                              │
   │              │ retryable error,             ├──────────► failed     permanent error, or
   │              │ attempts left (backoff)      │                       no attempts left
   │              └──────────────────────────────┤
   │                                             └──────────► cancelled  document version
   │                                                                     changed or deleted
   └── reaper: lease expired with attempts left, or message lost
```

* **Claim.** One statement takes a job that is `queued` and due (`run_after <= now()`),
  or `running` with an expired lease, and has attempts left:
  `UPDATE ingestion_jobs SET status='running', attempts=attempts+1,
  locked_until=now()+INGESTION_LEASE_SECONDS, started_at=coalesce(started_at, now())
  WHERE id=:job AND organization_id=:org AND attempts < max_attempts AND (...) RETURNING ...`.
  The new `attempts` value is the attempt's fencing token: every later write of the
  attempt checks it, so a worker that lost its lease can never overwrite the work of the
  attempt that replaced it.
* **Version guard.** A job records the document version it was created for. If the
  document has a newer version (re-upload), the job is cancelled with
  `error_code="superseded"`; the newer version's job owns the document.
* **Slow work holds no locks.** Fetching, parsing, chunking and embedding run outside any
  transaction; the document is marked `processing` first.
* **Retries.** Retryable failures put the job back to `queued` with
  `run_after = now() + INGESTION_RETRY_BASE_SECONDS * 2^(attempts-1)` plus up to 10%
  jitter, set the document back to `queued`, and re-enqueue the message with that
  countdown (+1 s for clock skew). Once `INGESTION_MAX_ATTEMPTS` attempts are used up the
  job fails and the message gets "Gave up after N attempts." appended.

| `error_code` | Retried | Cause |
|---|---|---|
| `corrupt_file`, `encrypted_pdf`, `too_many_pages`, `no_extractable_text`, `file_too_large`, `unreadable_text`, `unsupported_file_type`, `empty_document` | no | the document itself (see Formats) |
| `timeout` | no | Celery soft time limit (prefork pool only) |
| `embedding_failed` | no | provider configuration: wrong dimension, rejected request, library missing |
| `embedding_unavailable` | yes | provider unreachable or overloaded after its own retries |
| `storage_unavailable` | yes | the stored original could not be read |
| `service_unavailable` | yes | PostgreSQL or Redis operational error |
| `internal_error` | yes | unexpected exception (logged with its traceback) |
| `superseded` | - | cancelled: a newer document version exists |
| `worker_lost` | reaper | the lease expired: re-queued, or failed without attempts left |

`error_message` and `documents.processing_error` are written for end users: fixed,
actionable sentences without internals or document text.

## Reaper

`requeue_stale_jobs` runs every minute, once per organization in that organization's
tenant context (`ingestion_jobs` is under row-level security). Rows a worker is
committing are skipped (`SKIP LOCKED`) and looked at again next time.

1. `running` jobs whose lease expired (the worker crashed or was killed): back to
   `queued` with `error_code="worker_lost"` and a new message, or `failed` when no
   attempts are left. The document follows only if the job is for its current version.
2. `queued` jobs due for longer than `INGESTION_STALE_QUEUED_SECONDS` (the message was
   lost, e.g. Redis restarted or the enqueue after commit failed): sent again, or failed
   when no attempts are left.
3. Documents `queued` or `processing` for longer than that without an active job:
   `failed` with "Processing was interrupted. Reindex the document to try again."

## Retention

`apply_retention` runs every 6 hours (beat crontab, minute 15, UTC). Tenant tables are cleaned per organization in its
tenant context, with explicit organization filters as well:

| Data | Deleted when |
|---|---|
| Conversations (messages cascade) | last updated more than `CONVERSATION_RETENTION_DAYS` (365) ago and no message is newer than that |
| Audit events | older than `AUDIT_RETENTION_DAYS` (730) |
| Usage records | older than `USAGE_RETENTION_DAYS` (400) |
| Finished ingestion jobs | finished more than 30 days ago, except each document's latest job |
| User sessions (global) | expired, revoked or idle-expired more than 7 days ago |
| Invitations (global) | expired more than 30 days ago |

No audit event is written without an organization (row-level security would hide it
from the application role, so retention could never delete it): failed logins are
recorded in each organization of the account, and unknown emails are only logged.

## Running the worker

```
cd backend
uv run celery -A app.worker.celery_app worker -Q ingestion,maintenance --concurrency 1 -l INFO
uv run celery -A app.worker.celery_app beat -l INFO        # exactly one per deployment
```

* **One job at a time per worker process.** Scale with more worker containers
  (`--scale worker=N`), not with `--concurrency`: every pool process loads its own copy of
  the embedding model, and only one of them can serve the metrics port.
* **Pools and time limits.** docker-compose and the ECS task definition run the prefork
  pool with one child (`--concurrency 1`). Celery then enforces a hard time limit of
  `INGESTION_LEASE_SECONDS` (default 900 s) and a soft limit 60 s earlier; the soft limit
  fails the job permanently with `timeout`, the hard limit replaces the child. The solo
  pool (`--pool solo`, used by `make worker`) runs tasks in the main process and cannot
  enforce time limits: an attempt that outlives its lease is re-claimed by the reaper and
  its late result is discarded by the fencing check. Size `INGESTION_LEASE_SECONDS` for
  the largest documents and the slowest embedding model.
* **macOS.** Use `--pool solo`. billiard 4.3 starts prefork children with `spawn` on
  macOS, and Celery's prefork pool cannot run tasks in spawned children (every task fails
  with `ValueError: not enough values to unpack` in `fast_trace_task`). Linux uses `fork`.
* **Delivery.** `acks_late` + `reject_on_worker_lost` redeliver the message of a worker
  that died mid-task; the broker visibility timeout is twice the lease; prefetch is 1.
* **Metrics.** The process that executes tasks (the prefork child, or the main process
  with `--pool solo`) serves Prometheus metrics on `WORKER_METRICS_PORT` (default 9101)
  when `METRICS_ENABLED`; a busy port logs `worker.metrics_port_in_use` and the worker
  carries on. Check with `curl -s localhost:9101/metrics | grep eka_ingestion`.
* **Logging.** Worker and beat use the API's structured logging, configured by
  `LOG_LEVEL` and `LOG_FORMAT` (Celery's `-l` option does not override them).

## Observability

| Metric | Labels |
|---|---|
| `eka_ingestion_jobs_total` | `status`: the job's status after the attempt: `succeeded`, `failed`, `cancelled`, `retried` (queued again), or `running` (the attempt lost its lease) |
| `eka_ingestion_duration_seconds` | duration of each claimed attempt |
| `eka_embedding_cache_total` | `result`: `hit`, `miss`, `error` (Redis unavailable) |

Job `metrics` (JSON on the `ingestion_jobs` row, for operators):
`pages`, `blocks`, `chunks`, `tokens`, `cache_hits`, `cache_misses`, for PDFs
`pages_failed` and `pages_without_text`, and `timings_ms` with `fetch`, `parse`,
`clean`, `chunk`, `embed`, `index`, `total`.

Log events (JSON, with `job_id` and `org_id` from the job context; never document text,
titles or credentials): `ingestion.succeeded`, `ingestion.attempt_failed`,
`ingestion.unexpected_error` (with traceback), `ingestion.not_claimed`,
`ingestion.superseded`, `ingestion.lease_lost`, `ingestion.reaped`,
`ingestion.pdf_outline_unreadable`, `ingestion.enqueue_failed`, `retention.applied`,
`embedding_cache.unavailable`, `embeddings.request_retry`, `embeddings.model_loaded`,
`worker.metrics_port_in_use`.

## Tests

```
cd backend
EKA_TEST_DB=eka_test_c uv run pytest tests/test_parsers.py tests/test_chunking.py \
  tests/test_embeddings.py tests/test_ingestion.py tests/test_worker.py tests/test_retention.py \
  tests/test_flagged_text.py
HF_HUB_OFFLINE=1 uv run pytest tests/test_embeddings_model.py -m slow   # real model, local cache
```

Fixtures are generated in the tests (reportlab PDFs with outlines and running
headers/footers, python-docx documents, Markdown and text). The integration tests use
PostgreSQL with row-level security, Redis and local storage; HTTP providers run against
`httpx.MockTransport`.
