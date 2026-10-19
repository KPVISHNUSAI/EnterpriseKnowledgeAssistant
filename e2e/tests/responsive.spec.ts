import type { Locator, Page } from "@playwright/test";
import { expect, test } from "../support/test";

test.use({ viewport: { width: 390, height: 844 } }); // a phone

const LONG_NAME = "Quarterly planning and budget review for the regional operations teams";

/** How far the page is wider than the screen (anything above 0 means horizontal scrolling). */
const overflow = (page: Page) =>
  page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

test("every main page fits a phone screen, even with very long names", async ({ org }) => {
  const { page } = org;
  // Long names are what stretch a layout: a workspace, a document title and a conversation title.
  const workspace = await org.api.createWorkspace(`${LONG_NAME} ${LONG_NAME}`);
  const document = await org.api.upload({
    workspaceId: workspace.id,
    name: `${LONG_NAME}.md`,
    mimeType: "text/markdown",
    buffer: Buffer.from(
      `${LONG_NAME}.\n\nEmployees may work remotely up to three days per week with manager approval.\n`,
    ),
  });
  await org.api.waitUntilIndexed(document.id);
  const conversation = await org.api.post<{ conversation_id: string }>("/chat", {
    message: `${LONG_NAME}: how many days per week may employees work remotely?`,
  });

  // Each page and something that is only on screen once its data has arrived.
  const pages: Array<[string, (page: Page) => Locator]> = [
    ["/dashboard", (p) => p.getByTestId("dashboard-stats")],
    ["/documents", (p) => p.getByTestId("documents-table")],
    [`/documents/${document.id}`, (p) => p.getByTestId("document-title")],
    ["/search?q=remote", (p) => p.getByTestId("search-result").first()],
    ["/workspaces", (p) => p.getByTestId("workspace-list")],
    [`/workspaces/${workspace.id}`, (p) => p.getByTestId("members-table")],
    ["/admin?tab=usage", (p) => p.getByTestId("usage-totals")],
    ["/admin?tab=audit", (p) => p.getByTestId("audit-table")],
    ["/admin?tab=members", (p) => p.getByTestId("org-members-table")],
    ["/admin?tab=invitations", (p) => p.getByTestId("invite-open")],
    ["/settings", (p) => p.getByTestId("profile-name")],
    ["/chat", (p) => p.getByTestId("chat-input")],
    [`/chat/${conversation.conversation_id}`, (p) => p.getByTestId("assistant-message")],
  ];
  for (const [path, ready] of pages) {
    await page.goto(path);
    await expect(ready(page), path).toBeVisible();
    expect(await overflow(page), `${path} is wider than the screen`).toBeLessThanOrEqual(0);
  }

  await test.step("the sources open in a drawer that fits as well", async () => {
    await page.getByTestId("citation-chip").first().click();
    await expect(page.getByTestId("source-panel")).toBeVisible();
    expect(await overflow(page)).toBeLessThanOrEqual(0);
  });
});

test("the navigation is a drawer on a phone", async ({ org }) => {
  const { page } = org;
  await page.goto("/dashboard");
  await expect(page.getByTestId("nav-documents")).toBeHidden(); // the sidebar is not shown
  await page.getByRole("button", { name: "Open navigation" }).click();
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer.getByTestId("nav-admin")).toBeVisible();
  await drawer.getByTestId("nav-documents").click();
  await expect(page).toHaveURL(/\/documents$/);
  await expect(drawer).toBeHidden();
});
