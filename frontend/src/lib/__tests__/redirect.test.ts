import { describe, expect, it } from "vitest";
import { loginUrl, safeNext } from "../redirect";

describe("safeNext", () => {
  it("keeps same-origin paths with query and hash", () => {
    expect(safeNext("/documents/abc?page=4#x")).toBe("/documents/abc?page=4#x");
  });

  it.each([
    "https://evil.example/",
    "//evil.example/path",
    "/\\evil.example",
    "javascript:alert(1)",
    "documents",
    "/login",
    "/register?x=1",
    "/ok\nSet-Cookie: x=1",
    "",
    null,
    undefined,
  ])("falls back for %j", (value) => {
    expect(safeNext(value)).toBe("/dashboard");
  });

  it("builds a login URL that preserves the return path", () => {
    expect(loginUrl("/chat/1?x=y")).toBe("/login?next=%2Fchat%2F1%3Fx%3Dy");
  });
});
