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
* **Organization roles**: `org_admin` (members, invitations, deactivation, every
  workspace's settings and members, audit log, usage), `knowledge_manager` (creates
  workspaces), `employee`. Org admins are *content-blind*: no document is readable,
  manageable or uploadable because of the role. An admin who needs a workspace's documents
  joins it, which the audit log records (`workspace.member_added`) and its members see.
* **Workspaces** group documents. Workspace roles: `owner` (members + all documents),
  `editor` (upload, manage own uploads), `viewer` (read).
* **Document visibility** is stored as grants in `document_permissions`:
  a `workspace` grant on the document's own workspace makes it visible to all workspace
  members; without it the document is *restricted* to users with explicit `user` grants,
  its uploader (while still in the workspace) and workspace owners.
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

## Permission Proof

A leak test of the rule above (`app/proof/`, `app/services/permission_proof.py`), run
from Administration or weekly by Celery beat (`PERMISSION_PROOF_SCHEDULE`). It answers the
question a security reviewer asks: would the assistant ever send a person text from a
document they cannot read?

1. **Snapshot.** The run reads memberships, grants, documents and chunks in one
   `REPEATABLE READ` transaction, so a permission change made while it runs cannot raise a
   false alarm.
2. **A second rule.** `app/proof/oracle.py` implements the read rule again, sharing no code
   with `access.py`, so a bug in one is not repeated in the other. A test compares the two
   on randomly generated permission graphs.
3. **Access groups.** Active members with the same organization role, the same workspace
   roles and the same readable documents form one group; its first member by email is
   asked for the whole group. The application's readable set must equal the second rule's
   for every member, not only the one asked: the rule also reads per-person records
   (uploads, personal grants).
4. **Questions.** For every indexed document a group cannot read, up to three questions
   taken from the document itself: its title, and up to 300 characters from the start of
   its first and of its middle chunk. Copied text is the strongest way to pull a document
   into retrieval. At most `PERMISSION_PROOF_MAX_PROBES` questions per run; above that, a
   deterministic sample spread over groups and documents, and the report says so.
5. **Checks.** Each question goes through the chat's own `retrieve` (reranking off: it
   only reorders the same candidates), `build_context` and prompt assembly. No model is
   called. Every candidate must come from a document the group can read, and the exact
   model request, built without the question and without the fixed system prompt, must
   hold no 50-character run of normalized text that occurs only in documents the group
   cannot read (an index of windows taken every 25 characters makes this fast). A
   document's title and section names count as its text, since the request names them.
6. **Verdict.** Passed, failed (with findings per group and document), or nothing to test
   when every group can read every indexed document.

**Simulation.** An admin picks a person and a workspace they are not in. The run asks
only as that person, with an in-memory viewer membership of the workspace, while the
second rule keeps the real records: the run must report findings ("caught"). Nothing is
written to memberships or grants, and the admin page's "latest result" skips
simulations.

**What it shows, and what it does not.** For the questions it asked, the application's
retrieval and model requests stayed within the documented rule. It does not show that the
permissions themselves are right, or anything about paths other than retrieval.

**Operation.** Org admins only (`/api/v1/admin/permission-proof/runs`). One run at a time
per organization; a run created more than `INGESTION_LEASE_SECONDS` + 60 s ago that is
still queued or running is shown as interrupted and no longer blocks a new one. Runs execute on the `maintenance`
queue, so a long run delays ingestion on a single worker. Reports hold ids and counts,
never text; a finding shows a document's title only to a viewer who may read the
document. Both events are in the audit log (`permission_proof.run_requested`,
`permission_proof.run_completed`).

## Sessions, CSRF and request hygiene

* Opaque 256-bit session tokens in an HttpOnly `SameSite=Lax` cookie; only SHA-256 hashes
  are stored. Idle and absolute expiry; revocation on logout and password change.
* CSRF: the readable `eka_csrf` cookie holds `HMAC(SECRET_KEY, session token)`; unsafe
  methods must echo it in `X-CSRF-Token`. Cross-site `Origin` headers are rejected.
* Audit log (`audit_events`; the app role cannot update rows): sign-ins, membership and
  role changes, deactivation, workspace and document changes, downloads, and what the AI
  handed out: `chat.answered` (the documents an answer retrieved, cited and inherited from
  history) and `search.performed` (the documents returned). Identifiers and counts only;
  never a question, an answer or document text.
  A document's title appears in the log and the dashboard activity only for someone who can
  read the document or did the action, so content-blind admins see ids, not titles.
* Passwords: argon2id; unknown emails still pay for one hash verification. The audit rows
  of a failed sign-in (one per organization of an existing account) are written after the
  401 has been sent, so the response time does not tell whether an account exists.
* Sign-up is by invitation only unless `ALLOW_PUBLIC_REGISTRATION=true` (off by default;
  `scripts/create_organization.py` creates the first organization on the server and prints
  a one-time sign-up link for its admin, who chooses their own password). Public
  registration answers `409 email_taken` for an email that already has an account, so it
  reveals accounts, at `RATE_LIMIT_REGISTER_PER_HOUR` attempts per IP.
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
   embed in batches (Redis cache per organization, keyed by model + content hash) →
   replace the document's chunks in one transaction, guarded by the document version so a
   stale job can never overwrite a newer version.
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

`/metrics` requires `Authorization: Bearer <METRICS_TOKEN>` only when `METRICS_TOKEN` is
set: set it wherever the API port can be reached by anything but the metrics collector.
The worker's metrics port (`WORKER_METRICS_PORT`) has no authentication at all and must
only be reachable from the collector (see the security groups in DEPLOYMENT_AWS.md).
