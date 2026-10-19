import { expect, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { Api, LONG_TIMEOUT } from "./api";
import type { SampleFile } from "./documents";

/** A second signed-out browser (its own cookies), for tests that need two people at once. */
export async function newBrowserSession(browser: Browser): Promise<{ context: BrowserContext; page: Page; api: Api }> {
  const context = await browser.newContext();
  return { context, page: await context.newPage(), api: new Api(context) };
}

/** A success toast with this text (several can be on screen at once). */
export const toast = (page: Page, text: string) => page.getByTestId("toast-success").filter({ hasText: text });

/** Creates a workspace through the form and returns its id. */
export async function createWorkspace(page: Page, name: string): Promise<string> {
  await page.goto("/workspaces");
  await page.getByTestId("new-workspace").click();
  await page.getByTestId("workspace-name").fill(name);
  await page.getByTestId("workspace-save").click();
  await expect(toast(page, `Workspace “${name}” created`)).toBeVisible(); // lasts 5 s: look before anything else
  await expect(page.getByTestId("workspace-title")).toHaveText(name);
  return page.url().split("/").pop()!;
}

/** Uploads one file through the documents page and returns the new document's id. */
export async function uploadFile(
  page: Page,
  file: SampleFile,
  { workspace, restricted = false }: { workspace?: string; restricted?: boolean } = {},
): Promise<string> {
  await page.goto("/documents");
  if (workspace) await page.getByTestId("upload-workspace").selectOption({ label: workspace });
  await page
    .getByTestId("upload-visibility")
    .getByLabel(restricted ? "Only people I share with" : "Workspace members")
    .check();
  await page.getByTestId("upload-input").setInputFiles(file);
  const upload = page.getByTestId("upload-item").filter({ hasText: file.name });
  await expect(upload).toHaveAttribute("data-status", "done");
  return (await upload.getByRole("link", { name: "Open" }).getAttribute("href"))!.split("/").pop()!;
}

/** Waits (the table polls while anything is queued or processing) until the document's row says Indexed. */
export async function expectIndexedInLibrary(page: Page, title: string) {
  const row = page.getByTestId("document-row").filter({ hasText: title });
  await expect(row.getByTestId("document-status")).toHaveAttribute("data-status", "indexed", {
    timeout: LONG_TIMEOUT,
  });
}

/**
 * Creates an invitation as an organization admin and returns the one-time link, copied with the
 * dialog's "Copy link" button (the link is shown only once, so this is how an admin gets it).
 */
export async function invite(page: Page, email: string): Promise<string> {
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/admin?tab=invitations");
  await page.getByTestId("invite-open").click();
  await page.getByTestId("invite-email").fill(email);
  await page.getByTestId("invite-submit").click();
  const link = await page.getByTestId("invite-link").inputValue();
  await page.getByRole("button", { name: "Copy link" }).click();
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(link);
  await page.getByRole("button", { name: "Done" }).click();
  return link;
}

/** Adds an organization member to a workspace from the workspace page. */
export async function addWorkspaceMember(
  page: Page,
  workspaceId: string,
  person: { fullName: string; email: string },
  role: "Viewer" | "Editor" | "Owner" = "Viewer",
) {
  await page.goto(`/workspaces/${workspaceId}`);
  await page.getByTestId("pick-member").click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: new RegExp(person.email) })
    .click();
  await page.getByLabel("Role for the new member").selectOption({ label: role });
  await page.getByTestId("add-member").click();
  await expect(page.getByTestId("member-row").filter({ hasText: person.email })).toBeVisible();
}

/** Sends a chat message and waits until its answer is complete; returns the answer. */
export async function ask(page: Page, message: string) {
  const answers = page.getByTestId("assistant-message");
  const before = await answers.count();
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("chat-send").click();
  await expect(answers).toHaveCount(before + 1);
  const answer = answers.last();
  await expect(answer).toHaveAttribute("data-status", "complete");
  return answer;
}

export async function search(page: Page, query: string) {
  await page.goto("/search");
  await page.getByTestId("search-input").fill(query);
  await page.getByTestId("search-submit").click();
  await expect(page.getByTestId("search-summary").or(page.getByText(`No results for “${query}”`))).toBeVisible();
}
