# Enterprise Knowledge Assistant: web UI

Next.js (App Router) + React + TypeScript (strict) + Tailwind CSS 4. It talks only to the versioned REST API
under `/api/v1`, through a same-origin proxy, so the browser never needs CORS and the session cookies stay
first-party.

## Quick start

Requires Node.js 22.22+ or 24 LTS (`engines` in `package.json`) and npm.

```bash
cd frontend
npm ci
cp .env.example .env.local      # optional: API_INTERNAL_URL
npm run dev                      # http://localhost:3000 (the API is expected on http://localhost:8081)
# another port: PORT=3001 npm run dev
```

The backend must allow the browser origin: put the frontend's public origin (for example
`http://localhost:3000`) in the API's `ALLOWED_ORIGINS`, otherwise state-changing requests are rejected with
`origin_not_allowed`.

## Scripts

| Script                                     | What it does                                                                        |
| ------------------------------------------ | ----------------------------------------------------------------------------------- |
| `npm run dev` / `build` / `start`          | Next.js development server, production build (standalone output), production server |
| `npm run lint`                             | ESLint (Next.js core-web-vitals + TypeScript + React hooks/compiler rules)          |
| `npm run typecheck`                        | `tsc --noEmit` (strict)                                                             |
| `npm test` / `npm run test:watch`          | Vitest + Testing Library (jsdom)                                                    |
| `npm run format` / `format:check`          | Prettier                                                                            |
| `npm run gen:api`                          | Export the backend OpenAPI document **and** regenerate the TypeScript types         |
| `npm run gen:api:export` / `gen:api:types` | The two halves separately (export needs `uv` and the backend environment)           |

## Environment variables

| Variable           | Default                 | Used by                                                                                                          |
| ------------------ | ----------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `API_INTERNAL_URL` | `http://localhost:8081` | Target of the `/api/*` rewrite. Dev server and `next build` read it; the Docker image re-applies it at start-up  |
| `PORT`             | `3000`                  | `npm run dev`, `npm start`, the standalone server and the container (process environment only, not `.env` files) |
| `HOSTNAME`         | `0.0.0.0` (container)   | Interface the standalone server binds to                                                                         |

There are no `NEXT_PUBLIC_*` variables: nothing environment-specific is compiled into the browser bundle.

## How it talks to the API

- **Generated types are the source of truth.** `src/lib/api/openapi.json` (exported from the backend) and
  `src/lib/api/schema.d.ts` (generated with `openapi-typescript`) are committed. After a backend change run
  `npm run gen:api` and fix any type errors. `src/lib/api/types.ts` only gives the generated types friendly names.
  The SSE event payloads (`Stream*Event`) are not part of the OpenAPI document, so they are mirrored by hand in
  `types.ts` from `backend/app/schemas/chat.py`.
- **Client** (`src/lib/api/client.ts`, `endpoints.ts`): one typed function per endpoint, every failure is an
  `ApiError` carrying `status`, `code`, `details`, `requestId` and `Retry-After`. 401 redirects to
  `/login?next=<current page>` (only same-origin paths are accepted as `next`); 403, 429 and network errors have
  dedicated messages; the server's request id is always shown with errors.
- **CSRF**: every POST/PUT/PATCH/DELETE (fetch, upload and chat stream) sends `X-CSRF-Token` with the value of the
  readable `eka_csrf` cookie. The session cookie is HttpOnly and never touched by JavaScript.
- **Uploads** use `XMLHttpRequest` so the progress bar is real (`src/lib/api/upload.ts`).
- **Chat streaming** (`src/lib/api/chat-stream.ts`, `src/lib/sse.ts`): `fetch` + `ReadableStream` + a small
  spec-compliant SSE parser (chunk boundaries, CRLF, comments such as `: keep-alive`, multi-line data). Stop uses
  `AbortController`. If the response is not an event stream the UI falls back to `POST /chat`.
- **Data fetching** is TanStack Query; lists poll every 3 s while a document is queued or processing.

## Rendering untrusted text

Answers are rendered with `react-markdown` (GFM tables, highlighted code with copy buttons). Raw HTML is never
enabled, images are removed, and the output also goes through `rehype-sanitize` (`src/lib/markdown.ts`);
nothing uses `dangerouslySetInnerHTML`. `[S1]` markers become citation chips via a remark plugin
(`src/lib/citations.ts`). Search highlights, extracted text and audit metadata are rendered as plain React text.
Security headers and a Content-Security-Policy are set in `next.config.ts`.

## Proxy and deployment notes

- **Rewrite destinations are baked in at build time.** `next.config.ts` reads `API_INTERNAL_URL`, but Next.js
  stores the result in the build output, so changing the variable later has no effect on `next start` or the
  standalone server. The Docker image therefore runs `docker-entrypoint.mjs`, which rewrites the baked destination
  from the container's `API_INTERNAL_URL` before starting the server (whatever value the image was built with,
  including the optional `API_INTERNAL_URL` build argument, is replaced). Verified: one image, different upstreams,
  restarts, and an invalid URL is refused at start-up.
- **Streaming**: the chat stream passes through the rewrite proxy unbuffered because the API sends
  `Cache-Control: no-cache, no-transform` (Next.js otherwise gzips proxied responses, which buffers the whole stream
  until it ends: verified with a test server). Anything between the browser and the API (nginx, ALB, CloudFront)
  must keep that header and not buffer `/api/v1/chat/stream`.
- **Client IP**: the rewrite proxy does not append the client address to `X-Forwarded-For` (it passes through
  whatever header the client sent). Behind it the API therefore sees the frontend container's address for every
  user, so IP-based rate limits share one bucket, and the API must keep `TRUST_PROXY_HEADERS=false` or clients could
  spoof their address. In production prefer routing `/api/*` straight to the API at the load balancer (same origin,
  real client IPs, `TRUST_PROXY_HEADERS=true`) and use this proxy for local development and simple deployments.
- **Health check**: `GET /healthz` returns `{"status":"ok"}` without calling the API.
- **Caching**: pages are statically prerendered shells (no user data) and carry `Cache-Control: s-maxage=...`;
  invalidate the CDN on deploy.
- Security headers apply to pages only; `/api/*` responses keep the headers the API sets.

## Docker

```bash
docker build -t eka-frontend frontend
docker run -p 3000:3000 -e API_INTERNAL_URL=http://api:8081 eka-frontend
```

Multi-stage (`npm ci` -> `next build` -> `node:24-alpine` runtime), standalone output, runs as the non-root user
`app` (uid 1001), `HEALTHCHECK` on `/healthz`, about 300 MB.

## Tests

`npm test` runs ~150 unit and component tests: SSE parser, citations, CSRF header injection, error mapping,
Markdown sanitization (script, `onerror`, `javascript:`/`data:` URLs, images), upload and chat-stream clients, the
chat stream hook, login, upload panel, extracted-text viewer, source panel and more. End-to-end tests can rely on
the `data-testid` attributes below.

| Area      | Test ids                                                                                                                                                                                                                                                                                         |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Auth      | `login-email`, `login-password`, `login-submit`, `login-error`, `register-name`, `register-email`, `register-password`, `register-org`, `register-submit`                                                                                                                                        |
| Shell     | `nav-<dashboard\|documents\|chat\|search\|workspaces\|admin\|settings>`, `org-switcher`, `org-option`, `user-menu`, `sign-out`, `toast-<success\|error\|info>`                                                                                                                                   |
| Documents | `upload-input`, `upload-dropzone`, `upload-workspace`, `upload-visibility`, `upload-item` (`data-status`), `documents-table`, `document-row`, `document-status` (`data-status`), `document-search`, `document-actions`, `reindex`, `pdf-preview`, `extracted-text`, `chunk` (`data-highlighted`) |
| Chat      | `chat-input`, `chat-send`, `chat-stop`, `assistant-answer`, `assistant-message` (`data-status`), `user-message`, `citation-chip` (`data-source-id`, `data-state`), `source-panel`, `source-item`, `answer-banner` (`data-status`), `scope-selector`, `new-chat`, `retry-answer`                  |
| Search    | `search-input`, `search-submit`, `search-results`, `search-result`, `search-snippet`, `why-matched-toggle`, `why-matched`                                                                                                                                                                        |
| Admin     | `admin-tab-<usage\|audit\|members\|invitations>`, `invite-open`, `invite-email`, `invite-role`, `invite-submit`, `invite-link`                                                                                                                                                                   |

## Toolchain notes

- TypeScript is pinned to 5.9: `openapi-typescript` 7 requires TypeScript 5, and `typescript-eslint` does not yet
  support TypeScript 7 (which also no longer ships the JavaScript compiler API that Next.js uses).
- ESLint 10 is used with `overrides` in `package.json` for three Next.js lint plugins whose peer ranges stop at
  ESLint 9 (`eslint-config-next` 16.4 supports ESLint 10).
- `tailwind-merge` lets components accept a `className` override that really wins (`cn("w-full", "w-32")`).
- `npm audit` reports advisories in the dev-only lint toolchain (`braces` via `eslint-config-next`); production
  dependencies (`npm audit --omit=dev`) are clean.
