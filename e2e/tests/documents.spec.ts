import { readFileSync, rmSync, writeFileSync } from "node:fs";
import type { Locator, Page } from "@playwright/test";
import { expect, test } from "../support/test";
import { LONG_TIMEOUT } from "../support/api";
import { toast } from "../support/ui";
import { remoteWorkPolicy, safetyManual, travelPolicy, travelPolicyV2 } from "../support/documents";
import { pauseWorker, resumeWorker } from "../support/worker";

/** The value next to a label in the metadata card of the detail page. */
const metadata = (page: Page, label: string): Locator =>
  page.getByTestId("document-metadata").locator(`dt:text-is("${label}") + dd`);
const statuses = (page: Page) =>
  page.getByTestId("document-status").evaluateAll((badges) => badges.map((b) => b.getAttribute("data-status")));

// A test that times out while the worker is suspended must not leave it that way.
test.afterEach(() => resumeWorker());

test("upload TXT, Markdown and PDF, read them, re-index, replace the file and delete", async ({ org }) => {
  const { page } = org;
  const rows = page.getByTestId("document-row");
  /** Opens a document from the library and returns its id. */
  const open = async (title: string) => {
    await page.getByTestId("documents-table").getByRole("link", { name: title, exact: true }).click();
    await expect(page).toHaveURL(/\/documents\/[0-9a-f-]{36}$/);
    return page.url().split("/").pop()!;
  };

  await test.step("three uploads are queued while the worker is paused, then indexed", async () => {
    pauseWorker();
    try {
      await page.goto("/documents");
      await page.getByTestId("upload-input").setInputFiles([travelPolicy, remoteWorkPolicy, safetyManual]);
      const uploads = page.getByTestId("upload-item");
      await expect(uploads).toHaveCount(3);
      for (const upload of await uploads.all()) await expect(upload).toHaveAttribute("data-status", "done");
      await expect(rows).toHaveCount(3);
      await expect.poll(() => statuses(page)).toEqual(["queued", "queued", "queued"]);
    } finally {
      resumeWorker();
    }
    // The table polls every 3 s while anything is queued or processing.
    await expect.poll(() => statuses(page), { timeout: LONG_TIMEOUT }).toEqual(["indexed", "indexed", "indexed"]);
    await expect(page.getByTestId("documents-table")).toContainText("Facility Safety Manual");

    await page.goto("/dashboard");
    const stats = page.getByTestId("dashboard-stats");
    await expect(stats).toContainText("Documents3");
    await expect(stats).toContainText("Indexed3");
    await expect(stats).toContainText("Failed0");
    await expect(page.getByRole("link", { name: "Facility Safety Manual" })).toBeVisible();
    await expect(page.getByTestId("recent-activity")).toContainText("Facility Safety Manual");
  });

  await test.step("the library sorts by title in both directions", async () => {
    await page.goto("/documents");
    const titles = () => page.getByTestId("document-row").getByRole("link").allTextContents();
    await page.getByRole("button", { name: "Title" }).click();
    await expect.poll(titles).toEqual(["Facility Safety Manual", "Remote Work Policy", "Travel Policy"]);
    await page.getByRole("button", { name: "Title" }).click();
    await expect.poll(titles).toEqual(["Travel Policy", "Remote Work Policy", "Facility Safety Manual"]);
  });

  await test.step("the library filters by title and type", async () => {
    await page.getByTestId("document-search").fill("travel");
    await expect(rows).toHaveCount(1);
    await expect(rows).toContainText("Travel Policy");
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(rows).toHaveCount(3);
    await page.getByLabel("File type").selectOption("pdf");
    await expect(rows).toHaveCount(1);
    await expect(rows).toContainText("Facility Safety Manual");
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(rows).toHaveCount(3);
  });

  await test.step("the detail page of a text file shows its metadata and extracted text", async () => {
    await open("Travel Policy");
    await expect(page.getByTestId("document-title")).toHaveText("Travel Policy");
    await expect(metadata(page, "File")).toHaveText("Travel Policy.txt");
    await expect(metadata(page, "Type")).toContainText("Text");
    await expect(metadata(page, "Version")).toHaveText("1");
    await expect(metadata(page, "Workspace")).toHaveText("General");
    await expect(metadata(page, "Uploaded by")).toHaveText(org.account.fullName);
    await expect(metadata(page, "Chunks")).not.toHaveText("0");
    await expect(page.getByTestId("document-status")).toHaveAttribute("data-status", "indexed");
    await expect(page.getByTestId("extracted-text")).toContainText("Employees book economy class flights");
    await expect(page.getByTestId("pdf-preview")).toHaveCount(0);

    // "Download" returns the file as it was uploaded.
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("link", { name: "Download" }).click(),
    ]);
    expect(download.suggestedFilename()).toBe("Travel Policy.txt");
    expect(readFileSync(await download.path())).toEqual(travelPolicy.buffer);
  });

  await test.step("the detail page of a Markdown file shows its sections", async () => {
    await page.goto("/documents");
    await open("Remote Work Policy");
    await expect(page.getByTestId("extracted-text")).toContainText(
      "Employees may work remotely up to three days per week",
    );
    await expect(page.getByTestId("extracted-text")).toContainText("Eligibility");
    await expect(page.getByTestId("chunk")).toHaveCount(3);
  });

  await test.step("the detail page of a PDF previews the file and shows the text by page", async () => {
    await page.goto("/documents");
    const id = await open("Facility Safety Manual");
    await expect(metadata(page, "Pages")).toHaveText("3");
    await expect(metadata(page, "Type")).toContainText("PDF");

    const preview = page.getByTestId("pdf-preview");
    await expect(preview).toBeVisible();
    const source = (await preview.getAttribute("src"))!;
    expect(source).toBe(`/api/v1/documents/${id}/download?inline=true`);
    // The frame points at the real file: served inline as a PDF through the proxy.
    const file = await page.request.get(source);
    expect(file.status()).toBe(200);
    expect(file.headers()["content-type"]).toBe("application/pdf");
    expect(file.headers()["content-disposition"]).toMatch(/^inline/);
    expect((await file.body()).subarray(0, 5).toString()).toBe("%PDF-");

    await page.getByRole("tab", { name: "Extracted text" }).click();
    const text = page.getByTestId("extracted-text");
    await expect(text).toContainText("Staff gather at the assembly point behind building C");
    for (const heading of ["Page 1", "Page 2", "Page 3"])
      await expect(text.getByRole("heading", { name: heading })).toBeVisible();
    // "Show in PDF" moves the preview to that page.
    await text.getByRole("heading", { name: "Page 2" }).getByRole("button", { name: "Show in PDF" }).click();
    await expect(page.getByTestId("pdf-preview")).toHaveAttribute(
      "src",
      `/api/v1/documents/${id}/download?inline=true#page=2`,
    );
  });

  await test.step("re-indexing runs the document through the worker again", async () => {
    await page.goto("/documents");
    const id = await open("Travel Policy");
    const before = await org.api.document(id);
    await page.getByTestId("reindex").click();
    await expect(toast(page, "Re-indexing queued")).toBeVisible();
    await expect
      .poll(
        async () => {
          const doc = await org.api.document(id);
          return doc.processing_status === "indexed" && doc.indexed_at !== before.indexed_at;
        },
        { timeout: LONG_TIMEOUT },
      )
      .toBe(true);
    await expect(page.getByTestId("document-status")).toHaveAttribute("data-status", "indexed");
    await expect(page.getByTestId("extracted-text")).toContainText("Rental cars are permitted");
  });

  await test.step("replacing the file creates version 2 with the new text", async () => {
    await expect(metadata(page, "Version")).toHaveText("1");
    await page.getByTestId("replace-input").setInputFiles(travelPolicyV2);
    await expect(toast(page, "New version uploaded")).toBeVisible();
    await expect(metadata(page, "Version")).toHaveText("2");
    await expect(metadata(page, "File")).toHaveText("Travel Policy v2.txt");
    await expect(page.getByTestId("extracted-text")).toContainText("Train travel is preferred", {
      timeout: LONG_TIMEOUT,
    });
    await expect(page.getByTestId("extracted-text")).not.toContainText("Rental cars are permitted");
    await expect(page.getByTestId("document-status")).toHaveAttribute("data-status", "indexed");
  });

  await test.step("a deleted document leaves the library and the search results", async () => {
    // Before: the Markdown document is found by a word only it contains.
    await page.goto("/search");
    await page.getByTestId("search-input").fill("probation");
    await page.getByTestId("search-submit").click();
    await expect(page.getByTestId("search-result").first()).toContainText("Remote Work Policy");

    await page.goto("/documents");
    const id = await open("Remote Work Policy");
    await page.getByRole("button", { name: "Delete", exact: true }).click();
    const dialog = page.getByRole("alertdialog", { name: "Delete this document?" });
    await expect(dialog).toContainText("Remote Work Policy");
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).toBeHidden();
    await expect(page).toHaveURL(new RegExp(`/documents/${id}$`));

    await page.getByRole("button", { name: "Delete", exact: true }).click();
    await page.getByTestId("confirm-action").click();
    await expect(toast(page, "Document deleted")).toBeVisible(); // it only lasts 5 s, the page may take longer
    await expect(page).toHaveURL(/\/documents$/);
    await expect(rows).toHaveCount(2);
    await expect(page.getByTestId("documents-table")).not.toContainText("Remote Work Policy");
    expect((await org.api.getRaw(`/documents/${id}`)).status()).toBe(404);
    expect((await org.api.getRaw(`/documents/${id}/download`)).status()).toBe(404);

    await page.goto("/search");
    await page.getByTestId("search-input").fill("probation");
    await page.getByTestId("search-submit").click();
    await expect(page.getByTestId("search-summary")).toBeVisible();
    // Vector search still returns the other documents; nothing of the deleted one may be left.
    await expect(page.getByTestId("search-result").filter({ hasText: "Remote Work Policy" })).toHaveCount(0);
    await expect(page.getByTestId("search-snippet").filter({ hasText: "probation" })).toHaveCount(0);
  });
});

test("a file that cannot be parsed ends up failed with a readable reason", async ({ org }) => {
  const { page } = org;
  // Passes the upload checks (it starts like a PDF) but the worker cannot read it.
  const broken = {
    name: "Broken Report.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4\nthis is not really a pdf\n"),
  };

  await page.goto("/documents");
  await page.getByTestId("upload-input").setInputFiles(broken);
  const status = page.getByTestId("document-row").getByTestId("document-status");
  await expect(status).toHaveAttribute("data-status", "failed", { timeout: LONG_TIMEOUT });
  await expect(page.getByTestId("document-row")).toContainText(/could not|unreadable|corrupt|no text/i);
  await expect(page.getByRole("button", { name: "Retry indexing Broken Report" })).toBeVisible();

  await page.getByRole("link", { name: "Broken Report", exact: true }).click();
  await expect(page.getByTestId("ingestion-failure")).toBeVisible();
  await expect(page.getByTestId("reindex")).toContainText("Retry indexing");

  await page.goto("/dashboard");
  await expect(page.getByTestId("dashboard-stats")).toContainText("Failed1");
});

test("the row menu of the library opens, downloads, re-indexes and deletes", async ({ org }) => {
  const { page } = org;
  const document = await org.api.upload({ workspaceId: org.workspaceId, ...travelPolicy });
  await org.api.waitUntilIndexed(document.id);

  await page.goto("/documents");
  const row = page.getByTestId("document-row").filter({ hasText: "Travel Policy" });
  const menu = async (item: string) => {
    await row.getByTestId("document-actions").click();
    return page.getByRole("menuitem", { name: item });
  };

  await test.step("Download original", async () => {
    const [download] = await Promise.all([page.waitForEvent("download"), (await menu("Download original")).click()]);
    expect(download.suggestedFilename()).toBe("Travel Policy.txt");
    expect(readFileSync(await download.path())).toEqual(travelPolicy.buffer);
  });

  await test.step("Open", async () => {
    await (await menu("Open")).click();
    await expect(page).toHaveURL(new RegExp(`/documents/${document.id}$`));
    await page.goBack();
  });

  await test.step("Re-index", async () => {
    const before = (await org.api.document(document.id)).indexed_at;
    await (await menu("Re-index")).click();
    await expect(toast(page, "Re-indexing queued")).toBeVisible();
    await expect
      .poll(async () => (await org.api.document(document.id)).indexed_at !== before, { timeout: LONG_TIMEOUT })
      .toBe(true);
  });

  await test.step("Delete", async () => {
    await (await menu("Delete")).click();
    await page
      .getByRole("alertdialog", { name: "Delete this document?" })
      .getByRole("button", { name: "Delete document" })
      .click();
    await expect(toast(page, "Document deleted")).toBeVisible();
    await expect(page.getByText("No documents yet")).toBeVisible();
  });
});

test("files that cannot be uploaded are refused with a clear message", async ({ org }, testInfo) => {
  const { page } = org;
  await page.goto("/documents");
  const input = page.getByTestId("upload-input");
  const upload = (name: string) => page.getByTestId("upload-item").filter({ hasText: name });
  const refused = async (name: string, message: string | RegExp) => {
    await expect(upload(name)).toHaveAttribute("data-status", "error");
    await expect(upload(name)).toContainText(message);
  };

  await input.setInputFiles({
    name: "program.exe",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("MZ not a document"),
  });
  await refused("program.exe", "Unsupported file type");

  await input.setInputFiles({ name: "empty.txt", mimeType: "text/plain", buffer: Buffer.alloc(0) });
  await refused("empty.txt", "This file is empty.");

  await input.setInputFiles({
    name: "fake.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("plain text, not a PDF"),
  });
  await refused("fake.pdf", "not a valid PDF");

  await input.setInputFiles({
    name: "notes.txt",
    mimeType: "application/pdf",
    buffer: Buffer.from("a text file declared as a PDF"),
  });
  await refused("notes.txt", "declared content type does not match");

  await test.step("the same file twice is a duplicate, with a link to the first", async () => {
    await input.setInputFiles(travelPolicy);
    await expect(upload("Travel Policy.txt")).toHaveAttribute("data-status", "done");
    await input.setInputFiles(travelPolicy);
    const second = upload("Travel Policy.txt").nth(1);
    await expect(second).toHaveAttribute("data-status", "error");
    await expect(second).toContainText("This file already exists in the workspace.");
    await second.getByRole("link", { name: "View existing document" }).click();
    await expect(page.getByTestId("document-title")).toHaveText("Travel Policy");
  });

  await test.step("a 12 MB file passes through the proxy untouched, one over the limit is refused", async () => {
    // Playwright cannot hand files above 50 MB to the browser as buffers, so both go through the disk.
    const big = (name: string, megabytes: number) => {
      const path = testInfo.outputPath(name);
      writeFileSync(
        path,
        Buffer.concat([Buffer.from("Archive notes.\n"), Buffer.alloc(megabytes * 1024 * 1024 - 15, " ")]),
      );
      return path;
    };
    const twelve = big("twelve-mb.txt", 12);
    const tooLarge = big("fifty-one-mb.txt", 51);
    try {
      await page.goto("/documents");
      await input.setInputFiles(twelve);
      await expect(upload("twelve-mb.txt")).toHaveAttribute("data-status", "done", { timeout: LONG_TIMEOUT });
      const stored = (await org.api.get<{ items: Array<{ title: string; file_size: number }> }>("/documents?q=twelve"))
        .items[0]!;
      expect(stored.file_size).toBe(12 * 1024 * 1024); // the proxy neither truncated nor altered it

      await input.setInputFiles(tooLarge);
      await expect(upload("fifty-one-mb.txt")).toHaveAttribute("data-status", "error", { timeout: LONG_TIMEOUT });
      await expect(upload("fifty-one-mb.txt")).toContainText("larger than the upload limit");
    } finally {
      rmSync(twelve, { force: true });
      rmSync(tooLarge, { force: true });
    }
  });
});
