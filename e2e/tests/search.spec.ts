import type { Page } from "@playwright/test";
import { expect, test as base } from "../support/test";
import { uniqueName } from "../support/accounts";
import { remoteWorkPolicy, safetyManual, travelPolicy } from "../support/documents";
import { search } from "../support/ui";

/** Every test starts with a PDF and a Markdown file in "General" and a text file in "Archive", all indexed. */
const test = base.extend<{ library: { archive: string } }>({
  library: [
    async ({ org }, use) => {
      const archive = uniqueName("Archive");
      const { id: archiveId } = await org.api.createWorkspace(archive);
      const documents = [
        await org.api.upload({ workspaceId: org.workspaceId, ...safetyManual }),
        await org.api.upload({ workspaceId: org.workspaceId, ...remoteWorkPolicy }),
        await org.api.upload({ workspaceId: archiveId, ...travelPolicy }),
      ];
      for (const document of documents) await org.api.waitUntilIndexed(document.id);
      await use({ archive });
    },
    { auto: true },
  ],
});

const results = (page: Page) => page.getByTestId("search-result");
const resultFor = (page: Page, title: string) => results(page).filter({ hasText: title });

test("a search finds the passage, highlights the matching words and explains the match", async ({ org }) => {
  const { page } = org;
  await search(page, "assembly point evacuation");

  await expect(page.getByTestId("search-summary")).toContainText("hybrid mode");
  const best = results(page).first();
  await expect(best).toContainText("Facility Safety Manual");
  await expect(best).toContainText("PDF");
  await expect(best).toContainText("General");
  await expect(best).toContainText("Page 3 · Emergency Evacuation");
  await expect(best.getByTestId("search-snippet")).toContainText(
    "Staff gather at the assembly point behind building C",
  );

  await test.step("matching words are marked", async () => {
    const marked = best.getByTestId("search-snippet").locator("mark");
    await expect(marked.first()).toBeVisible();
    const words = (await marked.allTextContents()).map((word) => word.toLowerCase());
    expect(words).toEqual(expect.arrayContaining(["assembly", "point", "evacuation"]));
  });

  await test.step("the 'why this matched' details open and close", async () => {
    const toggle = best.getByTestId("why-matched-toggle");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(best.getByTestId("why-matched")).toHaveCount(0);
    await toggle.click();
    const why = best.getByTestId("why-matched");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(why).toContainText("Final rank#1");
    await expect(why).toContainText(/Keyword \(full text\)\s*rank 1/);
    await expect(why).toContainText("Vector (semantic)");
    await expect(why).toContainText("Matched terms");
    await expect(why).toContainText("assembl"); // the stem the full-text index matched
    await toggle.click();
    await expect(why).toBeHidden();
  });
});

test("results can be narrowed by file type and by workspace", async ({ org, library }) => {
  const { page } = org;
  await search(page, "policy");
  // The vector side returns every chunk of the three small documents: 3 (PDF) + 3 (Markdown) + 1 (text).
  await expect(results(page)).toHaveCount(7);

  const types = page.getByRole("group", { name: "File types" });
  await test.step("file type", async () => {
    await types.getByLabel("PDF").check();
    await expect(results(page)).toHaveCount(3);
    await expect(results(page).filter({ hasNotText: "Facility Safety Manual" })).toHaveCount(0);

    await types.getByLabel("Markdown").check();
    await expect(results(page)).toHaveCount(6);
    await expect(resultFor(page, "Travel Policy")).toHaveCount(0);

    await types.getByLabel("PDF").uncheck();
    await expect(results(page)).toHaveCount(3);
    await expect(resultFor(page, "Remote Work Policy")).toHaveCount(3);
    await types.getByLabel("Markdown").uncheck();
    await expect(results(page)).toHaveCount(7);
  });

  await test.step("workspace", async () => {
    await page.getByTestId("scope-selector").click();
    const picker = page.getByRole("dialog");
    await picker.getByRole("checkbox", { name: library.archive, exact: true }).check();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("scope-selector")).toContainText("1 workspace");
    await expect(results(page)).toHaveCount(1);
    await expect(results(page)).toContainText("Travel Policy");
    await expect(results(page)).toContainText(library.archive);
  });

  await test.step("a workspace and a type that do not match leave nothing", async () => {
    await types.getByLabel("PDF").check();
    await expect(results(page)).toHaveCount(0);
    await expect(page.getByText("No results for “policy”")).toBeVisible();
  });
});

test("opening a result lands on the document, at the page for PDFs", async ({ org }) => {
  const { page } = org;

  await test.step("PDF: the page of the passage", async () => {
    await search(page, "assembly point evacuation");
    await page.getByRole("link", { name: "Open at page 3" }).click();
    await expect(page).toHaveURL(/\/documents\/[0-9a-f-]{36}\?page=3&chunk=[0-9a-f-]{36}$/);
    await expect(page.getByTestId("document-title")).toHaveText("Facility Safety Manual");
    // A passage link opens the extracted text with the passage marked ...
    const cited = page.getByTestId("chunk").and(page.locator('[data-highlighted="true"]'));
    await expect(cited).toBeVisible();
    await expect(cited).toContainText("Staff gather at the assembly point");
    await expect(page.getByRole("heading", { name: "Page 3" })).toBeVisible();
    // ... and "Show in PDF" shows that page in the preview.
    await page.getByRole("heading", { name: "Page 3" }).getByRole("button", { name: "Show in PDF" }).click();
    await expect(page.getByTestId("pdf-preview")).toHaveAttribute("src", /\/download\?inline=true#page=3$/);
  });

  await test.step("Markdown: the passage itself", async () => {
    await search(page, "corporate VPN encryption");
    await page.getByRole("link", { name: "Open document" }).first().click();
    await expect(page).toHaveURL(/\/documents\/[0-9a-f-]{36}\?chunk=[0-9a-f-]{36}$/);
    await expect(page.getByTestId("document-title")).toHaveText("Remote Work Policy");
    await expect(page.locator('[data-testid="chunk"][data-highlighted="true"]')).toContainText("corporate VPN");
  });
});
