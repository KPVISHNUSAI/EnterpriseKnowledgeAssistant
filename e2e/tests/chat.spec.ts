import type { Page } from "@playwright/test";
import { expect, test } from "../support/test";
import { remoteWorkPolicy } from "../support/documents";
import { ask, toast } from "../support/ui";

const QUESTION = "How many days per week may employees work remotely?";
const FOLLOW_UP = "Who approves it?";
const UNRELATED = "What is the airspeed velocity of an unladen swallow?";

const conversations = (page: Page) => page.getByTestId("conversation-list").getByRole("link");

test("a question gets a cited answer; the source opens; follow-ups, abstention and deletion work", async ({ org }) => {
  const { page } = org;
  const document = await org.api.upload({ workspaceId: org.workspaceId, ...remoteWorkPolicy });
  await org.api.waitUntilIndexed(document.id);

  await page.goto("/chat");
  await expect(page.getByRole("heading", { name: "Ask your documents" })).toBeVisible();
  await expect(page.getByText("No conversations yet.")).toBeVisible();

  let conversationUrl = "";
  await test.step("the answer cites the document", async () => {
    const answer = await ask(page, QUESTION);
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    conversationUrl = page.url();
    await expect(page.getByTestId("user-message")).toHaveText(QUESTION);
    await expect(answer.getByTestId("assistant-answer")).toContainText("up to three days per week");
    await expect(answer.getByTestId("answer-banner")).toHaveCount(0);
    const chip = answer.getByTestId("citation-chip").first();
    await expect(chip).toHaveAttribute("data-state", "verified");
    await expect(chip).toHaveAccessibleName(/Remote Work Policy/);
  });

  await test.step("the conversation is listed and titled by the question", async () => {
    await expect(conversations(page)).toHaveCount(1);
    await expect(conversations(page).first()).toContainText(QUESTION);
    await expect(conversations(page).first()).toHaveAttribute("aria-current", "page");
  });

  await test.step("a citation opens the source panel with the passage", async () => {
    const chip = page.getByTestId("citation-chip").first();
    const sourceId = (await chip.getAttribute("data-source-id"))!;
    await chip.click();
    const panel = page.getByTestId("source-panel");
    await expect(panel).toBeVisible();
    const source = panel.locator(`[data-testid="source-item"][data-source-id="${sourceId}"]`);
    await expect(source).toHaveAttribute("data-cited", "true");
    await expect(source).toContainText("Cited");
    await expect(source).toContainText("Employees may work remotely up to three days per week with manager approval.");
    await expect(source.getByRole("link", { name: "Remote Work Policy" })).toBeVisible();

    await test.step("and the source opens the document at that passage", async () => {
      await source.getByRole("link", { name: "Remote Work Policy" }).click();
      await expect(page).toHaveURL(new RegExp(`/documents/${document.id}\\?chunk=[0-9a-f-]{36}$`));
      const cited = page.locator('[data-testid="chunk"][data-highlighted="true"]');
      await expect(cited).toContainText("up to three days per week");
      await expect(cited).toContainText("Cited passage");
    });
  });

  await test.step("the stored conversation comes back with its citation", async () => {
    await page.goto(conversationUrl);
    await expect(page.getByTestId("user-message")).toHaveText(QUESTION);
    await expect(page.getByTestId("citation-chip").first()).toHaveAttribute("data-state", "verified");
  });

  await test.step("a follow-up is answered in the same conversation", async () => {
    const answer = await ask(page, FOLLOW_UP);
    await expect(page).toHaveURL(conversationUrl);
    await expect(page.getByTestId("user-message")).toHaveCount(2);
    await expect(answer.getByTestId("assistant-answer")).toContainText("approved by the direct manager");
    await expect(answer.getByTestId("citation-chip").first()).toHaveAttribute("data-state", "verified");
    await expect(conversations(page)).toHaveCount(1);
  });

  await test.step("the dashboard lists the conversation", async () => {
    await page.goto("/dashboard");
    const recent = page.getByRole("link", { name: QUESTION });
    await expect(recent).toBeVisible();
    await expect(recent.locator("xpath=..")).toContainText("4 messages");
    await recent.click();
    await expect(page).toHaveURL(conversationUrl);
  });

  await test.step("a question the documents cannot answer shows the not-enough-evidence banner", async () => {
    await page.getByTestId("new-chat").click();
    await expect(page).toHaveURL(/\/chat$/);
    const answer = await ask(page, UNRELATED);
    const banner = answer.getByTestId("answer-banner");
    await expect(banner).toHaveAttribute("data-status", "insufficient_evidence");
    await expect(banner).toContainText("Not enough evidence in your documents");
    await expect(answer.getByTestId("citation-chip")).toHaveCount(0);
    await expect(conversations(page)).toHaveCount(2);
    await expect(conversations(page).first()).toContainText(UNRELATED); // newest first
  });

  await test.step("deleting conversations asks first and removes them", async () => {
    const unrelatedId = page.url().split("/").pop()!;
    await page.getByRole("button", { name: `Delete conversation ${UNRELATED}` }).click();
    const dialog = page.getByRole("alertdialog", { name: "Delete this conversation?" });
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(conversations(page)).toHaveCount(2);

    await page.getByRole("button", { name: `Delete conversation ${UNRELATED}` }).click();
    await page.getByTestId("confirm-action").click();
    await expect(toast(page, "Conversation deleted")).toBeVisible();
    await expect(conversations(page)).toHaveCount(1);
    await expect(page).toHaveURL(/\/chat$/); // the open conversation was the deleted one
    expect((await org.api.getRaw(`/conversations/${unrelatedId}/messages`)).status()).toBe(404);

    await page.getByRole("button", { name: `Delete conversation ${QUESTION}` }).click();
    await page.getByTestId("confirm-action").click();
    await expect(page.getByText("No conversations yet.")).toBeVisible();
  });
});
