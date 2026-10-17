import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CSRF_COOKIE, CSRF_HEADER, buildQuery, csrfHeaders, readCookie, request } from "../api/client";
import { api } from "../api/endpoints";
import { ApiError, apiErrorFrom, errorMessage, fieldErrors } from "../api/errors";

const fetchMock = vi.fn();

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
    ...init,
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("CSRF header injection", () => {
  it("reads the csrf cookie among others", () => {
    document.cookie = "other=1; path=/";
    document.cookie = `${CSRF_COOKIE}=abc123; path=/`;
    expect(readCookie(CSRF_COOKIE)).toBe("abc123");
    expect(readCookie("missing")).toBeNull();
  });

  it("does not confuse cookies with a similar suffix", () => {
    expect(readCookie("eka_csrf", "x_eka_csrf=bad; eka_csrf=good")).toBe("good");
  });

  it.each(["POST", "PUT", "PATCH", "DELETE"])("adds X-CSRF-Token on %s", async (method) => {
    document.cookie = `${CSRF_COOKIE}=token-1; path=/`;
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await request(method as "POST", "/anything");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)[CSRF_HEADER]).toBe("token-1");
  });

  it("does not send it on GET", async () => {
    document.cookie = `${CSRF_COOKIE}=token-1; path=/`;
    fetchMock.mockResolvedValue(jsonResponse({}));
    await request("GET", "/auth/me");
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.headers).not.toHaveProperty(CSRF_HEADER);
    expect(csrfHeaders("get")).toEqual({});
  });

  it("omits the header when there is no cookie yet (login, register)", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await api.auth.login({ email: "a@b.co", password: "secret" });
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.headers).not.toHaveProperty(CSRF_HEADER);
  });

  it("sends JSON bodies with a content type", async () => {
    document.cookie = `${CSRF_COOKIE}=t; path=/`;
    fetchMock.mockResolvedValue(jsonResponse({}));
    await api.auth.switchOrganization("org-1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/switch-organization");
    expect(init.body).toBe(JSON.stringify({ organization_id: "org-1" }));
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });
});

describe("buildQuery", () => {
  it("repeats array values and skips empty ones", () => {
    expect(buildQuery({ q: "", page: 2, workspace_id: ["a", "b"], status: undefined, flag: false })).toBe(
      "?page=2&workspace_id=a&workspace_id=b&flag=false",
    );
    expect(buildQuery({})).toBe("");
  });

  it("encodes path parameters", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    await api.auth.previewInvitation("a/b?c");
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/auth/invitations/a%2Fb%3Fc");
  });
});

describe("error handling", () => {
  it("turns the error envelope into an ApiError with the request id", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(
        { error: { code: "not_found", message: "Not found", details: null, request_id: "req-1" } },
        { status: 404 },
      ),
    );
    const err = await request("GET", "/documents/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 404, code: "not_found", message: "Not found", requestId: "req-1" });
  });

  it("falls back to the X-Request-ID header and a generic message for non-JSON errors", () => {
    const err = apiErrorFrom(502, "<html>Bad gateway</html>", (name) => (name === "x-request-id" ? "req-9" : null));
    expect(err).toMatchObject({ status: 502, code: "http_502", requestId: "req-9" });
    expect(err.message).toContain("502");
  });

  it("mentions Retry-After for 429", () => {
    const err = apiErrorFrom(
      429,
      JSON.stringify({ error: { code: "rate_limited", message: "Too many requests, slow down" } }),
      (name) => (name === "retry-after" ? "30" : null),
    );
    expect(err.retryAfter).toBe(30);
    expect(errorMessage(err)).toBe("Too many requests. Please try again in 30 seconds.");
  });

  it("shows a permission message for 403 and explains CSRF failures", () => {
    expect(errorMessage(new ApiError(403, "forbidden", "You do not have permission to do this"))).toBe(
      "You do not have permission to do this",
    );
    expect(errorMessage(new ApiError(403, "csrf_failed", "Missing or invalid CSRF token"))).toMatch(/Reload the page/);
  });

  it("reports unreachable servers as a network error", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const err = await request("GET", "/auth/me").catch((e: unknown) => e);
    expect(err).toMatchObject({ status: 0, code: "network_error" });
  });

  it("lets aborts through untouched", async () => {
    const abort = new DOMException("aborted", "AbortError");
    fetchMock.mockRejectedValue(abort);
    await expect(request("GET", "/auth/me")).rejects.toBe(abort);
  });

  it("maps 422 details to field errors", () => {
    const err = new ApiError(422, "validation_error", "Request validation failed", [
      { loc: ["body", "password"], msg: "String should have at least 12 characters", type: "string_too_short" },
      { loc: ["body", "email"], msg: "Invalid email", type: "value_error" },
    ]);
    expect(fieldErrors(err)).toEqual({
      password: "String should have at least 12 characters",
      email: "Invalid email",
    });
  });
});
