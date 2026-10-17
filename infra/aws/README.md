# AWS deployment templates

Templates for the architecture in [docs/DEPLOYMENT_AWS.md](../../docs/DEPLOYMENT_AWS.md):
ECS Fargate services behind an ALB, RDS PostgreSQL 17 with pgvector, ElastiCache Redis and
an S3 bucket encrypted with KMS. They hold placeholders only: no account ids, ARNs,
hostnames or credentials. Read the guide first; this file only explains how to fill them in.

## Layout

| File | Purpose |
|---|---|
| `ecs/api.json` | API service: uvicorn, one process per task, port 8081 |
| `ecs/worker.json` | Celery worker, queues `ingestion` and `maintenance`, solo pool |
| `ecs/beat.json` | Celery beat scheduler (run exactly one task) |
| `ecs/migrate.json` | One-off task: `alembic upgrade head` as the schema-owner role |
| `ecs/frontend.json` | Next.js frontend, port 3000 |
| `iam/ecs-tasks-trust-policy.json` | Trust policy for the execution and task roles |
| `iam/execution-role-policy.json` | Execution role: read the runtime secret (api, worker, beat, frontend) |
| `iam/execution-role-migrate-policy.json` | Execution role of the migrate task: also reads the owner-role secret |
| `iam/task-role-policy.json` | Task roles (api and worker): S3 objects, `s3:ListBucket`, KMS through S3 |
| `iam/task-role-bedrock-policy.json` | Optional, only with `ANTHROPIC_PLATFORM=bedrock` |
| `s3/bucket-policy.json` | Bucket policy: deny non-TLS requests and non-KMS encryption headers |
| `s3/lifecycle.json` | Bucket lifecycle: expire old versions, abort unfinished multipart uploads |
| `rds/bootstrap.sql` | One-time roles (`eka_owner`, `eka_app`), database and `vector` extension |

## Placeholders

Every placeholder is written as `${NAME}`, so `envsubst` can fill the files in. Export the
variables, then render with an explicit variable list. Without the list `envsubst` would
also replace any other `$` in the files.

| Variable | Value |
|---|---|
| `AWS_REGION`, `AWS_ACCOUNT_ID` | Target region and account |
| `BACKEND_IMAGE`, `FRONTEND_IMAGE` | Full image references, preferably an immutable tag or digest (`<registry>/eka-backend:<git-sha>`) |
| `CPU_ARCHITECTURE` | `X86_64` or `ARM64`; must match the architecture the images were built for |
| `EXECUTION_ROLE_ARN` | Execution role of api, worker, beat and frontend |
| `MIGRATE_EXECUTION_ROLE_ARN` | Execution role of the migrate task |
| `API_TASK_ROLE_ARN`, `WORKER_TASK_ROLE_ARN` | Task roles of api and worker (same S3/KMS policy; only the API role may get the Bedrock policy) |
| `APP_SECRET_ARN` | Secrets Manager secret with the JSON keys `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `METRICS_TOKEN`, `ANTHROPIC_API_KEY` |
| `MIGRATION_SECRET_ARN` | Secret with the JSON key `MIGRATION_DATABASE_URL` (owner-role credentials) |
| `SECRETS_KMS_KEY_ARN` | Customer-managed key that encrypts those secrets. Drop the `kms:Decrypt` statement from the execution policies if they use the default `aws/secretsmanager` key |
| `BUCKET_NAME`, `KMS_KEY_ARN` | Document bucket and the KMS key used for its objects (full key ARN) |
| `S3_PREFIX` | Key prefix inside the bucket, with a trailing slash (e.g. `eka/`), or empty for a dedicated bucket (`export S3_PREFIX=`). It is the application's `S3_PREFIX` setting and limits the task roles to that prefix |
| `PUBLIC_BASE_URL` | Public origin of the site, e.g. `https://<your-domain>` (no trailing slash) |
| `VPC_CIDR` | CIDR of the VPC; the only addresses whose `X-Forwarded-*` headers uvicorn trusts |
| `BEDROCK_MODEL_ID`, `BEDROCK_INFERENCE_PROFILE_ID` | Only for the Bedrock policy |

```bash
# bash. Export every variable of the table first (the two Bedrock ones only if you use Bedrock).
export AWS_REGION=... AWS_ACCOUNT_ID=... BACKEND_IMAGE=...

NAMES=$(grep -rhoE --include='*.json' '\$\{[A-Z0-9_]+\}' infra/aws | tr -d '${}' | sort -u)
VARS=$(printf '${%s} ' $NAMES)
for n in $NAMES; do [ -n "${!n+x}" ] || echo "not set: $n"; done

mkdir -p var/aws
for f in $(find infra/aws -name '*.json'); do
  envsubst "$VARS" < "$f" > "var/aws/$(basename "$f")"
done

jq empty var/aws/*.json         # JSON syntax
aws ecs register-task-definition --cli-input-json file://var/aws/api.json
```

`envsubst` replaces an unset variable with an empty string without complaint, which is why
the loop above reports unset names. `var/` is git-ignored; keep it that way, the rendered files contain
your ARNs. File names are unique across the folders, so they render side by side. The
policies are applied the same way, for example:

```bash
aws iam put-role-policy --role-name eka-task-api --policy-name eka-task \
  --policy-document file://var/aws/task-role-policy.json
```

## What to adjust

* Resources (`cpu`, `memory`) are starting points: 2 vCPU / 4 GiB for api and worker (both
  load the embedding model), small values for the rest. Tune them after a load test.
  Fargate only accepts certain cpu/memory pairs.
* `api.json` and `worker.json` assume `LLM_PROVIDER=anthropic`, the embedding and reranker
  models baked into the image, and `HF_HUB_OFFLINE=1`. For Bedrock, remove the
  `ANTHROPIC_API_KEY` secret and add `ANTHROPIC_PLATFORM=bedrock`. Other settings are in the
  configuration table of the guide.
* Log groups `/ecs/eka/<service>` must exist before the first task starts; the guide creates
  them with a retention period.
* All ARNs use the `aws` partition. In `aws-us-gov` or `aws-cn`, change the prefix.
* The migration reads `EMBEDDING_DIMENSIONS`, `FTS_CONFIG` and `DB_APP_ROLE`. If you
  override any of them for api and worker, set the same values in `migrate.json`.
