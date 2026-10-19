# End-to-end tests

Playwright (Chromium) tests that drive the product in a real browser against a real stack started from source: the
Next.js frontend (production build), the FastAPI API, the Celery ingestion worker, PostgreSQL with pgvector and Redis.
Nothing in the product is mocked; the one timing seam is described under [Streaming and Stop](#streaming-and-stop).

## Run

You need the development infrastructure (`make infra-up`: PostgreSQL on `localhost:5436`, Redis on `localhost:6381`),
`uv`, Node.js 22.22+ or 24, and the dependencies of the three projects:

```bash
(cd backend && uv sync)            # dev group only: the stack uses the hashing embedder, no model download
(cd frontend && npm ci)            # the launcher builds the frontend itself
cd e2e && npm ci && npx playwright install chromium
npm test                           # about two minutes on an idle machine, including the start-up
```

| Command                                  | What it does                                   |
| ---------------------------------------- | ---------------------------------------------- |
| `npm test`                               | Everything, headless, two workers              |
| `npx playwright test tests/chat.spec.ts` | One file (add `-g "follow-up"` to pick a test) |
| `npm run test:headed`                    | Watch the browser                              |
| `npx playwright test --repeat-each=3`    | Stress test for flakiness                      |
| `npm run report`                         | Open the last HTML report                      |
| `npm run typecheck`                      | `tsc --noEmit` over the tests and the launcher |

Everything that is started is stopped again when the run ends, also after a failure or Ctrl+C. The ports are checked
with `scripts/check_ports.py` first (it is a bind test, so a run waits up to a minute for the sockets of a previous
run to drain). macOS and Linux only: the launcher uses process groups and signals.

## What is covered

| File                          | Scenario                                                                                                                                                                                                                                                                                            |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tests/auth.spec.ts`          | Register an organization, sign out (the session is revoked server-side), wrong password, sign in and return to the requested page; weak password, duplicate email; password change ends other sessions; profile name; the return address cannot leave the site                                      |
| `tests/documents.spec.ts`     | Upload TXT, Markdown and a 3-page PDF (queued, then indexed), sorting and filtering, detail pages (metadata, extracted text, PDF preview, download), re-index, replace (version 2), delete with confirmation, the row menu; refused uploads and duplicates; large files; a file that fails to parse |
| `tests/search.spec.ts`        | Highlighted terms, "why this matched", file type and workspace filters, opening a result (at the page for PDFs)                                                                                                                                                                                     |
| `tests/chat.spec.ts`          | Cited answer, source panel, source to document, conversation list, follow-up in the same conversation, "not enough evidence" banner, deleting conversations                                                                                                                                         |
| `tests/permissions.spec.ts`   | A restricted document is invisible to an invited employee in the library, dashboard, search, chat (answer and sources), direct URL and crafted ids; workspace role, personal share and removal take effect immediately                                                                              |
| `tests/organizations.spec.ts` | A person in two organizations sees only the active one; another organization's admin gets 404 for every id and finds nothing                                                                                                                                                                        |
| `tests/admin.spec.ts`         | Audit log lists upload, delete, invitation, ... with action/resource/actor/time filters; usage totals; a non-admin gets "Administrators only" and 403s                                                                                                                                              |
| `tests/workspaces.spec.ts`    | Create, rename, duplicate names, delete (refused while it holds documents)                                                                                                                                                                                                                          |
| `tests/security.spec.ts`      | HTML/JS payloads in the organization, person, workspace, file name, document text and question stay inert in every view; CSRF token and origin checks; security headers                                                                                                                             |
| `tests/streaming.spec.ts`     | Tokens reach the browser one by one through the Next.js proxy; Stop cancels the generation and the partial answer is stored                                                                                                                                                                         |
| `tests/responsive.spec.ts`    | Every main page fits a 390 px phone screen with very long names; the navigation is a drawer                                                                                                                                                                                                         |

Not covered: DOCX uploads (the backend parser tests do), real LLM and embedding providers, S3 storage, rate limiting
(switched off here), other browsers, the visual rendering of the PDF inside the preview frame (headless Chromium has no
PDF viewer; the test checks the frame's address and fetches the file it points to).

## How the stack is started

`stack/launcher.ts` is the Playwright `globalSetup`; the function it returns is the teardown.

1. `stack/provision.py` (run with the backend environment) creates the `eka_owner`/`eka_app` roles and the database
   **`eka_e2e`** when missing, resets its schema and flushes **Redis DB 12**. It refuses any database whose name does
   not start with `eka_e2e` and Redis DB 0, so it can never touch the development data.
2. `alembic upgrade head` as `eka_owner`; the API connects as `eka_app`, so row-level security is active.
3. The Celery worker (`--pool solo`, metrics on 9193), the API through `stack/e2e_app.py` on 8093 and, built in parallel
   with all of that, the frontend with `API_INTERNAL_URL=http://127.0.0.1:8093` (the proxy target is compiled into the
   build). The Next.js standalone output, the artifact the Docker image runs, is copied to `e2e/.stack/frontend` and
   started on 3093, so a later `next build` in `frontend/` cannot disturb a run.
4. Both servers run with a 75 s keep-alive timeout (`--timeout-keep-alive 75` for uvicorn, `KEEP_ALIVE_TIMEOUT` for the
   Next.js server). With their 5 s defaults a request that reuses a pooled connection just as the server closes it
   fails with `ECONNRESET`, and the Next.js proxy turns that into a plain `500` (seen as "Could not load your account").
   The server that is called has to outlast the side that calls, which is also what an ALB in front of them needs.
5. Providers: hashing embeddings (384 dimensions), no reranker, extractive answers, rate limiting off,
   `APP_ENV=development`. Uploaded files go to a temporary directory.

Every variable is set explicitly (the application would otherwise also read the repository's `.env`); see
`stack/config.ts`. Every process runs in its own process group and logs to `e2e/.stack/logs/<service>.log`.

| Variable                                                | Default                                             | Meaning                                                |
| ------------------------------------------------------- | --------------------------------------------------- | ------------------------------------------------------ |
| `E2E_API_PORT`, `E2E_FRONTEND_PORT`, `E2E_METRICS_PORT` | 8093, 3093, 9193                                    | Host ports of the API, the frontend, the worker        |
| `EKA_POSTGRES_PORT`, `EKA_REDIS_PORT`                   | 5436, 6381                                          | Same variables as docker-compose and the backend tests |
| `E2E_ADMIN_DATABASE_URL`                                | `postgresql://postgres:...@localhost:5436/postgres` | Superuser URL used once per run to provision           |

## How the tests are written

- **Independent.** Every test registers its own organization (the `org` fixture in `support/test.ts`) with a unique
  email, so tests can run in any order and in parallel. The two workers share one API, one ingestion worker and one
  database.
- **UI first.** The tests drive the interface. The JSON API (`support/api.ts`) is used only for setup that another test
  already covers through the UI (registering, uploading, inviting, creating a workspace) and to read stored state back.
  The permission tests do the setup through the UI once and use the API for the repeated variations.
- **Locators** are roles, labels and the `data-testid` attributes listed in `frontend/README.md`. There are no fixed
  sleeps: waiting is `expect` retrying, and `expect.poll` for ingestion and the API. The time limits are generous
  (`playwright.config.ts`): a busy laptop is several times slower than CI, and a slow run should still pass.
- **Positive controls.** Every "cannot see" assertion has a counterpart that proves the data was there for someone who
  may see it (the admin finds the memo the employee cannot; the XSS check detects an element injected on purpose).
- Traces, screenshots and the HTML report are kept for failures (`test-results/`, `playwright-report/`).

### Streaming and Stop

The extractive answer generator produces its whole text in microseconds, so the answer would reach the browser in a
single piece and Stop would never find anything to interrupt. `stack/e2e_app.py` therefore wraps the generator so that it
waits 200 ms before each word group it yields. Nothing else changes: retrieval, the status line, citation validation,
the SSE bridge, cancellation and persistence are the product's own. The streaming test timestamps the events inside
the browser to show that they arrive spread over time through the Next.js proxy (which only holds because the API sends
`Cache-Control: no-transform`), and checks that Stop ends the stream and that the server stores the partial answer as
`incomplete`.

### Queued documents

The ingestion worker finishes small files in milliseconds, so "queued" would be gone before a test could look at it.
`documents.spec.ts` suspends the worker's process group (`SIGSTOP`, `support/worker.ts`) while it uploads, asserts that
all three documents are _Queued_, and resumes it (`SIGCONT`). Other tests running in parallel only wait a few seconds
longer. _Processing_ is too short-lived to observe deterministically and is not asserted.

### Logs are checked, too

After the services are stopped the launcher searches their logs for the passwords, the secret key, the database
passwords, sentences of the uploaded documents and the questions the tests asked (`support/canaries.ts`). Finding any of
them fails the run: the services must not log document text, prompts, answers, passwords or keys.

## CI

The `e2e` job in `.github/workflows/ci.yml` uses the same service containers as the backend job, installs Python (uv),
the frontend and the test dependencies, type-checks the tests, installs Chromium with its system packages and runs
`npm test`. On failure it uploads the Playwright report, the traces and the service logs. CI retries a failed test once
and reports it as flaky.

## Troubleshooting

- _The e2e ports are not free_: another run (or process) holds 8093/3093/9193. Find it with
  `lsof -nP -iTCP:3093 -sTCP:LISTEN`, or choose other ports with the variables above.
- _`provision: refusing ...`_: a safety check; the database name and the Redis DB are fixed in `stack/config.ts`.
- A failed start prints the tail of the failing service's log; the complete logs are in `e2e/.stack/logs/`.
- `npx playwright show-trace test-results/<test>/trace.zip` replays a failed test step by step.
- _frontend/node_modules is missing_: run `npm ci` in `frontend/`.
