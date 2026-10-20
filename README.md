# Enterprise Knowledge Assistant

A permission-aware retrieval-augmented generation (RAG) platform for internal documents.
Organizations upload PDF, DOCX, Markdown and text files into workspaces. Employees search
and chat over the documents they are allowed to read, and every answer cites the passages
it is grounded in or says that the documents do not contain the answer.

* **Multi-tenant by construction.** Organizations, workspaces and per-document grants are
  enforced by one access predicate in every read path, and PostgreSQL row-level security
  is a second, independent layer.
* **Hybrid retrieval.** pgvector HNSW and PostgreSQL full-text search are fused with
  reciprocal rank fusion (RRF) and reranked by a cross-encoder. Permission filters are
  applied inside SQL, so unauthorized text never reaches the reranker or the model.
* **Grounded answers.** Citations (`[S1]`), quotes verified against the supplied
  passages, explicit abstention (`insufficient_evidence`), resistance to prompt injection
  in retrieved text, and server-sent-event streaming.
* **Pluggable providers.** The Claude API, Amazon Bedrock and OpenAI-compatible servers
  (Ollama, vLLM). An offline extractive mode runs the whole product without any API key.
* **Operations.** Background ingestion with leased jobs, retries and a reaper; audit log;
  token usage and estimated cost; Prometheus metrics; structured logs that never contain
  document text; Docker images, CI and AWS deployment templates.
* **Evaluation suite.** Separate retrieval and generation metrics over a synthetic corpus,
  comparing vector-only, hybrid and hybrid + rerank retrieval
  ([docs/EVALUATION.md](docs/EVALUATION.md)).

## Contents

- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [API usage](#api-usage)
- [Testing](#testing)
- [Evaluation](#evaluation)
- [Security model](#security-model)
- [Observability and cost](#observability-and-cost)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [What has and has not been tested](#what-has-and-has-not-been-tested)

## Architecture

The system is a modular monolith: one FastAPI service, one Celery worker image, PostgreSQL
17 with pgvector and full-text search, Redis, and S3-compatible object storage (or the
local disk). The Next.js frontend talks only to the versioned REST API, through a
same-origin `/api` proxy.

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

A question goes through these steps:

```
question ─► normalize ─► (follow-up?) rewrite with chat history ─► permission scope
         ─► vector top-k (HNSW, cosine) ┐
         ─► keyword top-k (ts_rank_cd)  ┴─► RRF fusion ─► cross-encoder rerank
         ─► context builder (dedupe, token budget, S1..Sn labels) ─► grounded prompt
         ─► answer + status ─► citation and quote validation ─► structured sources
```

Design documents:

| Topic | Document |
|---|---|
| Tenancy, authorization, sessions, request hygiene | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Parsing, chunking, embeddings, the job state machine, retention | [docs/INGESTION.md](docs/INGESTION.md) |
| Vector and keyword search, fusion, reranking, context building | [docs/RETRIEVAL.md](docs/RETRIEVAL.md) |
| Prompt contract, statuses, citations, abstention, streaming, usage | [docs/GENERATION.md](docs/GENERATION.md) |
| Claude API, Bedrock and OpenAI-compatible providers | [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md) |
| Evaluation method, dataset and results | [docs/EVALUATION.md](docs/EVALUATION.md) |
| Production deployment on AWS (ECS Fargate, RDS, ElastiCache, S3) | [docs/DEPLOYMENT_AWS.md](docs/DEPLOYMENT_AWS.md) |
| Web UI | [frontend/README.md](frontend/README.md) |

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/api` | HTTP layer: versioned routers (`v1/`) and dependencies |
| `backend/app/services` | Business logic and authorization (`access.py` is the single access-control predicate) |
| `backend/app/rag` | Retrieval, context building, prompting, citation validation, answering |
| `backend/app/ingestion` | Parsers, cleaning, chunking, the job runner, reaper and retention |
| `backend/app/providers` | Embedding, reranker and LLM interfaces with their implementations |
| `backend/app/storage` | Object storage (local filesystem, S3) |
| `backend/app/worker` | Celery application and tasks |
| `backend/alembic` | Migrations; they own all DDL, including row-level security policies |
| `backend/eval` | RAG evaluation suite: synthetic corpus, datasets, runner, reports |
| `backend/tests` | Unit and integration tests (pytest) |
| `frontend/` | Next.js, React, TypeScript and Tailwind CSS UI, with Vitest tests |
| `e2e/` | Playwright end-to-end tests against the real stack |
| `infra/` | Database bootstrap for Docker, AWS deployment templates |
| `scripts/` | Developer utilities (port check) |

## Getting started

### Prerequisites

* Docker with Compose v2.
* To run from source: [uv](https://docs.astral.sh/uv/), Python 3.12 or newer (the images
  and CI use 3.14; uv is configured never to download an interpreter, so one must be
  installed), and Node.js 22.22+ or 24 LTS with npm.
* Optional: [Ollama](https://ollama.com/) for a local LLM, or an Anthropic API key for
  Claude. Without either, answers come from the offline extractive generator.

No AWS account or credentials are needed locally: files are stored on the local disk by
default.

### 1. Configure

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste the output as SECRET_KEY in .env
make check-ports                                             # bind-tests 5436, 6381, 8081, 3000, 9101
```

If a port is taken, change the matching `EKA_*_PORT` in `.env`. Also update the URLs that
contain it: `DATABASE_URL`, `MIGRATION_DATABASE_URL` and `REDIS_URL` for PostgreSQL and
Redis, or `PUBLIC_BASE_URL` and `ALLOWED_ORIGINS` for the frontend.

### 2a. Run the whole stack in containers

```bash
make up-app        # builds the images, runs migrations, starts api, worker, beat and frontend
```

Open <http://localhost:3000> and register: the first user of a new organization becomes
its administrator and gets a default workspace. Sign-up is open because `.env.example`
sets `ALLOW_PUBLIC_REGISTRATION=true`; unset, it is off and only invitations add users.
Then the server creates the first organization and prints a one-time sign-up link for its
administrator, who opens it and chooses their own password:

```bash
docker compose run --rm api python scripts/create_organization.py \
  --organization "Acme Ltd" --email admin@acme.example
# or, without containers: cd backend && uv run python scripts/create_organization.py ...
```

The API listens on <http://localhost:8081>, with interactive documentation at <http://localhost:8081/api/docs>.

On the first start the API and the worker download the embedding model
(`BAAI/bge-small-en-v1.5`) and the reranker (`cross-encoder/ms-marco-MiniLM-L6-v2`) into
the `hf-cache` volume. Later starts load them from there. `make down-app` stops
everything and keeps the data volumes.

### 2b. Run from source (development)

```bash
make infra-up                                   # PostgreSQL 17 + pgvector and Redis in Docker
(cd backend && uv sync --extra local-models)    # API dependencies plus the local embedding/reranker models
make migrate                                    # schema, roles and row-level security
make api                                        # http://localhost:8081 (auto-reload)
make worker                                     # second terminal: ingestion and maintenance queues
make beat                                       # third terminal: reaper and retention schedule
(cd frontend && npm ci && npm run dev)          # fourth terminal: http://localhost:3000
```

The `local-models` extra installs PyTorch and sentence-transformers, which the default
providers (`EMBEDDING_PROVIDER=sentence_transformers`, `RERANKER_PROVIDER=cross_encoder`)
need. To skip it, point `EMBEDDING_PROVIDER=openai_compatible` at an embedding server with
the same dimension, and set `RERANKER_PROVIDER=none`.

### 3. Load demo data (optional, from source)

```bash
SEED_PASSWORD='choose-a-local-passphrase' make seed
```

The seed belongs to the from-source setup (2b). It writes through the application's services to the
database and storage configured in `.env`. The container images contain only the application, so with the
containerized stack (2a) the uploaded files would land outside the containers' storage volume.

The seed creates the fictional company "Northwind Aerial Robotics (fictional)":
* five users: `admin@`, `manager@`, `employee@`, `finance@` and
  `contractor@demo.example.com`;
* three workspaces;
* the 18 synthetic documents of the evaluation corpus. One of them is restricted:
  `finance@` can read it and `employee@` cannot.

New users get `SEED_PASSWORD`; without it, each gets a random password that is printed
once. The worker indexes the uploads (`make worker`), or pass `--inline` to the script
(`cd backend && uv run python scripts/seed_demo.py --inline`). Re-running the seed leaves
existing data as it is, and it refuses to run with `APP_ENV=production`.

### Choosing an LLM

| Provider | Settings | Notes |
|---|---|---|
| Extractive (default) | `LLM_PROVIDER=extractive` | Offline, no key. Answers by selecting supporting sentences; for development and tests |
| Claude API | `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY`, `LLM_MODEL` (default `claude-opus-5-5`) | `LLM_QUERY_REWRITE_MODEL` can name a cheaper model for follow-up rewriting |
| Amazon Bedrock | `LLM_PROVIDER=anthropic`, `ANTHROPIC_PLATFORM=bedrock`, `AWS_REGION`, `LLM_MODEL` | Credentials come from the standard AWS chain (task role on ECS) |
| Ollama, vLLM, LM Studio | `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL` (e.g. `http://localhost:11434/v1`), `LLM_MODEL` | Start Ollama with `OLLAMA_CONTEXT_LENGTH=8192`, or lower `CONTEXT_MAX_TOKENS` |

Details, retries and error mapping: [docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md).

## Configuration

All configuration comes from environment variables, or from a `.env` file at the
repository root or in `backend/`. [`.env.example`](.env.example) lists every setting with
a safe local default or an empty placeholder. Validation lives in
[`backend/app/core/config.py`](backend/app/core/config.py). The most important groups:

| Group | Variables |
|---|---|
| Application | `APP_ENV` (`development`, `test`, `production`), `SECRET_KEY`, `PUBLIC_BASE_URL`, `ALLOWED_ORIGINS`, `TRUST_PROXY_HEADERS`, `LOG_LEVEL`, `LOG_FORMAT` |
| Database | `DATABASE_URL` (runtime role `eka_app`, subject to RLS), `MIGRATION_DATABASE_URL` (owner role `eka_owner`), `DB_POOL_SIZE`, `DB_STATEMENT_TIMEOUT_MS` |
| Redis and worker | `REDIS_URL`, `CELERY_BROKER_URL`, `INGESTION_MAX_ATTEMPTS`, `INGESTION_LEASE_SECONDS`, `WORKER_METRICS_PORT` |
| Storage | `STORAGE_BACKEND` (`local`, `s3`), `LOCAL_STORAGE_PATH`, `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_PREFIX`, `S3_SSE`, `S3_KMS_KEY_ID` |
| Limits | `MAX_UPLOAD_MB`, `MAX_REQUEST_BODY_KB`, `MAX_PDF_PAGES`, `MAX_PDF_UNCOMPRESSED_MB`, `MAX_DOCX_UNCOMPRESSED_MB`, `MAX_QUERY_CHARS`, `RATE_LIMIT_*` |
| Sessions | `COOKIE_SECURE`, `SESSION_TTL_HOURS`, `SESSION_IDLE_TIMEOUT_MINUTES`, `ALLOW_PUBLIC_REGISTRATION`, `INVITATION_TTL_HOURS`, `PASSWORD_MIN_LENGTH` |
| Embeddings | `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `EMBEDDING_QUERY_PREFIX`, `EMBEDDING_API_BASE_URL`, `EMBEDDING_CACHE_TTL_SECONDS` |
| Reranker | `RERANKER_PROVIDER`, `RERANKER_MODEL`, `RERANK_ENABLED` |
| LLM | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_QUERY_REWRITE_MODEL`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_TIMEOUT_SECONDS`, `ANTHROPIC_API_KEY`, `ANTHROPIC_PLATFORM`, `ANTHROPIC_EFFORT`, `LLM_BASE_URL`, `LLM_API_KEY` |
| Cost | `LLM_INPUT_COST_PER_MTOK`, `LLM_OUTPUT_COST_PER_MTOK` (and `LLM_REWRITE_*` for the rewrite model), used for the estimated cost in usage reports |
| Retrieval | `RETRIEVAL_MODE`, `RETRIEVAL_VECTOR_TOP_K`, `RETRIEVAL_KEYWORD_TOP_K`, `RETRIEVAL_RRF_K`, `RETRIEVAL_FINAL_TOP_K`, `RETRIEVAL_MIN_RERANK_SCORE`, `CONTEXT_MAX_TOKENS`, `HNSW_EF_SEARCH`, `QUERY_REWRITE_ENABLED` |
| Chunking | `CHUNK_TARGET_TOKENS`, `CHUNK_MAX_TOKENS`, `CHUNK_OVERLAP_TOKENS` |
| Retention | `CONVERSATION_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`, `USAGE_RETENTION_DAYS` |
| Metrics | `METRICS_ENABLED`, `METRICS_TOKEN` |

`EMBEDDING_DIMENSIONS` and `FTS_CONFIG` are read when the migrations create the schema.
Changing the embedding model therefore needs a migration and a re-index
([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#embedding-dimensions)).

With `APP_ENV=production` the API refuses to start with a placeholder `SECRET_KEY`, with
`COOKIE_SECURE=false`, or with the test-only `hashing` embeddings. The production settings
are listed in [docs/DEPLOYMENT_AWS.md](docs/DEPLOYMENT_AWS.md#9-production-configuration).

Never commit `.env` or real credentials; `.gitignore` excludes every `.env*` file except
the example. In production, secrets come from AWS Secrets Manager
([docs/DEPLOYMENT_AWS.md](docs/DEPLOYMENT_AWS.md#8-secrets)).

## API usage

The API is versioned under `/api/v1`. The OpenAPI document is served at
`/api/v1/openapi.json` and the interactive documentation at `/api/docs`.

**Authentication.** Logging in or registering sets two cookies: `eka_session`, an
HttpOnly opaque session token, and `eka_csrf`. Every state-changing request (`POST`,
`PUT`, `PATCH`, `DELETE`) made with a session must echo the `eka_csrf` value in the
`X-CSRF-Token` header. Browsers must also send an `Origin` listed in `ALLOWED_ORIGINS`.

**Errors** always use one envelope:
`{"error": {"code": "...", "message": "...", "details": ..., "request_id": "..."}}`.
A resource the caller may not read returns `404`, exactly like one that does not exist. A
readable resource the caller may not change returns `403`.

A complete session with curl:

```bash
API=http://localhost:8081/api/v1
JAR=$(mktemp)

# Register (creates an organization and a default workspace) and keep the cookies.
curl -s -c "$JAR" -H 'Content-Type: application/json' \
  -d '{"email":"ada@example.com","password":"a-long-local-passphrase","full_name":"Ada","organization_name":"Example Org"}' \
  "$API/auth/register" > me.json
CSRF=$(awk '$6 == "eka_csrf" {print $7}' "$JAR")
WORKSPACE=$(python3 -c 'import json; print(json.load(open("me.json"))["workspaces"][0]["id"])')

# Upload a document. Ingestion runs in the worker.
printf '# Travel policy\n\nHotel costs are reimbursed up to 180 euros per night in Berlin.\n' > travel.md
DOC=$(curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" -F "workspace_id=$WORKSPACE" -F "file=@travel.md" \
  "$API/documents/upload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["document"]["id"])')
curl -s -b "$JAR" "$API/documents/$DOC/status"          # queued -> processing -> indexed

# Hybrid search with reranking (only documents the caller may read are searched).
curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"query":"hotel limit in Berlin","top_k":5}' "$API/search"

# Ask a question: a JSON answer with status and citations ...
curl -s -b "$JAR" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"message":"What is the hotel limit in Berlin?"}' "$API/chat"

# ... or stream it as server-sent events (meta, sources, token..., done).
curl -sN -b "$JAR" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -d '{"message":"What is the hotel limit in Berlin?"}' "$API/chat/stream"
```

`search` accepts `filters` (`workspace_ids`, `document_ids`, `file_types`), `mode`
(`vector`, `keyword`, `hybrid`) and `rerank`. `chat` accepts `conversation_id` for a
follow-up question and `scope` (`workspace_ids`, `document_ids`). Filters can only narrow
what the caller may already read.

An answer has a `status`:

| Status | Meaning |
|---|---|
| `answered` | Answered, with at least one citation verified against the supplied passages |
| `partial` | Only part of the question is answered by the documents; at least one verified citation |
| `insufficient_evidence` | The accessible documents do not contain the answer (abstention) |
| `unverified` | The model answered, but no citation could be verified, so nothing is presented as supported |
| `refused` | The model declined; a neutral fixed message is returned |
| `error` | Generation failed; the partial text or a fixed safe message is returned |

Endpoints:

| Area | Endpoints |
|---|---|
| Health | `GET /health` (liveness), `GET /ready` (database, migrations, vector dimension, Redis, storage) |
| Auth | `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET`/`PATCH /auth/me`, `POST /auth/me/password`, `POST /auth/switch-organization`, `GET /auth/invitations/{token}`, `POST /auth/invitations/accept` |
| Organization | `GET`/`PATCH /organization`, `GET /organization/members`, `PATCH`/`DELETE /organization/members/{user_id}`, `GET`/`POST /organization/invitations`, `DELETE /organization/invitations/{invitation_id}` |
| Workspaces | `GET`/`POST /workspaces`, `GET`/`PATCH`/`DELETE /workspaces/{id}`, `GET`/`POST /workspaces/{id}/members`, `PATCH`/`DELETE /workspaces/{id}/members/{user_id}` |
| Documents | `POST /documents/upload`, `GET /documents`, `GET`/`PATCH`/`DELETE /documents/{id}`, `POST /documents/{id}/reindex`, `PUT /documents/{id}/file`, `GET /documents/{id}/status`, `GET /documents/{id}/download`, `GET /documents/{id}/chunks`, `PUT /documents/{id}/permissions` |
| Search and chat | `POST /search`, `POST /chat`, `POST /chat/stream` |
| Conversations | `GET /conversations`, `GET /conversations/{id}/messages`, `DELETE /conversations/{id}` |
| Dashboard and admin | `GET /dashboard`, `GET /admin/audit-events`, `GET /admin/usage` |

The streaming protocol is described in [docs/GENERATION.md](docs/GENERATION.md#streaming-protocol-post-chatstream).

## Testing

| What | Command | Needs |
|---|---|---|
| Backend lint, format, types | `make lint`, `make typecheck` | nothing |
| Backend dependency audit (known vulnerabilities, OSV) | `make audit` | network access |
| Backend unit tests | `make test-unit` | nothing |
| Backend unit + integration tests | `make test` | `make infra-up` |
| Real-model tests (embedding model, cross-encoder, live Ollama) | `cd backend && uv run pytest -m slow` | `local-models` extra; Ollama for the LLM tests (skipped otherwise) |
| Everything CI runs for the backend | `make check` | `make infra-up` |
| Frontend | `cd frontend && npm audit --omit=dev && npm run lint && npm run typecheck && npm test && npm run build` | network access for the audit |
| End-to-end (Playwright) | `make e2e` ([e2e/README.md](e2e/README.md)) | `make infra-up`, `npm ci` in `frontend/` and `e2e/`, `npx playwright install chromium`; the suite starts its own stack |
| RAG evaluation | `make eval`, then optionally `make eval-llm` ([Evaluation](#evaluation)) | `make infra-up` and the `local-models` extra; Ollama for `eval-llm` |

Integration tests create and migrate their own database (`eka_test`, or the value of
`EKA_TEST_DB` for parallel runs) through the local superuser, and run as the
least-privileged `eka_app` role. Row-level security is therefore active in every test,
as in production. Providers are deterministic in tests: hashing embeddings, no reranker,
extractive answers.

The suite includes the tenant-isolation and authorization tests: cross-tenant ids on
every resource, restricted documents, users removed from workspaces and organizations,
crafted search filters and chat scopes, RLS without the application layer, CSRF and
`Origin` checks, and prompt injection inside documents.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs on every push and pull
request. Its jobs: the backend (lock check, dependency audit, ruff, mypy, pytest against
PostgreSQL with pgvector and Redis services), the frontend (audit of the production
dependencies, lint, typecheck, Vitest, production build), the Docker images, the compose
file and AWS templates, and the end-to-end suite. The audits fail on any known
vulnerability. The frontend's development tools are not audited: `eslint-config-next`
depends on `braces`, which has an advisory with no fixed release (GHSA-vfj7-8cjw-p6xm),
and it only expands lint file patterns.

## Evaluation

`backend/eval` measures retrieval and answering separately. It runs the application's own
upload, ingestion, retrieval and answer code against a fresh database, with row-level
security active.

* **Corpus.** 18 synthetic documents (4 PDF, 4 DOCX, 7 Markdown, 3 text) for a fictional
  company. The corpus includes:
  * near-duplicates: the 2025 and 2026 travel policies, the UK and US benefits documents;
  * tables and technical runbooks;
  * one restricted document;
  * two documents that carry prompt injections.
* **Dataset v1.** 85 cases in these categories:
  * factual, multi-passage, table and technical questions;
  * similar pairs that need different answers;
  * unanswerable questions;
  * permission-bypass attempts, including authority claims and crafted scopes;
  * prompt injection and follow-up questions.
* **Retrieval metrics** (no generation): Recall@1/3/5/10, MRR, context precision and recall,
  and a leak check over every returned candidate. They are computed for vector-only,
  keyword-only, hybrid and hybrid + rerank retrieval.
* **Generation metrics:**
  * fact recall (correctness against reference facts);
  * faithfulness (claims supported by the passages they cite);
  * citation precision and recall;
  * abstention precision, recall and F1;
  * injection resistance;
  * answer and prompt leaks;
  * latency and tokens.
* **Deterministic scoring.** Every metric is a lexical check; no LLM judge is used. The
  definitions and their limits are in [docs/EVALUATION.md](docs/EVALUATION.md).

```bash
make eval        # retrieval for every configuration plus the offline extractive generator (minutes)
make eval-llm    # adds llama3.1:8b answers (Ollama) for vector, hybrid and hybrid + rerank (over an hour on a laptop)
```

Each run writes JSON results and a Markdown report to `backend/eval/reports/<date>/`. The
report records the machine, the package and model versions, every setting and the exact
commands.

Measured on 2026-10-19 ([report](backend/eval/reports/2026-10-19/report.md); analysis
and manual inspection in [docs/EVALUATION.md](docs/EVALUATION.md#results)):

* **Retrieval.**
  * Recall@1 / MRR: vector-only 0.754 / 0.879, keyword-only 0.746 / 0.891, hybrid
    0.790 / 0.913, hybrid + rerank 0.913 / 0.986.
  * Recall@10 is 1.000 for every configuration, because the corpus is small.
  * No configuration returned a chunk the asker may not read.
* **Answers by llama3.1:8b** (local, via Ollama):
  * Fact recall 0.94–0.97 and abstention F1 0.94–0.97 across vector, hybrid and
    hybrid + rerank retrieval. The extractive fallback scores 0.62–0.64 and 0.67–0.69.
  * No permission leak, in answers or in the 90 recorded model requests per configuration.
  * Prompt injection is the weak point: the model resisted 4 or 5 of the 7 injection
    cases. The citation validator removed every forged citation.
  * 12–15 s p50 per answer on the laptop.

## Security model

* **Tenant isolation.** Every tenant table has `FORCE ROW LEVEL SECURITY` and a policy on
  `organization_id`. The tenant is set transaction-locally on each transaction, the API
  connects as a role without `BYPASSRLS`, and composite foreign keys make cross-tenant
  references impossible.
* **Authorization.** `app/services/access.py` is the single predicate for "may read" and
  "may manage". Document APIs, search, chat retrieval, citations, downloads, the
  dashboard and conversation history all use it. Client filters are intersected with it
  inside SQL.
* **Sessions.** Opaque 256-bit tokens stored as SHA-256 hashes, with idle and absolute
  expiry. Sessions are revoked on logout, password change and member removal. HMAC CSRF
  tokens and an `Origin` check guard state changes; passwords are hashed with argon2id;
  sign-in is rate-limited per IP and per account.
* **Uploads.** The extension, the declared content type and the file's magic bytes must
  agree. Size, page and decompression limits apply, macro-enabled Office files are
  rejected, and storage keys are generated by the server. Downloads are attachments by
  default; only PDFs and plain text may be shown inline (text always as `text/plain`),
  always with `nosniff`.
* **LLM safety.** Retrieved text is fenced and labelled as untrusted data, so
  instructions inside documents are not followed, and the model has no tools. Citations
  and quotes are validated against the passages actually supplied. Answers are rendered
  as sanitized Markdown with raw HTML and images disabled.
* **Secrets and logs.** Configuration comes only from the environment or a secrets
  manager. Logs never contain document text, prompts, answers, passwords, tokens or keys.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#tenancy-and-authorization).

## Observability and cost

* **Logs.** Structured JSON with request and job ids (`LOG_FORMAT=console` for local
  reading).
* **Metrics.** Prometheus at `/metrics` on the API (protect it with `METRICS_TOKEN`) and
  on `WORKER_METRICS_PORT` in the worker. They cover HTTP traffic, retrieval stage
  latencies, generation latency, tokens, estimated cost, ingestion outcomes and job
  durations.
* **Usage.** Every answer records its input and output tokens, the models used and the
  estimated cost (from `LLM_*_COST_PER_MTOK`). Organization administrators see the totals
  at `GET /api/v1/admin/usage` and on the admin page.
* **Cost controls.** Per-user rate limits for chat, search and upload, a maximum
  query length, a context token budget (`CONTEXT_MAX_TOKENS`), a maximum output
  (`LLM_MAX_OUTPUT_TOKENS`), and an embedding cache that skips re-embedding unchanged
  passages.

## Deployment

[docs/DEPLOYMENT_AWS.md](docs/DEPLOYMENT_AWS.md) describes a production deployment:
ECS Fargate services for the API, worker, beat and frontend; RDS PostgreSQL 17 with
pgvector; ElastiCache Redis; S3 with SSE-KMS; Secrets Manager; an ALB configured for SSE;
autoscaling, alarms, backups, releases and rollback. Task definitions, IAM policies, the
S3 bucket policy and the RDS bootstrap SQL are in [infra/aws](infra/aws/README.md).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `make infra-up` or `make api` fails with "address already in use" | Another process owns the port. Run `make check-ports`, then change the `EKA_*_PORT` value and the URLs that contain it in `.env` (or run `make api API_PORT=8082`) |
| `403 origin_not_allowed` | The UI's origin is not in `ALLOWED_ORIGINS` (comma-separated, no trailing slash) |
| `403 csrf_failed` | A state-changing request lacks `X-CSRF-Token` with the current `eka_csrf` cookie value (it changes on every login) |
| `GET /api/v1/ready` returns 503 | The body names the failing check. Run `make migrate` for missing migrations. A vector dimension mismatch means `EMBEDDING_DIMENSIONS` differs from the database column |
| Documents stay `queued` | No worker is consuming the `ingestion` queue (`make worker`). Run `make beat` too: its reaper re-queues jobs whose broker message was lost and expired leases |
| A document is `failed` | `processing_error` on the document says why (for example `encrypted_pdf`, `empty_document`, `corrupt_file`, `too_many_pages`). Fix the file and use replace or reindex |
| The first search or chat after a start is slow | The API loads the embedding and reranker models in the background as soon as it starts (`models.warmed_up` in its log); a search that arrives earlier waits for them. The very first start also downloads them (into the `hf-cache` volume in Docker) |
| Uploads fail with `413` | The file exceeds `MAX_UPLOAD_MB` (default 50). The Next.js proxy buffers bodies up to 100 MB; raise `proxyClientMaxBodySize` in `frontend/next.config.ts` if you raise the limit beyond that |
| Ollama answers ignore the documents or abstain unexpectedly | Ollama silently truncates prompts longer than its context window (often 4096 tokens). Start it with `OLLAMA_CONTEXT_LENGTH=8192`, or lower `CONTEXT_MAX_TOKENS` |
| `make worker` on macOS shows `pool: solo` | Intentional: Celery's prefork pool does not work on macOS. Containers use prefork with one child, which also enforces the task time limits |
| `uv` refuses to install Python | The project never downloads interpreters (`python-downloads = "never"`). Install Python 3.12+ yourself, for example `brew install python@3.14` |
| Integration tests fail to connect | Start `make infra-up`. Tests reach PostgreSQL on `EKA_POSTGRES_PORT` with the local superuser (`TEST_ADMIN_DATABASE_URL` overrides the URL) |
| `ModuleNotFoundError: sentence_transformers` | Install the extra (`uv sync --extra local-models`), or configure `EMBEDDING_PROVIDER=openai_compatible` and `RERANKER_PROVIDER=none` |

Production issues (ALB timeouts, migrations, secrets) are covered in
[docs/DEPLOYMENT_AWS.md](docs/DEPLOYMENT_AWS.md#16-troubleshooting).

## What has and has not been tested

Tested, with results reported from actual runs:

* **Backend.** 1093 tests: unit tests, and integration tests against PostgreSQL with pgvector
  and Redis. They include the real embedding model and cross-encoder, and a live Ollama
  server for the OpenAI-compatible provider. CI runs everything except the real-model tests
  on every push.
* **Frontend.** 155 Vitest tests, ESLint, TypeScript and the production build.
* **End-to-end.** 27 Playwright tests in Chromium against the real stack (API, worker,
  frontend, PostgreSQL, Redis). They cover permissions, cross-tenant access, XSS payloads,
  CSRF, streaming and Stop. The run also checks that the service logs contain no passwords,
  keys, document text or questions. All 27 passed in each of two consecutive local runs
  (macOS) and in GitHub Actions (Ubuntu).
* **Container stack.** The full stack (`make up-app`) was tested by hand: register, upload,
  ingestion, hybrid search with reranking, and a streamed answer with a verified citation.
* **RAG evaluation**, as described above.

Not tested:

* **Claude API and Amazon Bedrock.** No credentials were available. The providers are
  tested only against a fake HTTP transport that replays the API's event streams
  ([docs/LLM_PROVIDERS.md](docs/LLM_PROVIDERS.md#what-was-and-was-not-tested)).
* **vLLM and LM Studio.** They speak the same protocol as Ollama, but only Ollama was run.
* **The AWS deployment** (ECS, RDS, ElastiCache, S3 with KMS) has not been deployed.
  * The templates are checked as JSON and the compose file is validated.
  * S3 storage is tested against moto, an in-process S3 emulator.
* **Browsers other than Chromium**, and assistive technologies.
* **Production-like conditions:** load, many concurrent users, and tenants with thousands
  of documents.
* **Other document types:** OCR or scanned PDFs, and documents that are not in English.
