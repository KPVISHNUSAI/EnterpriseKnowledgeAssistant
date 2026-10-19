import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import {
  closeSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readdirSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LOG_CANARIES } from "../support/canaries";
import {
  API_ORIGIN,
  API_PORT,
  E2E_ROOT,
  FRONTEND_ORIGIN,
  FRONTEND_PORT,
  METRICS_PORT,
  REPO_ROOT,
  STACK_DIR,
  stackEnv,
} from "./config";

/**
 * Playwright globalSetup: brings up the real stack from source (PostgreSQL and Redis come from
 * docker compose or CI services) and returns the function that tears it down again.
 *
 *   provision (reset database + Redis DB)  ->  migrate  ->  worker + API
 *   frontend build (in parallel with the above)  ->  standalone server
 *
 * Every process runs in its own process group, so stopping a service also stops what `uv run`
 * and `next` spawned below it. Output goes to e2e/.stack/logs/<service>.log.
 */

interface Service {
  name: string;
  child: ChildProcess;
  log: string;
}

const LOG_DIR = join(STACK_DIR, "logs");
const BACKEND = join(REPO_ROOT, "backend");
const FRONTEND = join(REPO_ROOT, "frontend");
const services: Service[] = [];
let storageDir: string | undefined;
let stopping: Promise<void> | undefined;

const say = (message: string) => console.log(`[e2e] ${message}`);

function tail(file: string, lines = 40): string {
  try {
    return readFileSync(file, "utf8").trimEnd().split("\n").slice(-lines).join("\n");
  } catch {
    return "(no log)";
  }
}

function launch(name: string, command: string, args: string[], cwd: string, env: NodeJS.ProcessEnv): Service {
  const log = join(LOG_DIR, `${name}.log`);
  const fd = openSync(log, "w");
  const child = spawn(command, args, { cwd, env, detached: true, stdio: ["ignore", fd, fd] });
  closeSync(fd);
  child.on("error", (error) => console.error(`[e2e] ${name}: ${error.message}`));
  const service = { name, child, log };
  services.push(service);
  return service;
}

const hasExited = ({ child }: Service) => child.exitCode !== null || child.signalCode !== null;

function exited(service: Service): Promise<void> {
  return new Promise((resolve) => {
    if (hasExited(service)) return resolve();
    service.child.once("exit", () => resolve());
    service.child.once("error", () => resolve());
  });
}

/** A command that must finish successfully. */
async function run(name: string, command: string, args: string[], cwd: string, env: NodeJS.ProcessEnv) {
  const service = launch(name, command, args, cwd, env);
  await exited(service);
  if (service.child.exitCode !== 0) {
    throw new Error(
      `${name} failed (exit ${service.child.exitCode}). Last lines of ${service.log}:\n${tail(service.log)}`,
    );
  }
}

async function waitForUrl(service: Service, url: string, timeoutMs = 300_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (hasExited(service)) {
      throw new Error(
        `${service.name} exited before ${url} answered. Last lines of ${service.log}:\n${tail(service.log)}`,
      );
    }
    try {
      if ((await fetch(url, { signal: AbortSignal.timeout(2_000) })).ok) return;
    } catch {
      // not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(
    `${service.name} did not answer ${url} within ${timeoutMs / 1000} s. Last lines of ${service.log}:\n${tail(service.log)}`,
  );
}

const uv = (...args: string[]) => ["run", "--directory", BACKEND, "--frozen", ...args];

// Both servers close an idle connection after 5 s by default, which is also how long Node keeps an
// idle proxy connection. A request that reuses a pooled connection at that very moment fails with
// ECONNRESET (the proxy answers 500). The side that is called must outlast the side that calls.
const KEEP_ALIVE_SECONDS = 75;
const KEEP_ALIVE_ARGS = ["--timeout-keep-alive", String(KEEP_ALIVE_SECONDS)];

/** The Next.js standalone output, copied out of frontend/.next so a later build there cannot disturb it. */
function installFrontend(): string {
  const target = join(STACK_DIR, "frontend");
  rmSync(target, { recursive: true, force: true });
  cpSync(join(FRONTEND, ".next/standalone"), target, { recursive: true });
  cpSync(join(FRONTEND, ".next/static"), join(target, ".next/static"), { recursive: true });
  cpSync(join(FRONTEND, "public"), join(target, "public"), { recursive: true });
  return target;
}

function groupAlive(pid: number | undefined): boolean {
  if (pid === undefined) return false;
  try {
    process.kill(-pid, 0);
    return true;
  } catch {
    return false;
  }
}

function signalGroup(service: Service, signal: NodeJS.Signals): void {
  if (!groupAlive(service.child.pid)) return;
  try {
    process.kill(-service.child.pid!, signal);
  } catch {
    // already gone
  }
}

async function waitForGroupExit(service: Service, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (groupAlive(service.child.pid)) {
    if (Date.now() > deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return true;
}

/** SIGTERM to every process group (SIGCONT first: a paused worker would never see it), SIGKILL after 10 s. */
function stopAll(): Promise<void> {
  stopping ??= (async () => {
    const running = [...services].reverse();
    for (const service of running) {
      signalGroup(service, "SIGCONT");
      signalGroup(service, "SIGTERM");
    }
    for (const service of running) {
      if (!(await waitForGroupExit(service, 10_000))) {
        say(`${service.name} ignored SIGTERM, killing it`);
        signalGroup(service, "SIGKILL");
        await waitForGroupExit(service, 3_000);
      }
    }
    if (storageDir) rmSync(storageDir, { recursive: true, force: true });
    rmSync(join(STACK_DIR, "frontend"), { recursive: true, force: true });
    say("stack stopped");
  })();
  return stopping;
}

// Last resort when the runner dies without calling the teardown (Ctrl+C during the start-up): the
// processes started with `detached` would otherwise outlive it.
process.on("exit", () => {
  for (const service of services) signalGroup(service, "SIGKILL");
  if (storageDir) rmSync(storageDir, { recursive: true, force: true });
  rmSync(join(STACK_DIR, "frontend"), { recursive: true, force: true });
});

/** The services must not log passwords, secrets, document text or questions: fail the run if the tests' canaries show up. */
function assertLogsAreClean(): void {
  const leaks = readdirSync(LOG_DIR)
    .filter((file) => file !== "build.log")
    .flatMap((file) => {
      const text = readFileSync(join(LOG_DIR, file), "utf8");
      return LOG_CANARIES.filter((canary) => text.includes(canary)).map(
        (canary) => `${file} contains "${canary.slice(0, 40)}"`,
      );
    });
  if (leaks.length > 0) throw new Error(`Sensitive text was written to the service logs:\n${leaks.join("\n")}`);
  say(`logs checked for ${LOG_CANARIES.length} sensitive strings: none found`);
}

/** python3 scripts/check_ports.py is a bind test, so sockets of a previous run (TIME_WAIT, ~30 s) count as busy: wait for them. */
async function waitForFreePorts(): Promise<void> {
  const ports = [API_PORT, FRONTEND_PORT, METRICS_PORT].map(String);
  for (let attempt = 0; ; attempt++) {
    const result = spawnSync("python3", [join(REPO_ROOT, "scripts/check_ports.py"), ...ports], { encoding: "utf8" });
    if (result.status === 0) return;
    if (attempt === 30) {
      throw new Error(
        `The e2e ports are not free (is a previous run still active?). Change them with E2E_API_PORT, E2E_FRONTEND_PORT and E2E_METRICS_PORT.\n${result.stdout}${result.stderr}`,
      );
    }
    if (attempt === 0) say("waiting for the ports to be released");
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }
}

export default async function globalSetup(): Promise<() => Promise<void>> {
  const started = Date.now();
  try {
    if (!existsSync(join(FRONTEND, "node_modules"))) {
      throw new Error("frontend/node_modules is missing: run `npm ci` in frontend/ first");
    }
    await waitForFreePorts();
    rmSync(LOG_DIR, { recursive: true, force: true });
    mkdirSync(LOG_DIR, { recursive: true });
    storageDir = mkdtempSync(join(tmpdir(), "eka-e2e-storage-"));
    const env = stackEnv(storageDir);

    // The rewrite destination of /api/* is compiled into the build, so it needs the API address.
    say(`building the frontend (API_INTERNAL_URL=${API_ORIGIN})`);
    const building = run("build", "npm", ["run", "build"], FRONTEND, {
      ...process.env,
      API_INTERNAL_URL: API_ORIGIN,
      NEXT_TELEMETRY_DISABLED: "1",
      NODE_ENV: "production",
    });
    building.catch(() => undefined); // a failure is reported where it is awaited, or by whatever failed first

    say("resetting the database and Redis, running migrations");
    await run("provision", "uv", uv("python", join(E2E_ROOT, "stack/provision.py")), REPO_ROOT, env);
    await run("migrate", "uv", uv("alembic", "upgrade", "head"), REPO_ROOT, env);

    say("starting the worker and the API");
    const worker = launch(
      "worker",
      "uv",
      uv(
        "celery",
        "-A",
        "app.worker.celery_app",
        "worker",
        "-Q",
        "ingestion,maintenance",
        "--pool",
        "solo",
        "-l",
        "INFO",
      ),
      REPO_ROOT,
      env,
    );
    const api = launch(
      "api",
      "uv",
      uv(
        "uvicorn",
        "e2e_app:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        String(API_PORT),
        ...KEEP_ALIVE_ARGS,
      ),
      REPO_ROOT,
      env,
    );
    await Promise.all([
      waitForUrl(worker, `http://127.0.0.1:${METRICS_PORT}/metrics`),
      waitForUrl(api, `${API_ORIGIN}/api/v1/ready`),
      building,
    ]);
    // The worker's process group is what tests suspend to keep a document "queued".
    process.env.E2E_WORKER_PID = String(worker.child.pid);

    say("starting the frontend");
    const frontend = launch("frontend", process.execPath, ["server.js"], installFrontend(), {
      ...process.env,
      NODE_ENV: "production",
      PORT: String(FRONTEND_PORT),
      HOSTNAME: "127.0.0.1",
      KEEP_ALIVE_TIMEOUT: String(KEEP_ALIVE_SECONDS * 1000),
      NEXT_TELEMETRY_DISABLED: "1",
    });
    await waitForUrl(frontend, `${FRONTEND_ORIGIN}/healthz`);
    await waitForUrl(frontend, `${FRONTEND_ORIGIN}/api/v1/ready`); // through the proxy to the API
    say(`stack ready in ${Math.round((Date.now() - started) / 1000)} s: ${FRONTEND_ORIGIN} (API ${API_ORIGIN})`);
  } catch (error) {
    await stopAll();
    throw error;
  }
  return async () => {
    await stopAll();
    assertLogsAreClean();
  };
}
