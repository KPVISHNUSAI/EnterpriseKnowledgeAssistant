import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CSRF_COOKIE } from "@/lib/api/client";
import { makeMe } from "@/test/fixtures";
import { nav, router } from "@/test/navigation";
import { errorResponse, jsonResponse, renderWithProviders } from "@/test/render";
import { LoginForm } from "../login-form";

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

async function fillAndSubmit(email: string, password: string) {
  const user = userEvent.setup();
  if (email) await user.type(screen.getByTestId("login-email"), email);
  if (password) await user.type(screen.getByTestId("login-password"), password);
  await user.click(screen.getByTestId("login-submit"));
}

describe("LoginForm", () => {
  it("validates before calling the API", async () => {
    renderWithProviders(<LoginForm />);
    await fillAndSubmit("not-an-email", "");
    expect(await screen.findByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByTestId("login-email")).toHaveAttribute("aria-invalid", "true");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("signs in, stores the identity and goes to the dashboard", async () => {
    const me = makeMe();
    fetchMock.mockResolvedValue(jsonResponse(me));
    const { client } = renderWithProviders(<LoginForm />);
    await fillAndSubmit("Ada@Acme.test", "correct horse battery");

    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/dashboard"));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/auth/login");
    expect(JSON.parse(init.body as string)).toEqual({ email: "Ada@Acme.test", password: "correct horse battery" });
    expect(client.getQueryData(["me"])).toEqual(me);
  });

  it("returns to the page the user came from, but never to another site", async () => {
    fetchMock.mockResolvedValue(jsonResponse(makeMe()));
    nav.search = new URLSearchParams({ next: "/documents?status=failed" });
    renderWithProviders(<LoginForm />);
    await fillAndSubmit("ada@acme.test", "pw");
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/documents?status=failed"));
  });

  it.each(["https://evil.example/phish", "//evil.example", "javascript:alert(1)"])(
    "ignores an unsafe return URL (%s)",
    async (next) => {
      fetchMock.mockResolvedValue(jsonResponse(makeMe()));
      nav.search = new URLSearchParams({ next });
      renderWithProviders(<LoginForm />);
      await fillAndSubmit("ada@acme.test", "pw");
      await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/dashboard"));
    },
  );

  it("shows the server's message and request id when the credentials are wrong", async () => {
    fetchMock.mockResolvedValue(
      errorResponse(401, "unauthorized", "Invalid email or password", { requestId: "req-401" }),
    );
    renderWithProviders(<LoginForm />);
    await fillAndSubmit("ada@acme.test", "wrong");
    const notice = await screen.findByTestId("login-error");
    expect(notice).toHaveTextContent("Invalid email or password");
    expect(notice).toHaveTextContent("req-401");
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("mentions Retry-After when rate limited", async () => {
    fetchMock.mockResolvedValue(
      errorResponse(429, "rate_limited", "Too many requests, slow down", { headers: { "retry-after": "45" } }),
    );
    renderWithProviders(<LoginForm />);
    await fillAndSubmit("ada@acme.test", "pw");
    expect(await screen.findByTestId("login-error")).toHaveTextContent("try again in 45 seconds");
  });

  it("skips the form when a session already exists", async () => {
    document.cookie = `${CSRF_COOKIE}=abc; path=/`;
    fetchMock.mockResolvedValue(jsonResponse(makeMe()));
    renderWithProviders(<LoginForm />);
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/dashboard"));
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/auth/me");
  });

  it("does not probe /auth/me when there is no session cookie", () => {
    renderWithProviders(<LoginForm />);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
