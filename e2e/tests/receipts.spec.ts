import { expect, test } from "../support/test";
import { travelPolicy, travelPolicyV2 } from "../support/documents";
import { ask, toast } from "../support/ui";

const QUESTION = "For which trips do employees book economy class flights?";

test("an answer has a receipt, and says so once the document it used is replaced", async ({ org }) => {
  const { page } = org;
  const document = await org.api.upload({ workspaceId: org.workspaceId, ...travelPolicy });
  await org.api.waitUntilIndexed(document.id);

  await page.goto("/chat");
  let conversationUrl = "";
  await test.step("every statement of the answer is checked against its source", async () => {
    const answer = await ask(page, QUESTION);
    conversationUrl = page.url();
    await expect(answer.getByTestId("assistant-answer")).toContainText("shorter than six hours");
    const receipt = answer.getByTestId("answer-receipt");
    // The offline generator quotes its passages, so every statement matches.
    await expect(receipt.getByTestId("receipt-summary")).toHaveText(
      /^(\d+) of \1 (statement matches its source|statements match their sources)$/,
    );
    await receipt.getByText("Receipt:").click();
    await expect(receipt.getByTestId("receipt-claim").first()).toHaveAttribute("data-verdict", "supported");
    await expect(receipt.getByTestId("receipt-documents")).toHaveText("Based on Travel Policy (version 1)");
    await expect(answer.getByTestId("replaced-sources")).toHaveCount(0);
  });

  await test.step("after the document is replaced, the old answer names the version it used", async () => {
    await page.goto(`/documents/${document.id}`);
    await page.getByTestId("replace-input").setInputFiles(travelPolicyV2);
    await expect(toast(page, "New version uploaded")).toBeVisible();
    await org.api.waitUntilIndexed(document.id);

    await page.goto(conversationUrl);
    const notice = page.getByTestId("assistant-message").first().getByTestId("replaced-sources");
    await expect(notice).toContainText("This answer used version 1 of “Travel Policy”. Version 2 replaced it on");
  });

  await test.step("asking again answers from the current version", async () => {
    const answer = await ask(page, QUESTION);
    await expect(answer.getByTestId("assistant-answer")).toContainText("shorter than eight hours");
    await expect(answer.getByTestId("replaced-sources")).toHaveCount(0);
    await answer.getByText("Receipt:").click();
    await expect(answer.getByTestId("receipt-documents")).toHaveText("Based on Travel Policy (version 2)");
  });
});
