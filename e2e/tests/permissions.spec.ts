import type { Page } from "@playwright/test";
import { expect, test } from "../support/test";
import { newAccount, uniqueName } from "../support/accounts";
import { compensationMemo, engineeringHandbook } from "../support/documents";
import {
  addWorkspaceMember,
  ask,
  createWorkspace,
  expectIndexedInLibrary,
  invite,
  newBrowserSession,
  search,
  toast,
  uploadFile,
} from "../support/ui";

const SECRET_QUESTION = "What is the executive bonus pool allocation?";

/** Opens the library and checks which documents it lists (the whole list, not just a prefix). */
async function expectLibrary(page: Page, titles: string[]) {
  await page.goto("/documents");
  if (titles.length === 0) return expect(page.getByText("No documents yet")).toBeVisible();
  await expect
    .poll(async () => (await page.getByTestId("document-row").getByRole("link").allTextContents()).sort())
    .toEqual([...titles].sort());
}

test("an employee never sees a restricted document, in any part of the product", async ({ org, browser }) => {
  const admin = org.page;
  const workspace = uniqueName("Legal");
  const eli = newAccount("employee");

  const workspaceId = await createWorkspace(admin, workspace);
  const restrictedId = await uploadFile(admin, compensationMemo, { workspace, restricted: true });
  await uploadFile(admin, engineeringHandbook, { workspace });
  await expectLibrary(admin, ["Executive Compensation Memo", "Engineering Handbook"]);
  await expectIndexedInLibrary(admin, "Executive Compensation Memo");
  await expectIndexedInLibrary(admin, "Engineering Handbook");
  await expect(admin.getByTestId("document-row").filter({ hasText: "Executive Compensation Memo" })).toContainText(
    "Restricted",
  );

  const employee = await newBrowserSession(browser);
  await test.step("the invitation link registers the employee into the organization", async () => {
    const link = await invite(admin, eli.email);
    expect(link).toMatch(/\/invite\/.{20,}/);
    await employee.page.goto(link);
    await expect(employee.page.getByRole("heading", { name: `Join ${org.account.organization}` })).toBeVisible();
    await expect(employee.page.getByTestId("register-email")).toHaveValue(eli.email);
    await expect(employee.page.getByTestId("register-email")).toHaveJSProperty("readOnly", true);
    await employee.page.getByTestId("register-name").fill(eli.fullName);
    await employee.page.getByTestId("register-password").fill(eli.password);
    await employee.page.getByTestId("register-submit").click();
    await expect(employee.page).toHaveURL(/\/dashboard$/);
    await expect(employee.page.getByTestId("org-switcher")).toContainText(org.account.organization);
    await expect(employee.page.getByTestId("nav-admin")).toHaveCount(0);
  });

  await test.step("a new employee who is in no workspace sees no documents at all", async () => {
    await expectLibrary(employee.page, []);
    expect((await employee.api.getRaw(`/documents/${restrictedId}`)).status()).toBe(404);
  });

  await test.step("the admin adds the employee to the workspace as a viewer", async () => {
    await addWorkspaceMember(admin, workspaceId, eli, "Viewer");
  });

  await test.step("library: the employee sees the shared document, not the restricted one", async () => {
    await expectLibrary(employee.page, ["Engineering Handbook"]);
    await employee.page.goto(`/workspaces/${workspaceId}`);
    await expect(employee.page.getByText("1 document", { exact: true })).toBeVisible(); // restricted ones are not even counted
    await admin.goto(`/workspaces/${workspaceId}`);
    await expect(admin.getByText("2 documents", { exact: true })).toBeVisible();
  });

  await test.step("dashboard: titles and activity do not leak it", async () => {
    await employee.page.goto("/dashboard");
    await expect(employee.page.getByText("Engineering Handbook")).toBeVisible();
    await expect(employee.page.locator("main")).not.toContainText("Executive Compensation Memo");
  });

  await test.step("direct link: opening the restricted document shows not found", async () => {
    await employee.page.goto(`/documents/${restrictedId}`);
    await expect(employee.page.getByRole("heading", { name: "Document not found" })).toBeVisible();
    await expect(employee.page.getByTestId("document-title")).toHaveCount(0);
    for (const path of ["", "/status", "/chunks", "/download", "/download?inline=true"]) {
      expect((await employee.api.getRaw(`/documents/${restrictedId}${path}`)).status(), path).toBe(404);
    }
  });

  await test.step("search: the admin finds the memo, the employee finds nothing of it", async () => {
    await search(admin, "zephyrquartz executive bonus");
    await expect(admin.getByTestId("search-result").first()).toContainText("Executive Compensation Memo");
    await expect(admin.getByTestId("search-snippet").first().locator("mark").first()).toBeVisible();

    await search(employee.page, "zephyrquartz executive bonus");
    // The vector side still returns what the employee may read; none of it is the memo.
    await expect(
      employee.page.getByTestId("search-result").filter({ hasText: "Executive Compensation Memo" }),
    ).toHaveCount(0);
    await expect(
      employee.page.getByTestId("search-snippet").filter({ hasText: /zephyrquartz|bonus pool/ }),
    ).toHaveCount(0);
    // Full-text only: the word exists only in the memo, so there is nothing to find.
    await employee.page.getByRole("button", { name: "Advanced" }).click();
    await employee.page.getByTestId("search-mode").selectOption("keyword");
    await expect(employee.page.getByText("No results for “zephyrquartz executive bonus”")).toBeVisible();
  });

  await test.step("chat: the admin gets an answer from the memo, the employee gets none", async () => {
    await admin.goto("/chat");
    const adminAnswer = await ask(admin, SECRET_QUESTION);
    await expect(adminAnswer.getByTestId("assistant-answer")).toContainText(
      "zephyrquartz allocation of four million credits",
    );
    await adminAnswer.getByTestId("citation-chip").first().click();
    await expect(admin.getByTestId("source-panel")).toContainText("Executive Compensation Memo");

    await employee.page.goto("/chat");
    const answer = await ask(employee.page, SECRET_QUESTION);
    await expect(answer.getByTestId("answer-banner")).toHaveAttribute("data-status", "insufficient_evidence");
    await expect(answer.getByTestId("assistant-answer")).not.toContainText(/zephyrquartz|four million/);
    await expect(answer.getByTestId("citation-chip")).toHaveCount(0);
    // Whatever passages were retrieved for the employee, the memo is not among them.
    await answer.getByTestId("open-sources").click();
    await expect(employee.page.getByTestId("source-panel")).not.toContainText(
      /Executive Compensation Memo|zephyrquartz/,
    );
    const conversationId = employee.page.url().split("/").pop()!;
    const stored = (await employee.api.messages(conversationId)).items.flatMap((m) => m.sources ?? []);
    expect(stored.map((s) => s.document_title)).not.toContain("Executive Compensation Memo");
    expect(JSON.stringify(stored)).not.toMatch(/zephyrquartz/);
  });

  await test.step("crafted requests that name the restricted document do not widen access", async () => {
    const chat = await employee.api.post<{ assistant_message: { answer_status: string; sources: unknown[] } }>(
      "/chat",
      {
        message: SECRET_QUESTION,
        scope: { document_ids: [restrictedId] },
      },
    );
    expect(chat.assistant_message.answer_status).toBe("insufficient_evidence");
    expect(chat.assistant_message.sources).toHaveLength(0);
    const found = await employee.api.post<{ results: unknown[] }>("/search", {
      query: "zephyrquartz",
      filters: { document_ids: [restrictedId] },
    });
    expect(found.results).toHaveLength(0);
  });

  await employee.context.close();
});

test("workspace membership and sharing changes reach the employee immediately", async ({ org, browser }) => {
  const admin = org.page;
  const workspace = uniqueName("Legal");
  const eli = newAccount("employee");

  // Setup through the API (the tests above drive the same actions through the UI).
  const { id: workspaceId } = await org.api.createWorkspace(workspace);
  const memo = await org.api.upload({ workspaceId, ...compensationMemo, visibility: "restricted" });
  const handbook = await org.api.upload({ workspaceId, ...engineeringHandbook });
  const employee = await newBrowserSession(browser);
  await employee.api.register(eli, (await org.api.invite(eli.email)).token);
  await org.api.waitUntilIndexed(memo.id);
  await org.api.waitUntilIndexed(handbook.id);

  const memoTitle = "Executive Compensation Memo";
  const handbookTitle = "Engineering Handbook";
  const roleOf = (page: Page) => page.getByLabel(`Role of ${eli.fullName}`);

  await expectLibrary(employee.page, []);

  await test.step("added as a viewer: the shared document appears", async () => {
    await addWorkspaceMember(admin, workspaceId, eli, "Viewer");
    await expectLibrary(employee.page, [handbookTitle]);
  });

  await test.step("made an owner: owners can read every document of their workspace", async () => {
    await admin.goto(`/workspaces/${workspaceId}`);
    await roleOf(admin).selectOption({ label: "Owner" });
    await expect(roleOf(admin)).toHaveValue("owner");
    await expectLibrary(employee.page, [handbookTitle, memoTitle]);
    await employee.page.getByRole("link", { name: memoTitle }).click();
    await expect(employee.page.getByTestId("document-title")).toHaveText(memoTitle);
  });

  await test.step("made a viewer again: the restricted document disappears", async () => {
    await roleOf(admin).selectOption({ label: "Viewer" });
    await expect(roleOf(admin)).toHaveValue("viewer");
    await expectLibrary(employee.page, [handbookTitle]);
    await employee.page.goto(`/documents/${memo.id}`);
    await expect(employee.page.getByRole("heading", { name: "Document not found" })).toBeVisible();
  });

  await test.step("shared with the employee personally: the restricted document appears", async () => {
    await admin.goto(`/documents/${memo.id}`);
    await admin.getByRole("tab", { name: "Sharing" }).click();
    await admin.getByTestId("add-grant").click();
    await admin
      .getByRole("dialog")
      .getByRole("button", { name: new RegExp(eli.email) })
      .click();
    await admin.getByTestId("save-sharing").click();
    await expect(toast(admin, "Sharing updated")).toBeVisible();
    await expectLibrary(employee.page, [handbookTitle, memoTitle]);

    await employee.page.goto("/chat");
    const answer = await ask(employee.page, SECRET_QUESTION);
    await expect(answer.getByTestId("assistant-answer")).toContainText("zephyrquartz allocation");
  });

  await test.step("the grant is removed: it disappears again", async () => {
    await admin.goto(`/documents/${memo.id}?tab=sharing`);
    await admin.getByRole("button", { name: `Remove ${eli.fullName}` }).click();
    await admin.getByTestId("save-sharing").click();
    await expect(toast(admin, "Sharing updated")).toBeVisible();
    await expectLibrary(employee.page, [handbookTitle]);
  });

  await test.step("removed from the workspace: nothing is left, and what was cited stays redacted", async () => {
    await admin.goto(`/workspaces/${workspaceId}`);
    await admin.getByRole("button", { name: `Remove ${eli.fullName}` }).click();
    await admin.getByTestId("confirm-action").click();
    await expect(admin.getByTestId("member-row").filter({ hasText: eli.email })).toHaveCount(0);
    await expectLibrary(employee.page, []);
    await employee.page.goto(`/documents/${handbook.id}`);
    await expect(employee.page.getByRole("heading", { name: "Document not found" })).toBeVisible();

    // The earlier answer is still in the employee's history, but its source text is hidden now.
    await employee.page.goto("/chat");
    await employee.page.getByTestId("conversation-list").getByRole("link").first().click();
    await expect(employee.page.getByTestId("user-message")).toHaveText(SECRET_QUESTION);
    await employee.page.getByTestId("open-sources").click();
    const panel = employee.page.getByTestId("source-panel");
    await expect(panel).not.toContainText("zephyrquartz");
    await expect(panel.getByTestId("source-item").first()).toHaveAttribute("data-redacted", "true");
  });

  await employee.context.close();
});
