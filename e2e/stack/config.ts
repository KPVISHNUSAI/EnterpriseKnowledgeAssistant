import { resolve } from "node:path";

/**
 * Everything the launcher and the tests agree on. Ports and the infrastructure endpoints can be
 * overridden with environment variables (the defaults match docker-compose and .env.example);
 * the database name, the Redis DB and the secrets are fixed so a run can never touch real data.
 */

export const E2E_ROOT = resolve(__dirname, "..");
export const REPO_ROOT = resolve(E2E_ROOT, "..");
export const STACK_DIR = resolve(E2E_ROOT, ".stack"); // logs and the copied frontend (git-ignored)

const port = (name: string, fallback: number) => Number(process.env[name] ?? fallback);

export const API_PORT = port("E2E_API_PORT", 8093);
export const FRONTEND_PORT = port("E2E_FRONTEND_PORT", 3093);
export const METRICS_PORT = port("E2E_METRICS_PORT", 9193);

// 127.0.0.1 everywhere: "localhost" may resolve to ::1 where nothing listens.
export const API_ORIGIN = `http://127.0.0.1:${API_PORT}`;
export const FRONTEND_ORIGIN = `http://127.0.0.1:${FRONTEND_PORT}`;

/** Delay before each word group of an answer; see stack/e2e_app.py. */
export const STREAM_DELAY_MS = 200;

const DATABASE = "eka_e2e";
const REDIS_DB = 12;
const postgresPort = process.env.EKA_POSTGRES_PORT ?? "5436";
const redisPort = process.env.EKA_REDIS_PORT ?? "6381";

/** Superuser connection used once per run to create roles/database and reset the schema. */
export const ADMIN_DATABASE_URL =
  process.env.E2E_ADMIN_DATABASE_URL ??
  `postgresql://postgres:postgres_dev_password@localhost:${postgresPort}/postgres`;

/**
 * Environment of the API, the worker and the provisioning and migration commands. Set
 * explicitly for every variable that matters, because the application also reads the
 * repository's .env file for anything left unset.
 */
export function stackEnv(storagePath: string): NodeJS.ProcessEnv {
  return {
    ...process.env,
    APP_ENV: "development",
    LOG_LEVEL: "INFO",
    LOG_FORMAT: "json",
    SECRET_KEY: "e2e-only-secret-key-0123456789abcdef0123456789abcdef",
    PUBLIC_BASE_URL: FRONTEND_ORIGIN,
    ALLOWED_ORIGINS: FRONTEND_ORIGIN,
    COOKIE_SECURE: "false",
    RATE_LIMIT_ENABLED: "false",
    ALLOW_PUBLIC_REGISTRATION: "true", // the tests sign up their own organizations
    DATABASE_URL: `postgresql+psycopg://eka_app:eka_app_dev_password@localhost:${postgresPort}/${DATABASE}`,
    MIGRATION_DATABASE_URL: `postgresql+psycopg://eka_owner:eka_owner_dev_password@localhost:${postgresPort}/${DATABASE}`,
    DB_APP_ROLE: "eka_app",
    E2E_ADMIN_DATABASE_URL: ADMIN_DATABASE_URL,
    REDIS_URL: `redis://localhost:${redisPort}/${REDIS_DB}`,
    STORAGE_BACKEND: "local",
    LOCAL_STORAGE_PATH: storagePath,
    EMBEDDING_PROVIDER: "hashing",
    EMBEDDING_DIMENSIONS: "384",
    RERANKER_PROVIDER: "none",
    RERANK_ENABLED: "false",
    LLM_PROVIDER: "extractive",
    METRICS_ENABLED: "true",
    WORKER_METRICS_PORT: String(METRICS_PORT),
    E2E_STREAM_DELAY_MS: String(STREAM_DELAY_MS),
    PYTHONPATH: resolve(E2E_ROOT, "stack"),
  };
}
