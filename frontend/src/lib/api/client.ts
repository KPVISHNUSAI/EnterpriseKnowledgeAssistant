import { ApiError, apiErrorFrom, isAbortError, networkError } from "./errors";

export const API_PREFIX = "/api/v1";
// Backend defaults (CSRF_COOKIE_NAME / CSRF_HEADER_NAME in backend/app/core/config.py).
export const CSRF_COOKIE = "eka_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue | readonly QueryValue[]>;

export function readCookie(name: string, cookies?: string): string | null {
  const source = cookies ?? (typeof document === "undefined" ? "" : document.cookie);
  for (const part of source.split(";")) {
    const index = part.indexOf("=");
    if (index > 0 && part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return null;
}

/** Double-submit CSRF: unsafe requests echo the readable eka_csrf cookie in a header. */
export function csrfHeaders(method: string): Record<string, string> {
  if (SAFE_METHODS.has(method.toUpperCase())) return {};
  const token = readCookie(CSRF_COOKIE);
  return token ? { [CSRF_HEADER]: token } : {};
}

export function buildQuery(query: QueryParams = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    const values = Array.isArray(value) ? (value as QueryValue[]) : [value as QueryValue];
    for (const item of values) {
      if (item !== undefined && item !== null && item !== "") params.append(key, String(item));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function apiUrl(path: string, query?: QueryParams): string {
  return `${API_PREFIX}${path}${buildQuery(query)}`;
}

export interface RequestOptions {
  query?: QueryParams;
  json?: unknown;
  form?: FormData;
  signal?: AbortSignal;
}

export async function errorFromResponse(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => "");
  return apiErrorFrom(res.status, text, (name) => res.headers.get(name));
}

/** Typed JSON request. Failed responses throw ApiError; 204 resolves to undefined. */
export async function request<T>(method: HttpMethod, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...csrfHeaders(method) };
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) {
    body = options.form; // the browser sets the multipart boundary
  }
  let res: Response;
  try {
    res = await fetch(apiUrl(path, options.query), {
      method,
      headers,
      body,
      signal: options.signal,
      credentials: "same-origin",
    });
  } catch (err) {
    throw isAbortError(err) ? err : networkError();
  }
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
