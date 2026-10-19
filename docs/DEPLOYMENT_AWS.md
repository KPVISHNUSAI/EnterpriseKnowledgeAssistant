# Deploying to AWS

This guide runs the Enterprise Knowledge Assistant on ECS Fargate with RDS PostgreSQL,
ElastiCache Redis and S3. The IAM policies, task definitions, bucket policy and database
bootstrap script it refers to are in [infra/aws/](../infra/aws/README.md) and contain
placeholders only. Application behaviour is described in [ARCHITECTURE.md](ARCHITECTURE.md).

Contents: [architecture](#1-architecture) - [images](#2-container-images) -
[network](#3-networking) - [database](#4-database-rds-postgresql-17-with-pgvector) -
[Redis](#5-redis-elasticache) - [S3](#6-object-storage-s3) - [IAM](#7-iam) -
[secrets](#8-secrets) - [configuration](#9-production-configuration) -
[load balancer and SSE](#10-load-balancer-and-streaming-sse) - [ECS](#11-ecs-services) -
[scaling](#12-scaling) - [monitoring](#13-monitoring-and-alarms) -
[backups](#14-backups-and-retention) - [release and rollback](#15-first-deployment-release-and-rollback) -
[troubleshooting](#16-troubleshooting)

## 1. Architecture

```
Browser --HTTPS 443--> ALB (public subnets; ACM certificate, optional AWS WAF)
                        |
                        +-- /api/*         --> eka-api -------+--> RDS PostgreSQL 17 + pgvector
                        |                                     +--> ElastiCache Redis (jobs, limits, cache)
                        +-- everything else --> eka-frontend  +--> S3 bucket (originals, SSE-KMS)
                                                   |
                                                   +--> eka-api (server-side calls, Service Connect)

eka-beat     --> Redis             schedules the maintenance tasks (exactly one task)
eka-worker   <-- Redis             ingestion and maintenance jobs; reads and writes RDS and S3
eka-migrate  --> RDS               one-off task as the schema-owner role

ECS Fargate tasks and the data stores run in private subnets across 2+ Availability Zones.
```

| Concern | AWS service | Notes |
|---|---|---|
| Edge and TLS | Route 53, ACM, ALB (optional WAF) | TLS ends at the ALB; `/api/*` is routed straight to the API |
| UI | ECS Fargate `eka-frontend` | Next.js, port 3000 |
| API | ECS Fargate `eka-api` | FastAPI, one uvicorn process per task, port 8081 |
| Ingestion | ECS Fargate `eka-worker`, `eka-beat` | Celery, queues `ingestion` and `maintenance`; exactly one beat task |
| Migrations | ECS Fargate one-off task `eka-migrate` | `alembic upgrade head` as the schema-owner role |
| Database | RDS PostgreSQL 17, pgvector | roles `eka_owner` (migrations) and `eka_app` (runtime, `NOBYPASSRLS`) |
| Queue and caches | ElastiCache for Redis (or Valkey) | Celery broker, rate limits, embedding cache |
| Originals | S3 with SSE-KMS | presigned download redirects |
| Secrets | Secrets Manager | injected as ECS secrets |
| Logs and metrics | CloudWatch Logs, Container Insights, Prometheus scrape | see section 13 |
| Images | ECR | immutable tags |

Everything runs from one backend image and one frontend image. Redis is only a trigger: the
`ingestion_jobs` table is the source of truth, so losing Redis delays work but loses none.

## 2. Container images

Build for the architecture of the Fargate tasks (`X86_64` below; for `ARM64` use
`--platform linux/arm64` and `CPU_ARCHITECTURE=ARM64`; `uv.lock` has wheels for both).

```bash
REGISTRY=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com
TAG=$(git rev-parse --short=12 HEAD)

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"
docker buildx build --platform linux/amd64 -t "$REGISTRY/eka-backend:$TAG" --push backend
docker buildx build --platform linux/amd64 -t "$REGISTRY/eka-frontend:$TAG" --push frontend
```

* Create the repositories `eka-backend` and `eka-frontend` with immutable tags and scan on
  push. Deploy by tag (the git SHA) or digest, never `latest`.
* The frontend rewrites `/api/*` to `API_INTERNAL_URL`. Next.js evaluates rewrites when it
  builds, so if `frontend/Dockerfile` declares an `API_INTERNAL_URL` build argument, pass
  `--build-arg API_INTERNAL_URL=http://api:8081` (the Service Connect name of section 11).
* No secret is baked into either image. The backend image runs as uid 10001, its code is
  read-only for that user, and it writes only to `HF_HOME` and `LOCAL_STORAGE_PATH`
  (unused with S3).

### Embedding and reranker models

The local models (`EMBEDDING_MODEL`, `RERANKER_MODEL`) download from Hugging Face on first
use. Do not let production tasks do that: it needs internet access, slows every start and
adds an external dependency. Pick one:

1. **Bake them into a derived image** (recommended; tasks then need no internet access for
   models). Use the same model names as in the task definitions:

   ```dockerfile
   FROM <registry>/eka-backend:<git-sha>
   ARG EMBEDDING_MODEL
   ARG RERANKER_MODEL
   RUN python -c "import sys; from sentence_transformers import CrossEncoder, SentenceTransformer; SentenceTransformer(sys.argv[1]); CrossEncoder(sys.argv[2])" "$EMBEDDING_MODEL" "$RERANKER_MODEL"
   ENV HF_HUB_OFFLINE=1
   ```

   The build runs as the non-root user and fills `HF_HOME` with exactly the files the app
   loads. Rebuild it whenever the backend image or a model name changes.
2. **EFS cache.** Mount an EFS access point (POSIX uid and gid 10001, root directory
   `/hf`) at `/var/lib/eka/huggingface` in `api.json` and `worker.json`: add `mountPoints`
   `{"sourceVolume": "hf-cache", "containerPath": "/var/lib/eka/huggingface"}` and a volume
   `{"name": "hf-cache", "efsVolumeConfiguration": {"fileSystemId": "<fs-id>", "transitEncryption": "ENABLED", "authorizationConfig": {"accessPointId": "<fsap-id>", "iam": "ENABLED"}}}`.
   The task role then needs `elasticfilesystem:ClientMount` and `elasticfilesystem:ClientWrite`
   on the file system, and the mount targets' security group must allow 2049 from the tasks.
   Fill the cache once from a task that has internet access, then keep `HF_HUB_OFFLINE=1`.

The templates set `HF_HUB_OFFLINE=1`, so a task without cached models fails fast with a
clear error instead of silently downloading.

## 3. Networking

* One VPC (`VPC_CIDR`) with at least two Availability Zones. **Public subnets** hold only
  the ALB (and a NAT gateway if you need one). **Private subnets** hold the Fargate tasks,
  RDS and ElastiCache; tasks get no public IP (`assignPublicIp=DISABLED`).
* Security groups follow least access. Reference groups, not CIDRs.

| Security group | Inbound | Outbound |
|---|---|---|
| `alb` | 443 from the internet (or your WAF/CloudFront prefix list), 80 for the redirect | 8081 to `api`, 3000 to `frontend` |
| `api` | 8081 from `alb` and `frontend` | 5432 to `rds`, 6379 to `redis`, 443 to `endpoints` and the S3 prefix list; 443 to the internet only for `ANTHROPIC_PLATFORM=anthropic` |
| `frontend` | 3000 from `alb` | 8081 to `api`, 443 to `endpoints` |
| `worker` | none (9101 only from your metrics collector, if it scrapes across tasks) | 5432 to `rds`, 6379 to `redis`, 443 to `endpoints` and the S3 prefix list |
| `beat` | none | 6379 to `redis`, 443 to `endpoints` |
| `migrate` | none | 5432 to `rds`, 443 to `endpoints` |
| `rds` | 5432 from `api`, `worker`, `migrate` | none |
| `redis` | 6379 from `api`, `worker`, `beat` | none |
| `endpoints` | 443 from every task group | none |

* **VPC endpoints** keep AWS traffic off the internet and remove the need for a NAT gateway:

| Endpoint | Type | Used for |
|---|---|---|
| `com.amazonaws.<region>.s3` | Gateway | document objects, and ECR image layers |
| `ecr.api`, `ecr.dkr` | Interface | image pulls |
| `secretsmanager` | Interface | ECS secret injection at task start |
| `logs` | Interface | the `awslogs` log driver |
| `bedrock-runtime` | Interface | only with `ANTHROPIC_PLATFORM=bedrock` |
| `monitoring` or `aps-workspaces` | Interface | only if a collector pushes metrics from the private subnets |

  If you attach an endpoint policy to the S3 gateway endpoint, it must also allow
  `arn:aws:s3:::prod-<region>-starport-layer-bucket/*`, where ECR keeps image layers, or image
  pulls fail. Browsers download documents from S3 over the internet through presigned URLs, so
  do not add an `aws:SourceVpce` condition to the bucket policy.
* **Internet egress.** `LLM_PROVIDER=anthropic` with `ANTHROPIC_PLATFORM=anthropic` calls the
  Anthropic API, so the `api` tasks need a route through a NAT gateway (and 443 outbound).
  With Bedrock, or with the extractive provider, no NAT gateway is needed. Models must not
  need downloads (section 2).
* The security groups of RDS and ElastiCache must not allow anything from outside the VPC;
  neither is publicly accessible.

## 4. Database: RDS PostgreSQL 17 with pgvector

Create the instance in private subnets with a dedicated subnet group:

| Setting | Value |
|---|---|
| Engine | PostgreSQL 17, Multi-AZ, storage encrypted with a KMS key, gp3 with storage autoscaling (set a maximum) |
| Parameter group | `rds.force_ssl=1`; no other change is required |
| Backups | automated, retention 14 days or more, deletion protection on, final snapshot on delete |
| Monitoring | Performance Insights and enhanced monitoring on, Log exports for `postgresql` |
| Connections | the app opens up to `DB_POOL_SIZE + DB_MAX_OVERFLOW` (10 + 10) connections per process, so `max_connections` must cover (api tasks + worker tasks) x 20 with headroom |
| Memory | the HNSW index should fit in memory: 1 million chunks at 384 dimensions are about 1.5 GB of raw vectors plus graph overhead |

Check that the instance offers pgvector 0.5.0 or newer (the migration builds an HNSW index
with `vector_cosine_ops`): `SELECT name, default_version FROM pg_available_extensions WHERE name = 'vector'`.

### Roles, database and extension

The runtime and the migrations use different roles so that row-level security always applies
to the application (see [ARCHITECTURE.md](ARCHITECTURE.md#tenancy-and-authorization)):

* `eka_owner` owns the schema and is used only by the migration task (`MIGRATION_DATABASE_URL`).
* `eka_app` is used by the API and workers (`DATABASE_URL`) and is created `NOSUPERUSER
  NOCREATEDB NOCREATEROLE NOBYPASSRLS`. The migration grants it table privileges only.

[infra/aws/rds/bootstrap.sql](../infra/aws/rds/bootstrap.sql) creates both roles, the `eka`
database and `CREATE EXTENSION vector`, mirroring `infra/postgres/init/01-init.sh`. Run it once
as the RDS master user from a host inside the VPC (CloudShell in the VPC, an SSM-managed
bastion or a one-off task with the `psql` client):

```bash
psql "host=<rds-endpoint> dbname=postgres user=<master-user> sslmode=require" \
  -v ON_ERROR_STOP=1 -v owner_pw="$EKA_OWNER_PASSWORD" -v app_pw="$EKA_APP_PASSWORD" \
  -f infra/aws/rds/bootstrap.sql
```

Generate the two passwords with `openssl rand -hex 24` (URL-safe, because they are embedded in
the connection URLs) and store them straight in Secrets Manager (section 8). The master user
creates the extension because `vector` needs `rds_superuser`; the migration's own
`CREATE EXTENSION IF NOT EXISTS` is then a no-op for `eka_owner`.

Connection URLs enforce TLS with `?sslmode=require`:

```
DATABASE_URL=postgresql+psycopg://eka_app:<password>@<rds-endpoint>:5432/eka?sslmode=require
MIGRATION_DATABASE_URL=postgresql+psycopg://eka_owner:<password>@<rds-endpoint>:5432/eka?sslmode=require
```

`sslmode=require` encrypts the connection. To also verify the server certificate, add the RDS
CA bundle to your image and use `sslmode=verify-full&sslrootcert=<path>`.

### Migrations

Migrations run as a one-off Fargate task, never inside the API start-up, so that one task
migrates while many serve:

```bash
TASK_ARN=$(aws ecs run-task --cluster eka --launch-type FARGATE --task-definition eka-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-a>,<private-subnet-b>],securityGroups=[<migrate-sg>],assignPublicIp=DISABLED}" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster eka --tasks "$TASK_ARN"
aws ecs describe-tasks --cluster eka --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].{exitCode:exitCode,reason:reason}'    # exitCode must be 0
```

The migrate task definition is the only one that can read the owner credentials. It must see
the same `EMBEDDING_DIMENSIONS`, `FTS_CONFIG` and `DB_APP_ROLE` as the runtime tasks: the first
migration bakes the dimension into the `vector(N)` column, and `/api/v1/ready` reports 503 if
they differ. Because the previous release keeps serving while a migration runs, write
migrations so that the old code still works with the new schema (add first, remove in a later
release).

## 5. Redis (ElastiCache)

| Setting | Value |
|---|---|
| Engine | ElastiCache for Redis OSS 7.x or Valkey, replication group with a replica and automatic failover (Multi-AZ) |
| Security | in-transit encryption, at-rest encryption, AUTH token (or an RBAC user), private subnet group, security group `redis` |
| Eviction | keep `maxmemory-policy volatile-lru` (the default). Celery queues are lists without a TTL and are never evicted; the embedding cache and rate-limit keys all have TTLs. Do not use an `allkeys-*` policy |
| Persistence | not required. Losing the queue is recoverable: the reaper re-queues queued jobs whose message was lost after `INGESTION_STALE_QUEUED_SECONDS` |

```
REDIS_URL=rediss://:<auth-token>@<primary-endpoint>:6379/0?ssl_cert_reqs=required
```

Keep `ssl_cert_reqs=required`. Without it, Celery's Redis transport only logs a warning and
uses TLS without verifying the server certificate.

## 6. Object storage (S3)

Use a dedicated bucket in the same Region and settings as follows. The commands assume
`BUCKET_NAME` and `KMS_KEY_ARN` are exported; the JSON files are rendered with `envsubst`
(see [infra/aws/README.md](../infra/aws/README.md)).

```bash
aws s3api create-bucket --bucket "$BUCKET_NAME" --region "$AWS_REGION" \
  --create-bucket-configuration LocationConstraint="$AWS_REGION"   # omit in us-east-1
aws s3api put-public-access-block --bucket "$BUCKET_NAME" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-ownership-controls --bucket "$BUCKET_NAME" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-encryption --bucket "$BUCKET_NAME" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms","KMSMasterKeyID":"'"$KMS_KEY_ARN"'"},"BucketKeyEnabled":true}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET_NAME" --versioning-configuration Status=Enabled
aws s3api put-bucket-policy --bucket "$BUCKET_NAME" --policy file://var/aws/bucket-policy.json
aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET_NAME" \
  --lifecycle-configuration file://infra/aws/s3/lifecycle.json
```

* **Block Public Access** (all four settings) and **ACLs disabled** (`BucketOwnerEnforced`).
  Objects are only ever read through short-lived presigned URLs.
* **Encryption** is SSE-KMS with a customer-managed key (annual rotation on) and S3 Bucket
  Keys, which cut KMS requests. The application sends the encryption headers itself:
  `S3_SSE=aws:kms` and `S3_KMS_KEY_ID=<full key ARN>`.
* **[bucket-policy.json](../infra/aws/s3/bucket-policy.json)** denies every request that is
  not TLS and every upload that asks for an encryption other than `aws:kms`.
* **Versioning and lifecycle** ([lifecycle.json](../infra/aws/s3/lifecycle.json)) keep old
  versions for 90 days and abort unfinished multipart uploads after 7 days; adjust to your
  retention rules.
* **Presigned URLs.** `S3_PRESIGN_TTL_SECONDS` (default 300) is how long a download link
  works; keep it short. Links are signed with the task role's temporary credentials and stop
  working when those expire, and for SSE-KMS objects the signer also needs `kms:Decrypt`,
  which the task role policy grants. Set `S3_REGION` so links use the regional endpoint.
* **No CORS configuration is needed.** The browser follows a redirect to the presigned URL
  (a navigation, not a cross-origin request) and uploads go through the API. Add CORS only if
  you later let browsers call S3 directly.
* **Key policy.** The KMS key policy must delegate to IAM (the default "Enable IAM policies"
  statement) so that the task role policy below takes effect.

## 7. IAM

ECS uses two kinds of role, and mixing them up is the most common least-privilege mistake:

* The **execution role** is used by the ECS agent, before and around your container: it pulls
  the image, writes logs and reads the Secrets Manager entries that become environment
  variables. The application code never sees these credentials.
* The **task role** is what your code gets through the SDK's credential chain (S3, KMS,
  Bedrock). It is optional: beat, migrate and the frontend call no AWS API, so they have none.
  The API and the worker get separate roles so that each has only what it uses.

| Role | Attached to | Policies |
|---|---|---|
| `eka-task-execution` | api, worker, beat, frontend | managed `AmazonECSTaskExecutionRolePolicy` (ECR pull, log writes) plus [execution-role-policy.json](../infra/aws/iam/execution-role-policy.json): `secretsmanager:GetSecretValue` on the runtime secret and `kms:Decrypt` on the key that encrypts it |
| `eka-task-execution-migrate` | migrate | the managed policy plus [execution-role-migrate-policy.json](../infra/aws/iam/execution-role-migrate-policy.json), which adds the owner-credential secret. Runtime tasks cannot read it |
| `eka-task-api` | api | [task-role-policy.json](../infra/aws/iam/task-role-policy.json): `s3:GetObject`, `PutObject`, `DeleteObject` on the objects under `S3_PREFIX`; `s3:ListBucket` on the bucket; `kms:GenerateDataKey` and `kms:Decrypt` on the bucket key, only through S3 |
| `eka-task-api` (optional) | api | [task-role-bedrock-policy.json](../infra/aws/iam/task-role-bedrock-policy.json): `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, only with `ANTHROPIC_PLATFORM=bedrock` |
| `eka-task-worker` | worker | the same `task-role-policy.json`, nothing else. The worker parses uploaded files, so it gets no model access |

Notes on the task roles: `s3:ListBucket` is required by the readiness probe (`HeadBucket`), and
it also makes a missing key return 404 instead of 403. Every role trusts
`ecs-tasks.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn` conditions
([ecs-tasks-trust-policy.json](../infra/aws/iam/ecs-tasks-trust-policy.json)) against the
confused-deputy problem.

```bash
aws iam create-role --role-name eka-task-execution \
  --assume-role-policy-document file://var/aws/ecs-tasks-trust-policy.json
aws iam attach-role-policy --role-name eka-task-execution \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name eka-task-execution --policy-name eka-runtime-secret \
  --policy-document file://var/aws/execution-role-policy.json
# Repeat for eka-task-execution-migrate (migrate policy), eka-task-api and eka-task-worker
# (task-role-policy.json; add the Bedrock policy to eka-task-api when you use Bedrock).
```

Your deployment pipeline needs its own role to push to ECR and update ECS services. Use
GitHub OIDC federation to assume it; do not store access keys. The CI workflow in this
repository only builds and tests.

## 8. Secrets

Secrets live in Secrets Manager and reach containers as ECS `secrets`. Never put them in the
image, in `environment` or in a task definition's plaintext, and never commit rendered files.

| Secret | JSON key | Used by |
|---|---|---|
| `eka/prod/app` | `SECRET_KEY` | api, worker, beat, migrate |
| | `DATABASE_URL` (role `eka_app`) | api, worker, beat, migrate |
| | `REDIS_URL` | api, worker, beat |
| | `METRICS_TOKEN` | api |
| | `ANTHROPIC_API_KEY` (omit for Bedrock) | api |
| `eka/prod/migration` | `MIGRATION_DATABASE_URL` (role `eka_owner`) | migrate only |

The settings object requires `SECRET_KEY` and `DATABASE_URL` at start-up even for processes that
barely use them, which is why beat and migrate receive them. Their network access is still
limited by security groups (beat cannot reach RDS).

```bash
umask 077
cat > app-secret.json <<'JSON'
{"SECRET_KEY":"<64 hex characters>","DATABASE_URL":"<url>","REDIS_URL":"<url>",
 "METRICS_TOKEN":"<random>","ANTHROPIC_API_KEY":"<key>"}
JSON
aws secretsmanager create-secret --name eka/prod/app --kms-key-id "$SECRETS_KMS_KEY_ARN" \
  --secret-string file://app-secret.json
shred -u app-secret.json 2>/dev/null || rm -P app-secret.json
```

* Generate `SECRET_KEY` with `openssl rand -hex 32`. Use hex only: the production check
  rejects any value containing `change`, `replace`, `example` or `dev`, which a random
  alphanumeric string can contain by chance.
* ECS resolves secrets when a task starts. After rotating a database password or a token,
  update the secret, then `aws ecs update-service --force-new-deployment` for every service.
* Rotating `SECRET_KEY` invalidates every CSRF cookie, so users have to sign in again.
* The application never logs passwords, tokens, API keys, prompts, answers or document text.

## 9. Production configuration

Set these in the task definitions (plain values in `environment`, sensitive ones in `secrets`).
The defaults of every other setting are in `backend/app/core/config.py` and `.env.example`.

| Variable | Production value | Notes |
|---|---|---|
| `APP_ENV` | `production` | Turns on start-up checks: a placeholder `SECRET_KEY`, `COOKIE_SECURE=false` and `EMBEDDING_PROVIDER=hashing` are rejected |
| `SECRET_KEY` | secret, 64 hex characters | Signs CSRF tokens |
| `LOG_FORMAT`, `LOG_LEVEL` | `json`, `INFO` | One JSON object per line for CloudWatch |
| `PUBLIC_BASE_URL` | `https://<your-domain>` | Public origin of the site |
| `ALLOWED_ORIGINS` | the same origin, no trailing slash; comma-separated for several | A browser `Origin` on POST/PUT/PATCH/DELETE must match, otherwise 403 `origin_not_allowed` |
| `COOKIE_SECURE` | `true` | Required in production |
| `COOKIE_DOMAIN` | unset | Only if UI and API are on different subdomains |
| `TRUST_PROXY_HEADERS` | `true` (api) | Behind the ALB only. The client IP (rate limits, audit log) is the last `X-Forwarded-For` entry, which the ALB appends |
| `FORWARDED_ALLOW_IPS` | `VPC_CIDR` (api) | Read by uvicorn (`--proxy-headers`): trusts `X-Forwarded-Proto` from the ALB so redirects use https. Not an application setting |
| `ALLOW_PUBLIC_REGISTRATION` | `false` (the default) | Sign-up by invitation only. Create the first organization and its admin with `scripts/create_organization.py` (section 15) |
| `DATABASE_URL`, `MIGRATION_DATABASE_URL` | secrets | section 4. `DB_APP_ROLE` stays `eka_app` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_STATEMENT_TIMEOUT_MS` | 10, 10, 30000 | Per process; see the connection budget |
| `REDIS_URL` | secret | section 5. `CELERY_BROKER_URL` is unset and falls back to it |
| `INGESTION_MAX_ATTEMPTS`, `INGESTION_LEASE_SECONDS` | 3, 900 | The lease is also the Celery time limit; size `stopTimeout` and alarms with it in mind |
| `WORKER_METRICS_PORT` | 9101 | Where the worker exposes `/metrics` |
| `STORAGE_BACKEND` | `s3` | `S3_BUCKET` is then required |
| `S3_BUCKET`, `S3_REGION`, `S3_PREFIX` | bucket, Region, key prefix | A dedicated bucket needs no prefix (empty). With one, use a trailing slash (`eka/`); the task-role policy is limited to the same prefix |
| `S3_SSE`, `S3_KMS_KEY_ID` | `aws:kms`, full key ARN | `S3_ENDPOINT_URL` stays unset on AWS |
| `S3_PRESIGN_TTL_SECONDS` | 300 | Lifetime of download links |
| `MAX_UPLOAD_MB`, `MAX_REQUEST_BODY_KB` | 50, 1024 | The ALB has no body limit; keep a WAF rule consistent with these |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS` | `sentence_transformers`, model, 384 | The dimension must match the database column and be identical in migrate |
| `RERANKER_PROVIDER`, `RERANKER_MODEL`, `RERANK_ENABLED` | as in `.env.example` | |
| `HF_HUB_OFFLINE` | `1` | Models come from the image or EFS (section 2) |
| `LLM_PROVIDER`, `LLM_MODEL` | `anthropic`, model name | Cost settings: `LLM_INPUT_COST_PER_MTOK`, `LLM_OUTPUT_COST_PER_MTOK` |
| `ANTHROPIC_API_KEY` | secret | Not needed with Bedrock |
| `ANTHROPIC_PLATFORM`, `AWS_REGION` | `bedrock`, Region | Bedrock only. Set `LLM_MODEL` to the Bedrock model or inference profile id and attach the Bedrock policy |
| `METRICS_ENABLED`, `METRICS_TOKEN` | `true`, secret | Always set a token; without one `/metrics` is open to whatever can reach the task |
| `RATE_LIMIT_*` | defaults | Per client IP, so `TRUST_PROXY_HEADERS` must be correct |
| `CONVERSATION_RETENTION_DAYS`, `AUDIT_RETENTION_DAYS`, `USAGE_RETENTION_DAYS` | 365, 730, 400 | Applied by beat every 6 hours |

## 10. Load balancer and streaming (SSE)

| Setting | Value |
|---|---|
| Listeners | 443 with an ACM certificate and a current TLS policy (for example `ELBSecurityPolicy-TLS13-1-2-2021-06`); 80 redirects to 443 |
| Target group `eka-api` | IP targets, HTTP 8081, health check `GET /api/v1/health` (liveness only), healthy 2, unhealthy 3, interval 15-30 s |
| Target group `eka-frontend` | IP targets, HTTP 3000, health check `/`, success codes 200-399 |
| Rule, priority 10 | path `/api/*` forwards to `eka-api`; the default action forwards to `eka-frontend` |
| Load balancer attributes | `idle_timeout.timeout_seconds` 300 or more, `routing.http.drop_invalid_header_fields.enabled` true, `routing.http.xff_header_processing.mode` `append` (the default; the app trusts the last entry) |
| Target group attribute | `deregistration_delay.timeout_seconds` 120 |

```bash
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "$ALB_ARN" --attributes \
  Key=idle_timeout.timeout_seconds,Value=300 \
  Key=routing.http.drop_invalid_header_fields.enabled,Value=true
aws elbv2 modify-target-group-attributes --target-group-arn "$API_TG_ARN" \
  --attributes Key=deregistration_delay.timeout_seconds,Value=120
aws elbv2 create-rule --listener-arn "$HTTPS_LISTENER_ARN" --priority 10 \
  --conditions Field=path-pattern,Values='/api/*' \
  --actions Type=forward,TargetGroupArn="$API_TG_ARN"
```

Why `/api/*` goes to the API directly instead of through the Next.js rewrite: the API sees the
client address the ALB recorded (rate limits and audit logs would otherwise see the frontend
task), and chat streams do not pass through a second proxy with its own timeouts. The rewrite
stays useful for server-side calls and for local Docker. `/metrics` is not under `/api`, so
it is never reachable from the internet. Add a higher-priority rule returning a fixed 404 for
`/api/docs*` and `/api/v1/openapi.json` if you do not want the interactive API docs public.

**Server-Sent Events** (`POST` chat answers):

* The ALB does not buffer responses. The application sends `Cache-Control: no-cache,
  no-transform` and `X-Accel-Buffering: no`, uses no buffering middleware and writes a
  `: keep-alive` comment every 15 seconds while the model is silent.
* Set the idle timeout to 300 seconds or more so that a slow model or client never hits the
  60-second default. The heartbeat keeps the connection active, but the longer timeout is the
  safety margin.
* If you put CloudFront or another proxy in front, disable response buffering and
  compression for `text/event-stream` and raise its origin read timeout accordingly.
* When a client disconnects the application cancels generation and saves the partial answer.
  On deployment, uvicorn lets in-flight streams finish: `stopTimeout` is 120 s (the Fargate
  maximum) and the deregistration delay 120 s.

## 11. ECS services

Create a cluster with Container Insights and the `FARGATE` capacity provider
(`FARGATE_SPOT` is an option for workers: an interrupted job is retried after its lease
expires). Create the log groups before the first task starts:

```bash
for s in api worker beat migrate frontend; do
  aws logs create-log-group --log-group-name /ecs/eka/$s
  aws logs put-retention-policy --log-group-name /ecs/eka/$s --retention-in-days 90
done
```

Render and register the task definitions as described in
[infra/aws/README.md](../infra/aws/README.md). Starting sizes:

| Service | Task definition | Size | Desired | Deployment (`minimum / maximum` percent) |
|---|---|---|---|---|
| `eka-api` | `api.json` | 2 vCPU, 4 GiB | 2 or more, spread over AZs | 100 / 200, circuit breaker with rollback, health check grace period 120 s |
| `eka-frontend` | `frontend.json` | 0.5 vCPU, 1 GiB | 2 | 100 / 200, circuit breaker with rollback |
| `eka-worker` | `worker.json` | 2 vCPU, 4 GiB | 1 or more (section 12) | 100 / 200 |
| `eka-beat` | `beat.json` | 0.25 vCPU, 0.5 GiB | exactly 1 | **0 / 100**, so the old scheduler stops before the new one starts |
| `eka-migrate` | `migrate.json` | 0.5 vCPU, 1 GiB | one-off `run-task` | none |

The worker runs Celery's prefork pool with one child: one job at a time per task, scaled by
task count. The API service in full (`<...>` are your values):

```bash
aws ecs create-service --cluster eka --service-name eka-api --task-definition eka-api \
  --desired-count 2 --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-a>,<private-subnet-b>],securityGroups=[<api-sg>],assignPublicIp=DISABLED}" \
  --load-balancers "targetGroupArn=$API_TG_ARN,containerName=api,containerPort=8081" \
  --health-check-grace-period-seconds 120 \
  --deployment-configuration "minimumHealthyPercent=100,maximumPercent=200,deploymentCircuitBreaker={enable=true,rollback=true}" \
  --service-connect-configuration '{"enabled":true,"namespace":"eka","services":[{"portName":"http","clientAliases":[{"port":8081,"dnsName":"api"}],"timeout":{"idleTimeoutSeconds":300,"perRequestTimeoutSeconds":0}}]}'
```

**Service-to-service traffic.** The frontend reaches the API at `API_INTERNAL_URL` (set to
`http://api:8081` in the template). The command above publishes the API under that name with
ECS Service Connect; create the frontend service with
`--service-connect-configuration '{"enabled":true,"namespace":"eka"}'` and the `eka`
namespace. An internal ALB or Cloud Map DNS works as well, then change `API_INTERNAL_URL`.
The timeouts above are for the rare stream that does cross Service Connect.

**Container health checks.** The API and frontend templates define one. The worker has none:
ECS replaces it when the process exits, Celery's hard time limit kills a stuck job, and a
probe of the `/metrics` port would pass while a job is stuck anyway. Watch the worker
through the alarms of section 13. Beat has no endpoint to probe.

**Known limits.**

* *Time limits.* The worker runs prefork with one child (`--concurrency 1` in `worker.json`),
  so Celery enforces `task_time_limit` (`INGESTION_LEASE_SECONDS`) and a soft limit 60 s
  earlier; a document that needs longer fails with `timeout`. Size the lease for the largest
  documents and the slowest embedding model.
* *Beat state.* Beat keeps its schedule file in `/tmp`, so it is lost when the task restarts.
  That is harmless: the reaper runs every minute, and retention runs on a wall-clock
  `crontab` (minute 15 of every sixth hour, UTC), so restarts do not postpone it.

## 12. Scaling

* **API.** One uvicorn process per task (no `--workers`): each process loads the embedding
  model and reranker, so memory scales with processes, and Fargate scales horizontally
  better. Scale tasks on CPU with target tracking (about 60 percent):

```bash
aws application-autoscaling register-scalable-target --service-namespace ecs \
  --resource-id service/eka/eka-api --scalable-dimension ecs:service:DesiredCount \
  --min-capacity 2 --max-capacity 10
aws application-autoscaling put-scaling-policy --policy-name eka-api-cpu \
  --service-namespace ecs --resource-id service/eka/eka-api \
  --scalable-dimension ecs:service:DesiredCount --policy-type TargetTrackingScaling \
  --target-tracking-scaling-policy-configuration '{"TargetValue":60.0,"PredefinedMetricSpecification":{"PredefinedMetricType":"ECSServiceAverageCPUUtilization"},"ScaleInCooldown":300,"ScaleOutCooldown":60}'
```

  Streams keep connections open, so `ALBRequestCountPerTarget` is a poor signal; CPU and
  memory are better.
* **Workers.** Concurrency is the number of worker tasks (one pool child, prefetch 1). Scale on the
  backlog: publish `LLEN ingestion` as a custom metric (section 13) and target-track
  "queue length per task", or add tasks manually before a bulk import. Scale-in is safe because
  jobs are leased in the database and a stopped task's job is retried.
* **Database connections.** Total connections are at most
  `(api tasks + worker tasks) x (DB_POOL_SIZE + DB_MAX_OVERFLOW)`. Raise the instance size, or
  lower the pool settings, before scaling past `max_connections`.
* **Models.** Memory per task is roughly 1.5-2 GB with both local models loaded. Models are
  read from the image or EFS, so new tasks start without downloads.
* **Redis and RDS** scale vertically; add read replicas only if you introduce read-heavy
  reporting (the application uses the primary only).

## 13. Monitoring and alarms

**Logs.** All services log JSON to stdout, which the `awslogs` driver ships to
`/ecs/eka/<service>`. Each line has `ts`, `level`, `logger`, `message` (a dotted event name)
and, when known, `request_id`, `user_id`, `org_id` and `job_id`. Document text, prompts and
answers are never logged. Useful Logs Insights queries:

```
filter message = "request.completed" and status >= 500
| fields @timestamp, route, status, duration_ms, request_id
| sort @timestamp desc

filter message = "request.completed"
| stats pct(duration_ms, 95) as p95_ms, count() as requests by route
| sort p95_ms desc
```

Health checks and `/metrics` are not logged. A metric filter with the pattern
`{ $.level = "ERROR" }` on each log group turns errors into an alarmable metric.

**Metrics.** The API serves Prometheus metrics at `/metrics` (HTTP, retrieval stages,
generation latency, tokens, estimated cost, ingestion outcomes) and requires
`Authorization: Bearer <METRICS_TOKEN>`; the worker serves them on `WORKER_METRICS_PORT`. Run
an AWS Distro for OpenTelemetry (ADOT) collector as a sidecar in the task, so it scrapes
`localhost` and no metrics port is exposed in the VPC, and send the samples to Amazon Managed
Service for Prometheus (the task role then needs `aps:RemoteWrite`) or to CloudWatch with the
`awsemf` exporter. Example collector configuration, passed through the `AOT_CONFIG_CONTENT`
variable (an unvalidated starting point):

```yaml
extensions:
  sigv4auth: { region: "${env:AWS_REGION}", service: aps }
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: eka-api
          scrape_interval: 30s
          metrics_path: /metrics
          authorization: { type: Bearer, credentials: "${env:METRICS_TOKEN}" }
          static_configs: [{ targets: ["localhost:8081"] }]
exporters:
  prometheusremotewrite:
    endpoint: "${env:AMP_REMOTE_WRITE_URL}"
    auth: { authenticator: sigv4auth }
service:
  extensions: [sigv4auth]
  pipelines:
    metrics: { receivers: [prometheus], exporters: [prometheusremotewrite] }
```

**Alarms.** Send them to an SNS topic that pages the on-call.

| Alarm | Source | Suggested condition |
|---|---|---|
| API 5xx rate | ALB `HTTPCode_Target_5XX_Count` / `RequestCount` (metric math) | above 2 percent for 5 of 5 minutes |
| No healthy targets | ALB `UnHealthyHostCount`, `HTTPCode_ELB_5XX_Count` | at least 1 for 5 minutes; any 502/503 from the ALB |
| p95 latency | ALB `TargetResponseTime`, statistic p95 (time to response headers, so a long SSE stream does not distort it) | above 3 s for 5 of 5 minutes; tune after a load test |
| Failed ingestion jobs | Prometheus `increase(eka_ingestion_jobs_total{status="failed"}[15m]) > 0` (check the label values on `/metrics`), or the `ERROR` metric filter on `/ecs/eka/worker` | any failure |
| Ingestion queue backlog | custom metric `EKA/IngestionQueueLength`: `LLEN ingestion` against the Redis primary, published every minute by a scheduled Lambda in the VPC | above what the workers drain in about 15 minutes, for 15 minutes |
| Worker or beat down | Container Insights `RunningTaskCount` below desired | 2 of 3 minutes |
| RDS CPU | `CPUUtilization` | above 80 percent for 15 minutes |
| RDS storage and memory | `FreeStorageSpace`, `FreeableMemory` | storage below 20 percent of allocated; memory below 15 percent |
| RDS connections | `DatabaseConnections` | above 80 percent of `max_connections` |
| Redis | `EngineCPUUtilization`, `DatabaseMemoryUsagePercentage`, `Evictions` | CPU or memory above 80 percent; any eviction |
| Scrape or app errors | `up == 0` for the API job; `ERROR` log metric | for 5 minutes |

Also create a synthetic check (CloudWatch Synthetics or Route 53 health check) against
`https://<your-domain>/api/v1/ready`. It returns 503 when the database is unreachable or not
migrated, Redis is down, storage is unusable or the embedding dimension differs, which the
load balancer's liveness check deliberately does not.

## 14. Backups and retention

| Data | Mechanism |
|---|---|
| PostgreSQL | automated backups with point-in-time recovery (14-35 days), a manual snapshot before each migration, AWS Backup copies to a second Region or account for disaster recovery. Test a restore regularly. Roles are part of the restore, so `eka_owner` and `eka_app` come back with it |
| S3 originals | versioning (a deleted or overwritten object is recoverable until the lifecycle rule expires it), optional cross-Region replication with a KMS key in the target Region |
| Embeddings | derived data. If vectors are lost but the documents and bucket survive, re-index (`POST /api/v1/documents/{id}/reindex`) |
| Redis | no backup needed (see section 5); take daily snapshots only if you want a warm embedding cache after a disaster |
| Secrets | Secrets Manager keeps versions; keep a sealed offline copy of `SECRET_KEY` and the database passwords in your own vault |
| Logs | CloudWatch retention per log group (90 days above); export to S3 for longer audit needs |

Application-level retention is enforced by beat: conversations (`CONVERSATION_RETENTION_DAYS`),
audit events (`AUDIT_RETENTION_DAYS`) and usage records (`USAGE_RETENTION_DAYS`). Deleted data
persists in database backups until they expire, and in S3 versions until the lifecycle rule
removes them; align both with your deletion commitments.

## 15. First deployment, release and rollback

**First deployment**, in this order:

1. VPC, subnets, route tables, security groups, VPC endpoints (section 3).
2. KMS keys, S3 bucket (section 6), RDS and ElastiCache (sections 4 and 5).
3. ECR repositories; build and push both images (section 2), including the models.
4. IAM roles and policies (section 7); secrets (section 8) once the endpoints exist.
5. Run `bootstrap.sql`, then store the database URLs in the secrets.
6. Log groups; render and register all five task definitions; run `eka-migrate` and check
   the exit code (section 4).
7. ALB, listeners and target groups (section 10); ECS cluster and the four services (section 11).
8. Create the first organization and its administrator. Sign-up is by invitation only, so
   this runs once as a one-off task from the API task definition:

   ```bash
   aws ecs run-task --cluster eka --launch-type FARGATE --task-definition eka-api \
     --network-configuration "awsvpcConfiguration={subnets=[<private-subnet-a>],securityGroups=[<api-sg>],assignPublicIp=DISABLED}" \
     --overrides '{"containerOverrides":[{"name":"api","command":["python","scripts/create_organization.py","--organization","<name>","--email","<admin email>","--name","<admin name>"],"environment":[{"name":"ADMIN_PASSWORD","value":"<temporary password>"}]}]}'
   ```

   CloudTrail records the RunTask parameters, password included, so the administrator
   changes it at first sign-in (Settings, Change password). Everyone else is invited.
9. Verify (below), then create the alarms and enable autoscaling.

**Release checklist**

1. CI is green on the commit. Build and push images tagged with the git SHA and let the
   ECR scan finish.
2. If the release contains a migration, take a snapshot:
   `aws rds create-db-snapshot --db-instance-identifier <instance> --db-snapshot-identifier eka-pre-$TAG`.
3. Render and register new revisions of all task definitions with the new images:

   ```bash
   export BACKEND_IMAGE=$REGISTRY/eka-backend:$TAG FRONTEND_IMAGE=$REGISTRY/eka-frontend:$TAG
   VARS=$(grep -rhoE --include='*.json' '\$\{[A-Z0-9_]+\}' infra/aws | sort -u | tr '\n' ' ')
   for f in api worker beat migrate frontend; do
     envsubst "$VARS" < infra/aws/ecs/$f.json > var/aws/$f.json
     aws ecs register-task-definition --cli-input-json file://var/aws/$f.json > /dev/null
   done
   ```
4. Run `eka-migrate` and require exit code 0 (section 4).
5. Update the services. Deploy workers and beat before the API when the release changes task
   names or arguments; otherwise the order does not matter.

   ```bash
   for s in worker beat api frontend; do
     aws ecs update-service --cluster eka --service eka-$s --task-definition eka-$s > /dev/null
   done
   aws ecs wait services-stable --cluster eka --services eka-worker eka-beat eka-api eka-frontend
   ```
6. Verify: `curl -fsS https://<your-domain>/api/v1/ready` returns `ready`; sign in, upload a
   document, wait for it to be indexed and ask a question; check the 5xx, latency and queue
   alarms, and the error log metric, for 15 minutes.

**Rollback**

* A deployment that fails its health checks rolls itself back (circuit breaker). To roll
  back a bad release by hand, point each service at the previous task definition revision:
  `aws ecs update-service --cluster eka --service eka-api --task-definition eka-api:<previous-revision>`.
* Migrations are not reverted automatically. If migrations keep the previous release working
  with the new schema (section 4), rolling the code back is safe. Prefer a forward fix. Never run `alembic downgrade` against production casually: downgrading the
  initial migration drops every table. If data was damaged, restore the pre-migration snapshot
  into a new instance and switch `DATABASE_URL`.
* Roll back secrets by restoring the previous version (`--version-stage AWSPREVIOUS`) and
  forcing a new deployment.

## 16. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Task stops at start with `SECRET_KEY looks like a placeholder` or `COOKIE_SECURE must be true` | The production checks of section 9; the log line is in `/ecs/eka/<service>` |
| Task cannot pull the image or read secrets (`ResourceInitializationError`) | Missing VPC endpoint or NAT route, endpoint security group, or execution role lacks `secretsmanager:GetSecretValue` / `kms:Decrypt` |
| `eka-migrate` fails with `permission denied to create extension "vector"` | `bootstrap.sql` was not run as the master user |
| `/api/v1/ready` returns 503 `migrations pending` or `embedding dimension mismatch` | Migration not run, or `EMBEDDING_DIMENSIONS` differs between runtime and database |
| `/api/v1/ready` returns 503 `object storage unavailable` | Task role lacks `s3:ListBucket`, wrong `S3_BUCKET` or `S3_REGION`, or no S3 endpoint |
| Uploads fail with `AccessDenied` | The bucket policy requires `aws:kms`; check `S3_SSE`, `S3_KMS_KEY_ID` and the key policy or `kms:GenerateDataKey` |
| Browser requests fail with 403 `origin_not_allowed` | `ALLOWED_ORIGINS` does not match the site origin exactly |
| Every client shares one rate limit, or audit logs show ALB addresses | `TRUST_PROXY_HEADERS` is false, or traffic reaches the API through the frontend proxy instead of the `/api/*` rule |
| `csrf_failed` for everyone after a deployment | `SECRET_KEY` changed; users must sign in again |
| Model errors mentioning offline mode | The models are not in the image or EFS cache (section 2) |
| Chat streams end after about 60 seconds | ALB idle timeout is still the default, or another proxy in the path times out |
| Worker idle while documents wait | Beat or worker not running, wrong `REDIS_URL` (host, auth token, `rediss://` scheme) or the `redis` security group |
