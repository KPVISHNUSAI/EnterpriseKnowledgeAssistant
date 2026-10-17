/** The API error envelope: {"error": {"code", "message", "details", "request_id"}}. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly details: unknown = null,
    readonly requestId: string | null = null,
    /** Seconds from the Retry-After header (429 responses). */
    readonly retryAfter: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function isAbortError(err: unknown): boolean {
  return typeof err === "object" && err !== null && (err as { name?: unknown }).name === "AbortError";
}

export function networkError(): ApiError {
  return new ApiError(0, "network_error", "Cannot reach the server. Check your connection and try again.");
}

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.round(seconds));
  const date = Date.parse(value);
  return Number.isNaN(date) ? null : Math.max(0, Math.ceil((date - Date.now()) / 1000));
}

/** Build an ApiError from a failed response (shared by the fetch and XHR code paths). */
export function apiErrorFrom(status: number, bodyText: string, header: (name: string) => string | null): ApiError {
  let envelope: { code?: unknown; message?: unknown; details?: unknown; request_id?: unknown } = {};
  try {
    const parsed: unknown = JSON.parse(bodyText);
    const inner = (parsed as { error?: unknown } | null)?.error;
    if (inner && typeof inner === "object") envelope = inner;
  } catch {
    // Not JSON (for example an HTML error page from a proxy): fall back to the status.
  }
  const code = typeof envelope.code === "string" ? envelope.code : `http_${status}`;
  const message =
    typeof envelope.message === "string" && envelope.message
      ? envelope.message
      : `The server returned an unexpected response (HTTP ${status}).`;
  const requestId = typeof envelope.request_id === "string" ? envelope.request_id : header("x-request-id");
  return new ApiError(
    status,
    code,
    message,
    envelope.details ?? null,
    requestId,
    parseRetryAfter(header("retry-after")),
  );
}

/** A message that is safe and useful to show to the user. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) {
      return err.retryAfter
        ? `Too many requests. Please try again in ${err.retryAfter} second${err.retryAfter === 1 ? "" : "s"}.`
        : "Too many requests. Please wait a moment and try again.";
    }
    if (err.status === 403 && err.code === "csrf_failed") {
      return "Your session security token is missing or has expired. Reload the page and try again.";
    }
    if (err.status === 403) return err.message || "You do not have permission to do this.";
    if (err.status === 413) return "That file is larger than the upload limit.";
    return err.message;
  }
  if (isAbortError(err)) return "The request was cancelled.";
  if (err instanceof Error && err.message) return err.message;
  return "Something went wrong. Please try again.";
}

export function errorRequestId(err: unknown): string | null {
  return err instanceof ApiError ? err.requestId : null;
}

/** Map a 422 response to {field: message} (last path element of each error's `loc`). */
export function fieldErrors(err: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!(err instanceof ApiError) || err.status !== 422 || !Array.isArray(err.details)) return out;
  for (const item of err.details as Array<{ loc?: unknown; msg?: unknown }>) {
    const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : undefined;
    if (typeof field === "string" && typeof item.msg === "string" && !(field in out)) {
      out[field] = item.msg;
    }
  }
  return out;
}
