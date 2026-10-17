# Architecture

Enterprise Knowledge Assistant is a modular monolith: one FastAPI service, one Celery
worker image, PostgreSQL (pgvector + full-text search), Redis and S3-compatible object
storage. The Next.js frontend talks only to the versioned REST API.

```
 Browser ── Next.js (frontend/) ── /api/* proxy ──► FastAPI (backend/app)
                                                    │  auth, documents, search, chat (SSE)
                                                    │
               ┌────────────────────────────────────┼─────────────────────────┐
               ▼                                    ▼                         ▼
     PostgreSQL 17 + pgvector              Redis (broker, rate          S3 / local disk
     tenants, documents, chunks,           limits, embedding cache)     (original files)
     embeddings (HNSW), tsvector (GIN),           │
     conversations, audit, usage, RLS             ▼
               ▲                         Celery worker (backend/app/worker)
               └──────────────────────── parse → clean → chunk → embed → index
```

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/api` | HTTP layer only: routers (`v1/`), dependencies (`deps.py`) |
| `backend/app/services` | Business logic and authorization (`access.py` is the single access-control source) |
| `backend/app/rag` | Retrieval and answer pipeline, independent of HTTP (`types.py` holds its data model) |
| `backend/app/ingestion` | Parsers, cleaning, chunking, the ingestion job runner |
| `backend/app/providers` | Embedding, reranker and LLM provider interfaces + implementations |
| `backend/app/storage` | Object storage (local filesystem, S3) |
| `backend/app/worker` | Celery app, tasks, enqueue helpers |
| `backend/app/models`, `backend/alembic` | SQLAlchemy models; Alembic migrations own the DDL |
| `backend/eval` | RAG evaluation suite (datasets, runner, reports) |
| `frontend/` | Next.js + TypeScript + Tailwind UI |
| `infra/` | Docker/database bootstrap, AWS deployment templates |

## Tenancy and authorization

* **Organization** = tenant. Users can belong to several; a session has one *active*
  organization (`POST /auth/switch-organization`).
* **Organization roles**: `org_admin` (everything in the org, members, audit, usage),
  `knowledge_manager` (creates workspaces), `employee`.
* **Workspaces** group documents. Workspace roles: `owner` (members + all documents),
  `editor` (upload, manage own uploads), `viewer` (read).
* **Document visibility** is stored as grants in `document_permissions`:
  a `workspace` grant on the document's own workspace makes it visible to all workspace
  members; without it the document is *restricted* to users with explicit `user` grants,
  its uploader (while still in the workspace), workspace owners and org admins.
  `manage` grants allow editing a restricted document.
* `app/services/access.py::readable_documents_clause` is the one predicate used by every
  read path - document APIs, search, chat retrieval and citation rendering - so filters
  supplied by the client can only narrow results. Unreadable resources return **404**
  (indistinguishable from nonexistent); readable-but-not-manageable returns **403**.
* **Row-level security** is a second, independent layer. Every tenant table has
  `ENABLE` + `FORCE ROW LEVEL SECURITY` and a policy comparing `organization_id` with
  `app_current_org_id()`, which reads a *transaction-local* setting written at the start
  of every transaction (`app/db/session.py`). The API connects as `eka_app`
  (`NOSUPERUSER NOBYPASSRLS`); migrations run as the owner role `eka_owner`. Composite
  foreign keys `(x_id, organization_id)` make cross-tenant references impossible.

## Sessions, CSRF and request hygiene

* Opaque 256-bit session tokens in an HttpOnly `SameSite=Lax` cookie; only SHA-256 hashes
  are stored. Idle and absolute expiry; revocation on logout and password change.
* CSRF: the readable `eka_csrf` cookie holds `HMAC(SECRET_KEY, session token)`; unsafe
  methods must echo it in `X-CSRF-Token`. Cross-site `Origin` headers are rejected.
* Passwords: argon2id; unknown emails still pay for one hash verification.
* Pure-ASGI middleware (so SSE is never buffered): request IDs, access logs, metrics,
  body-size limits (before parsing), security headers.
* Errors always use `{"error": {"code", "message", "details", "request_id"}}`.

## Ingestion

`ingestion_jobs` is the source of truth; Redis/Celery only *trigger* work.

1. Upload: validate (extension, declared MIME and magic bytes agree; size; structure),
   hash (SHA-256), store under a server-generated key
   `orgs/{org}/documents/{doc}/v{version}/{uuid}.{ext}`, insert document + job, commit,
   then enqueue (`eka.ingestion.process`).
2. Worker claims the job with a lease (`UPDATE ... WHERE status='queued' OR lease expired
   ... RETURNING`), so redelivered messages are harmless. A partial unique index allows
   one active job per document.
3. Parse (page numbers, headings, tables, code blocks preserved) → clean → chunk
   (token-based with overlap, never splitting code blocks mid-way where avoidable) →
   embed in batches (Redis cache keyed by model + content hash) → replace the document's
   chunks in one transaction, guarded by the document version so a stale job can never
   overwrite a newer version.
4. Failures are recorded on the job and document (`processing_error`); retryable errors
   back off and retry up to `INGESTION_MAX_ATTEMPTS`. A beat task re-queues expired
   leases and jobs whose broker message was lost.

## Retrieval and answering

```
question ─► normalize ─► (follow-up?) rewrite with chat history ─► SearchScope
         ─► vector top-k (pgvector HNSW, cosine)  ┐
         ─► keyword top-k (tsvector + ts_rank_cd) ┴─► RRF fusion ─► cross-encoder rerank
         ─► context builder (dedupe, token budget, S1..Sn labels) ─► grounded LLM prompt
         ─► answer + status ─► citation validation ─► structured sources
```

* Both retrievers apply `readable_documents_clause` plus the caller's filters inside the
  SQL query, before ranking, so unauthorized text never reaches the reranker or LLM.
* Retrieved text is fenced and labelled as untrusted data in the prompt; instructions
  inside documents are ignored by policy and by construction (the model cannot call
  tools).
* Citations (`[S1]`) are validated against the passages actually supplied; unmatched
  markers are flagged and never shown as verified. Answers without support are marked
  `insufficient_evidence` (abstention) instead of guessed.
* `LLM_PROVIDER=extractive` answers offline by selecting supporting sentences, so the
  whole product runs without an API key.

## Embedding dimensions

The vector column is created as `vector(EMBEDDING_DIMENSIONS)` by the migrations, and the
full-text `tsvector` uses `FTS_CONFIG`; both are read from settings when the migration
runs. The API's `/ready` check fails if the database dimension differs from the
configured one. To change the embedding model:

1. Set `EMBEDDING_MODEL` and `EMBEDDING_DIMENSIONS` (e.g. `BAAI/bge-base-en-v1.5` = 768).
2. Add a migration that alters `document_chunks.embedding` to the new dimension and
   rebuilds `ix_document_chunks_embedding_hnsw` (existing vectors must be dropped).
3. Re-index all documents (`POST /api/v1/documents/{id}/reindex`); retrieval only
   compares query vectors with chunks embedded by the same `embedding_model`.

## Observability

Structured JSON logs (request/job IDs, no document text or secrets), Prometheus metrics
at `/metrics` (HTTP, retrieval stages, generation latency, tokens, estimated cost,
ingestion outcomes), `GET /api/v1/health` (liveness) and `GET /api/v1/ready`
(database + migrations + vector dimension, Redis, storage).
