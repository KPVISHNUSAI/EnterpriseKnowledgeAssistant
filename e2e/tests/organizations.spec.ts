import type { Page } from "@playwright/test";
import { expect, test } from "../support/test";
import { newAccount } from "../support/accounts";
import { engineeringHandbook, remoteWorkPolicy } from "../support/documents";
import { newBrowserSession, search, toast } from "../support/ui";

const QUESTION = "How many days per week may employees work remotely?";

/** The titles the signed-in person's library lists. */
async function library(page: Page): Promise<string[]> {
  await page.goto("/documents");
  await expect(
    page.getByTestId("documents-table").or(page.getByText("No documents yet")),
  ).toBeVisible();
  return page.getByTestId("document-row").getByRole("link").allTextContents();
}

test("a person in two organizations sees only the active one, and switching changes everything", async ({
  org,
  browser,
}) => {
  const ada = org.account;
  const { page } = org;

  // Organization one (Ada's own): a document and a conversation about it.
  const remote = await org.api.upload({
    workspaceId: org.workspaceId,
    ...remoteWorkPolicy,
  });
  await org.api.waitUntilIndexed(remote.id);
  const conversation = await org.api.post<{ conversation_id: string }>(
    "/chat",
    { message: QUESTION },
  );

  // Organization two (Bea's): its own document; Bea invites Ada.
  const bea = await newBrowserSession(browser);
  const beaAccount = newAccount();
  const beaMe = await bea.api.register(beaAccount);
  const handbook = await bea.api.upload({
    workspaceId: beaMe.workspaces[0]!.id,
    ...engineeringHandbook,
  });
  await bea.api.waitUntilIndexed(handbook.id);
  const invitation = await bea.api.invite(ada.email);

  await test.step("an existing user accepts the invitation while signed in", async () => {
    await page.goto(invitation.invite_url);
    await expect(
      page.getByRole("heading", { name: `Join ${beaAccount.organization}` }),
    ).toBeVisible();
    await expect(page.getByText("You were invited as Employee")).toBeVisible();
    await page.getByRole("button", { name: "Accept invitation" }).click();
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByTestId("org-switcher")).toContainText(
      beaAccount.organization,
    );
    await bea.api.addWorkspaceMember(
      beaMe.workspaces[0]!.id,
      org.me.user.id,
      "viewer",
    );
  });

  await test.step("the switcher lists both organizations with the role in each", async () => {
    await page.getByTestId("org-switcher").click();
    const options = page.getByTestId("org-option");
    await expect(options).toHaveCount(2);
    await expect(options.filter({ hasText: ada.organization })).toContainText(
      "Organization admin",
    );
    await expect(
      options.filter({ hasText: beaAccount.organization }),
    ).toContainText("Employee");
    await expect(
      options
        .filter({ hasText: beaAccount.organization })
        .getByLabel("Current organization"),
    ).toBeVisible();
    await page.keyboard.press("Escape");
  });

  await test.step("in the second organization nothing of the first is visible", async () => {
    expect(await library(page)).toEqual(["Engineering Handbook"]);
    await search(page, "probation");
    await expect(
      page
        .getByTestId("search-result")
        .filter({ hasText: "Remote Work Policy" }),
    ).toHaveCount(0);
    await page.goto("/chat");
    await expect(page.getByText("No conversations yet.")).toBeVisible();
    for (const path of [
      `/documents/${remote.id}`,
      `/documents/${remote.id}/chunks`,
      `/conversations/${conversation.conversation_id}/messages`,
    ]) {
      expect((await org.api.getRaw(path)).status(), path).toBe(404);
    }
    await page.goto(`/documents/${remote.id}`);
    await expect(
      page.getByRole("heading", { name: "Document not found" }),
    ).toBeVisible();
  });

  await test.step("switching back restores the first organization, and hides the second", async () => {
    await page.getByTestId("org-switcher").click();
    await page
      .getByTestId("org-option")
      .filter({ hasText: ada.organization })
      .click();
    await expect(toast(page, `Switched to ${ada.organization}`)).toBeVisible();
    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByTestId("org-switcher")).toContainText(
      ada.organization,
    );

    expect(await library(page)).toEqual(["Remote Work Policy"]);
    await page.goto(`/chat/${conversation.conversation_id}`);
    await expect(page.getByTestId("user-message")).toHaveText(QUESTION);
    expect((await org.api.getRaw(`/documents/${handbook.id}`)).status()).toBe(
      404,
    );
  });
});

test("an admin of another organization cannot reach anything by id", async ({
  org,
  browser,
}) => {
  // Organization A: a workspace with a document, a conversation and a pending invitation.
  const document = await org.api.upload({
    workspaceId: org.workspaceId,
    ...remoteWorkPolicy,
  });
  await org.api.waitUntilIndexed(document.id);
  const conversation = await org.api.post<{ conversation_id: string }>(
    "/chat",
    { message: QUESTION },
  );
  const invitation = await org.api.invite(newAccount("employee").email);

  // Organization B's admin, who is an admin in the same product but not in A.
  const other = await newBrowserSession(browser);
  await other.api.register(newAccount());

  await test.step("the API answers 404 for every id of the other organization", async () => {
    const notFound: Array<["GET" | "POST" | "DELETE", string]> = [
      ["GET", `/documents/${document.id}`],
      ["GET", `/documents/${document.id}/status`],
      ["GET", `/documents/${document.id}/chunks`],
      ["GET", `/documents/${document.id}/download`],
      ["DELETE", `/documents/${document.id}`],
      ["POST", `/documents/${document.id}/reindex`],
      ["GET", `/workspaces/${org.workspaceId}`],
      ["GET", `/workspaces/${org.workspaceId}/members`],
      ["DELETE", `/workspaces/${org.workspaceId}`],
      ["GET", `/conversations/${conversation.conversation_id}/messages`],
      ["DELETE", `/conversations/${conversation.conversation_id}`],
      ["DELETE", `/organization/invitations/${invitation.id}`],
    ];
    for (const [method, path] of notFound) {
      expect(
        (await other.api.send(method, path)).status(),
        `${method} ${path}`,
      ).toBe(404);
    }
    // Uploading into the other organization's workspace, as an admin of one's own.
    const upload = await other.context.request.post(
      "/api/v1/documents/upload",
      {
        headers: {
          "X-CSRF-Token": (await other.context.cookies()).find(
            (c) => c.name === "eka_csrf",
          )!.value,
        },
        multipart: {
          workspace_id: org.workspaceId,
          file: {
            name: "x.txt",
            mimeType: "text/plain",
            buffer: Buffer.from("hello there my friend"),
          },
        },
      },
    );
    expect(upload.status()).toBe(404);
    const chat = await other.api.send("POST", "/chat", {
      message: QUESTION,
      conversation_id: conversation.conversation_id,
    });
    expect(chat.status()).toBe(404);
  });

  await test.step("directories and logs only contain the admin's own organization", async () => {
    const members = await other.api.get<{ items: Array<{ email: string }> }>(
      "/organization/members",
    );
    expect(members.items.map((m) => m.email)).not.toContain(org.account.email);
    const audit = await other.api.get<{
      items: Array<{ actor_email: string | null }>;
    }>("/admin/audit-events?page_size=100");
    expect(audit.items.map((e) => e.actor_email)).not.toContain(
      org.account.email,
    );
  });

  await test.step("crafted scopes and searches find nothing", async () => {
    const answer = await other.api.post<{
      assistant_message: { answer_status: string; sources: unknown[] };
    }>("/chat", {
      message: QUESTION,
      scope: { workspace_ids: [org.workspaceId], document_ids: [document.id] },
    });
    expect(answer.assistant_message.answer_status).toBe(
      "insufficient_evidence",
    );
    expect(answer.assistant_message.sources).toHaveLength(0);
    const found = await other.api.post<{ results: unknown[] }>("/search", {
      query: "remote work approval",
      filters: {
        workspace_ids: [org.workspaceId],
        document_ids: [document.id],
      },
    });
    expect(found.results).toHaveLength(0);
  });

  await test.step("the pages show not found", async () => {
    const { page } = other;
    await page.goto(`/documents/${document.id}`);
    await expect(
      page.getByRole("heading", { name: "Document not found" }),
    ).toBeVisible();
    await page.goto(`/workspaces/${org.workspaceId}`);
    await expect(
      page.getByRole("heading", { name: "Workspace not found" }),
    ).toBeVisible();
    await page.goto(`/chat/${conversation.conversation_id}`);
    await expect(
      page.getByRole("heading", { name: "Conversation not found" }),
    ).toBeVisible();
  });

  await other.context.close();
});

test("an admin deactivates a member: signed out at once, still listed, and can be let back in", async ({
  org,
  browser,
}) => {
  const { page } = org;
  const eli = newAccount("employee");
  const employee = await newBrowserSession(browser);
  await employee.api.register(eli, (await org.api.invite(eli.email)).token);
  const signIn = { email: eli.email, password: eli.password };

  await page.goto("/admin?tab=members");
  const row = page.getByTestId("org-member-row").filter({ hasText: eli.email });
  await row.getByRole("button", { name: `Deactivate ${eli.fullName}` }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "Deactivate" })
    .click();
  await expect(toast(page, "Member deactivated")).toBeVisible();
  await expect(row).toContainText("Deactivated");
  expect((await employee.api.getRaw("/auth/me")).status()).toBe(401);
  expect(
    (await employee.api.send("POST", "/auth/login", signIn)).status(),
  ).toBe(401);

  await row.getByRole("button", { name: `Reactivate ${eli.fullName}` }).click();
  await expect(toast(page, "Member reactivated")).toBeVisible();
  await expect(row).not.toContainText("Deactivated");
  expect(
    (await employee.api.send("POST", "/auth/login", signIn)).status(),
  ).toBe(200);
  await employee.context.close();
});
