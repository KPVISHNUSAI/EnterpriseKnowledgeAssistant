import type { Page } from "@playwright/test";
import { newAccount, uniqueName } from "../support/accounts";
import { Api } from "../support/api";
import { expect, test } from "../support/test";
import { ask, expectIndexedInLibrary, newBrowserSession, uploadFile } from "../support/ui";
import { FRONTEND_ORIGIN } from "../stack/config";

// Every place the product shows user-controlled text gets a payload: the organization, the person,
// the workspace, the file name (which becomes the title), the document text and the question.
const ORGANIZATION = "<svg onload=window.__xss=10> Org";
const FULL_NAME = "<img src=x onerror=window.__xss=11> Admin";
const WORKSPACE = "<img src=x onerror=window.__xss=9> Space";
const FILE_NAME = "<b>bold <img src=x onerror=window.__xss=1>.md";
const TITLE = "<b>bold <img src=x onerror=window.__xss=1>";
const QUESTION = "What does the lanternfish procedure require? <img src=x onerror=window.__xss=8>";

const PAYLOAD_MD = `# Lanternfish Audit Notes

The lanternfish procedure requires badge scanning at every door, see the [lanternfish guide](javascript:window.__xss=4;alert('md-link')) before entry <img src=x onerror="window.__xss=2;alert('img')">.

<script>window.__xss=3;alert('script')</script>

Reviewers confirm the lanternfish procedure monthly <a href="javascript:window.__xss=5;alert('a-tag')">lanternfish link</a> and file the report.

<iframe src="javascript:window.__xss=6;alert('iframe')"></iframe>

<svg onload="window.__xss=7;alert('svg')"></svg>

![tracking pixel](http://tracker.invalid/pixel.png)
`;

/**
 * What a payload would have left behind: markup that can run (an element with an on* attribute, a
 * javascript: URL, a script or frame inside the page content, any image) or a variable it set.
 * The product itself renders none of these.
 */
const injectedMarkup = (page: Page) =>
  page.evaluate(() => {
    const found: string[] = [];
    for (const element of document.querySelectorAll("*")) {
      const handlers = element.getAttributeNames().filter((name) => name.startsWith("on"));
      if (handlers.length) found.push(`<${element.localName}> has ${handlers.join(", ")}`);
    }
    for (const element of document.querySelectorAll("a[href], iframe[src]")) {
      const url = element.getAttribute("href") ?? element.getAttribute("src") ?? "";
      if (/^\s*(javascript|data|vbscript):/i.test(url)) found.push(`<${element.localName}> points to ${url}`);
    }
    for (const element of document.querySelectorAll(
      "main script, main iframe, main img, main object, main embed, main form[action^='javascript' i]",
    )) {
      found.push(`<${element.localName}> inside the page content`);
    }
    found.push(...Object.keys(window).filter((key) => key.startsWith("__xss")));
    return found;
  });

test("HTML and script payloads in names, titles, documents and questions stay inert everywhere", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const api = new Api(context);
  const dialogs: string[] = [];
  const foreignRequests: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(`${dialog.type()}: ${dialog.message()}`);
    void dialog.dismiss();
  });
  page.on("request", (request) => {
    const { protocol, origin } = new URL(request.url());
    if (protocol.startsWith("http") && origin !== FRONTEND_ORIGIN) foreignRequests.push(request.url());
  });
  const expectInert = async (where: string) => expect(await injectedMarkup(page), where).toEqual([]);

  const account = { ...newAccount(), fullName: FULL_NAME, organization: ORGANIZATION };
  await api.register(account);
  await api.createWorkspace(WORKSPACE);

  await test.step("the check itself would notice injected markup", async () => {
    await page.goto("/dashboard");
    await expect(page.getByTestId("org-switcher")).toBeVisible();
    await page.evaluate(() =>
      document.body.insertAdjacentHTML("beforeend", '<img src=x onerror="window.__xss_control=1">'),
    );
    await page.waitForFunction(() => "__xss_control" in window); // the failed image load runs the handler
    expect(await injectedMarkup(page)).toEqual(expect.arrayContaining(["<img> has onerror", "__xss_control"]));
    await page.reload(); // the control is gone again
    await expect(page.getByTestId("org-switcher")).toBeVisible();
    await expectInert("after reload");
  });

  await test.step("names are shown literally in the shell, the dashboard and the workspace list", async () => {
    await expect(page.getByTestId("org-switcher")).toContainText(ORGANIZATION);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Welcome back, <img");
    await expectInert("dashboard");
    await page.getByTestId("user-menu").click();
    await expect(page.getByRole("menu")).toContainText(FULL_NAME);
    await expectInert("user menu");
    await page.keyboard.press("Escape");
    await page.goto("/workspaces");
    await expect(page.getByTestId("workspace-list")).toContainText(WORKSPACE);
    await expectInert("workspaces");
  });

  let documentId = "";
  await test.step("an upload with a hostile file name", async () => {
    documentId = await uploadFile(
      page,
      { name: FILE_NAME, mimeType: "text/markdown", buffer: Buffer.from(PAYLOAD_MD) },
      { workspace: WORKSPACE },
    );
    await expect(page.getByTestId("upload-item")).toContainText(FILE_NAME);
    await expectInert("upload list");
    await expectIndexedInLibrary(page, TITLE);
    await expect(page.getByRole("link", { name: TITLE, exact: true })).toBeVisible();
    await expectInert("library");
  });

  await test.step("the detail page shows the title, the file name and the text as plain text", async () => {
    await page.goto(`/documents/${documentId}`);
    await expect(page.getByTestId("document-title")).toHaveText(TITLE);
    await expect(page.getByTestId("document-metadata")).toContainText(FILE_NAME);
    const text = page.getByTestId("extracted-text");
    await expect(text).toContainText("<script>window.__xss=3;alert('script')</script>");
    await expect(text).toContainText("<svg onload=");
    await expect(text).toContainText("[lanternfish guide](javascript:window.__xss=4");
    await expectInert("document detail");
    await page.getByRole("tab", { name: "Sharing" }).click();
    await expectInert("sharing tab");
    expect(await page.title()).toContain(TITLE);
  });

  await test.step("search results show the passage literally, with only <mark> added", async () => {
    await page.goto("/search");
    await page.getByTestId("search-input").fill("lanternfish procedure");
    await page.getByTestId("search-submit").click();
    const result = page.getByTestId("search-result").first();
    await expect(result).toContainText(TITLE);
    await expect(result).toContainText(WORKSPACE);
    await expect(result.getByTestId("search-snippet")).toContainText("<script>window.__xss=3");
    await expect(result.getByTestId("search-snippet").locator("mark").first()).toBeVisible();
    await expectInert("search results");
    await result.getByTestId("why-matched-toggle").click();
    await expectInert("search explanation");
  });

  await test.step("chat renders the answer as sanitized Markdown and the question as text", async () => {
    await page.goto("/chat");
    const answer = await ask(page, QUESTION);
    await expect(page.getByTestId("user-message")).toHaveText(QUESTION);
    // The sentence comes back with the Markdown link's label; its javascript: target is gone.
    await expect(answer.getByTestId("assistant-answer")).toContainText("lanternfish procedure requires badge scanning");
    await expect(answer.getByTestId("assistant-answer")).toContainText("lanternfish guide");
    await expect(answer.getByTestId("citation-chip").first()).toBeVisible();
    // The Markdown link keeps its label but lost its javascript: target; raw HTML is dropped.
    const guide = answer.getByTestId("assistant-answer").getByText("lanternfish guide");
    await expect(guide).not.toHaveAttribute("href", /./);
    await guide.click();
    await expectInert("chat answer");
    await answer.getByTestId("citation-chip").first().click();
    await expect(page.getByTestId("source-panel")).toContainText(TITLE);
    await expectInert("source panel");
    await expect(page.getByTestId("conversation-list")).toContainText("What does the lanternfish procedure require?");
    await expectInert("conversation list");
  });

  await test.step("the audit log shows the title in its details as plain text", async () => {
    await page.goto("/admin?tab=audit");
    await page.getByRole("button", { name: "Show details for document.uploaded" }).click();
    await expect(page.getByRole("row").filter({ hasText: "Metadata" })).toContainText(TITLE.replace(/"/g, '\\"'));
    await expectInert("audit log");
    await page.goto("/settings");
    await expect(page.getByTestId("profile-name")).toHaveValue(FULL_NAME);
    await expectInert("settings");
  });

  await test.step("the inline copy of the file is served as plain text", async () => {
    const response = await page.goto(`/api/v1/documents/${documentId}/download?inline=true`);
    expect(response?.headers()["content-type"]).toMatch(/^text\/plain/);
    expect(response?.headers()["x-content-type-options"]).toBe("nosniff");
    expect(response?.headers()["content-security-policy"]).toContain("default-src 'none'");
    await expect(page.locator("body")).toContainText("<script>window.__xss=3;alert('script')</script>");
    await expectInert("raw download");
  });

  expect(dialogs, "no alert, confirm or prompt may open").toEqual([]);
  expect(foreignRequests, "nothing may be fetched from other hosts (tracking pixels)").toEqual([]);
  await context.close();
});

test("state-changing requests need this session's CSRF token and an allowed origin", async ({ org, browser }) => {
  const request = org.context.request;
  const token = (await org.context.cookies()).find((c) => c.name === "eka_csrf")!.value;
  const createWorkspace = (headers: Record<string, string>) =>
    request.post("/api/v1/workspaces", { data: { name: uniqueName("CSRF") }, headers });

  expect((await createWorkspace({})).status(), "no token").toBe(403);
  expect((await createWorkspace({ "X-CSRF-Token": "0".repeat(64) })).status(), "wrong token").toBe(403);

  const otherSession = await newBrowserSession(browser);
  await otherSession.api.register(newAccount());
  const foreignToken = (await otherSession.context.cookies()).find((c) => c.name === "eka_csrf")!.value;
  expect((await createWorkspace({ "X-CSRF-Token": foreignToken })).status(), "another session's token").toBe(403);
  await otherSession.context.close();

  const crossSite = await createWorkspace({ "X-CSRF-Token": token, Origin: "https://evil.example" });
  expect(crossSite.status(), "cross-site origin").toBe(403);
  expect((await crossSite.json()).error.code).toBe("origin_not_allowed");

  // The same request from the app's own origin works, and none of the refused ones created anything.
  expect((await createWorkspace({ "X-CSRF-Token": token, Origin: FRONTEND_ORIGIN })).status()).toBe(201);
  const workspaces = await org.api.get<Array<{ name: string }>>("/workspaces");
  expect(workspaces.filter((w) => w.name.startsWith("CSRF "))).toHaveLength(1);

  // Reading needs no token, and a cross-site preflight is not granted CORS access.
  expect((await request.get("/api/v1/auth/me")).status()).toBe(200);
  const preflight = await request.fetch("/api/v1/workspaces", {
    method: "OPTIONS",
    headers: { Origin: "https://evil.example", "Access-Control-Request-Method": "POST" },
  });
  expect(preflight.headers()["access-control-allow-origin"]).toBeUndefined();
});

test("pages and API responses carry the security headers", async ({ org }) => {
  const page = (await org.context.request.get("/documents")).headers();
  const csp = page["content-security-policy"] ?? "";
  for (const directive of [
    "default-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
  ]) {
    expect(csp, "page CSP").toContain(directive);
  }
  expect(page["x-content-type-options"]).toBe("nosniff");
  expect(page["x-frame-options"]).toBe("SAMEORIGIN");
  expect(page["referrer-policy"]).toBe("strict-origin-when-cross-origin");
  expect(page["permissions-policy"]).toContain("camera=()");
  expect(page["x-powered-by"]).toBeUndefined();

  const api = (await org.context.request.get("/api/v1/auth/me")).headers();
  expect(api["cache-control"]).toBe("no-store");
  expect(api["x-content-type-options"]).toBe("nosniff");
  expect(api["content-security-policy"]).toContain("default-src 'none'");
  expect(api["cross-origin-resource-policy"]).toBe("same-origin");
  expect(api["x-request-id"]).toMatch(/^[0-9a-f]{32}$/);
});
