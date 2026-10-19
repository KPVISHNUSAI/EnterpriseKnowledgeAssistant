import type { Page } from "@playwright/test";
import { expect, test } from "../support/test";
import { newAccount } from "../support/accounts";
import { remoteWorkPolicy, travelPolicy } from "../support/documents";
import { ask, expectIndexedInLibrary, invite, newBrowserSession, uploadFile } from "../support/ui";

const auditRows = (page: Page) => page.getByTestId("audit-row");
/** The action codes of the rows currently listed, newest first. */
const actions = (page: Page) => auditRows(page).locator("code").allTextContents();
/** The big number of a usage card, without its hint. */
const stat = (page: Page, label: string) =>
  page.getByTestId("usage-totals").locator(`div:has(> dt:text-is("${label}")) > dd > span:first-child`);

test("the audit log lists the actions, its filters narrow them, and usage shows totals", async ({ org, browser }) => {
  const { page } = org;
  const eli = newAccount("employee");

  await test.step("an admin uploads two documents, deletes one, invites a colleague and asks a question", async () => {
    await uploadFile(page, remoteWorkPolicy);
    const travelId = await uploadFile(page, travelPolicy);
    await expectIndexedInLibrary(page, "Remote Work Policy");
    await expectIndexedInLibrary(page, "Travel Policy");

    await page.goto(`/documents/${travelId}`);
    await page.getByRole("button", { name: "Delete", exact: true }).click();
    await page.getByTestId("confirm-action").click();
    await expect(page).toHaveURL(/\/documents$/);
    await expect(page.getByTestId("document-row")).toHaveCount(1);

    const link = await invite(page, eli.email);
    // The colleague accepts (registering through the link is covered by permissions.spec.ts).
    const colleague = await newBrowserSession(browser);
    await colleague.api.register(eli, link.split("/").pop());
    await colleague.context.close();

    await page.goto("/chat");
    await ask(page, "How many days per week may employees work remotely?");
  });

  await page.goto("/admin?tab=audit");
  await expect(page.getByRole("tab", { name: "Audit log" })).toHaveAttribute("aria-selected", "true");

  await test.step("every performed action is listed, newest first", async () => {
    await expect
      .poll(() => actions(page))
      .toEqual(
        expect.arrayContaining([
          "document.uploaded",
          "document.deleted",
          "organization.invitation_created",
          "organization.invitation_accepted",
          "auth.register",
          "workspace.created",
        ]),
      );
    const listed = await actions(page);
    expect(listed.filter((a) => a === "document.uploaded")).toHaveLength(2);
    expect(listed.indexOf("organization.invitation_created")).toBeLessThan(listed.indexOf("document.deleted"));
    expect(listed.indexOf("document.deleted")).toBeLessThan(listed.indexOf("document.uploaded"));
    await expect(auditRows(page).filter({ hasText: "document.deleted" })).toContainText(org.account.email);
  });

  await test.step("filter by action prefix", async () => {
    await page.getByTestId("audit-action").fill("document.*");
    await expect.poll(async () => (await actions(page)).every((a) => a.startsWith("document."))).toBe(true);
    const listed = await actions(page);
    expect(listed).toEqual(expect.arrayContaining(["document.uploaded", "document.deleted"]));
    expect(listed).not.toContain("organization.invitation_created");
  });

  await test.step("filter by exact action, then read its details", async () => {
    await page.getByTestId("audit-action").fill("organization.invitation_created");
    await expect(auditRows(page)).toHaveCount(1);
    await page.getByRole("button", { name: "Show details for organization.invitation_created" }).click();
    const details = page.getByRole("row").filter({ hasText: "Metadata" });
    await expect(details).toContainText(`"email": "${eli.email}"`);
    await expect(details).toContainText('"role": "employee"');
  });

  await test.step("filter by resource type", async () => {
    await page.getByTestId("audit-action").fill("");
    await page.getByLabel("Resource type").fill("invitation");
    await expect
      .poll(async () => (await actions(page)).sort())
      .toEqual(["organization.invitation_accepted", "organization.invitation_created"]);
  });

  await test.step("filter by actor", async () => {
    await page.getByRole("button", { name: "Clear filters" }).click();
    await page.getByTestId("audit-actor").click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: new RegExp(eli.email) })
      .click();
    await expect
      .poll(async () => (await actions(page)).sort())
      .toEqual(["auth.register", "organization.invitation_accepted"]);
    await expect(auditRows(page).filter({ hasNotText: eli.email })).toHaveCount(0);
  });

  await test.step("filter by time", async () => {
    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect.poll(async () => (await actions(page)).length).toBeGreaterThan(5);
    await page.getByLabel("From").fill("2099-01-01T00:00");
    await expect(page.getByText("No audit events found")).toBeVisible();
    await page.getByLabel("From").fill("2000-01-01T00:00");
    await expect.poll(async () => (await actions(page)).length).toBeGreaterThan(5);
    await page.getByLabel("Until").fill("2000-01-02T00:00");
    await expect(page.getByText("No audit events found")).toBeVisible();
  });

  await test.step("usage shows the totals of the organization", async () => {
    await page.goto("/admin?tab=usage");
    await expect(stat(page, "Documents")).toHaveText("1");
    await expect(stat(page, "Conversations")).toHaveText("1");
    await expect(stat(page, "Chat requests")).toHaveText("1");
    await expect(stat(page, "Storage")).not.toHaveText("0 B");
    await expect(
      page.getByTestId("usage-totals").getByTestId("document-status").and(page.locator('[data-status="indexed"]')),
    ).toBeVisible();
    await expect(page.getByTestId("latency-table")).toContainText("Retrieval");
    await expect(page.getByTestId("latency-table")).toContainText(/\d+ ms/);
    await expect(page.getByTestId("ingestion-health")).toContainText("Failed (24 h)0");

    await page.getByLabel("Period").selectOption({ label: "Last 7 days" });
    await expect(page.getByTestId("usage-totals")).toContainText("last 7 days");
  });
});

test("a member who is not an organization admin cannot use the admin area", async ({ org, browser }) => {
  const eli = newAccount("employee");
  const employee = await newBrowserSession(browser);
  await employee.api.register(eli, (await org.api.invite(eli.email)).token);

  const { page } = employee;
  await page.goto("/dashboard");
  await expect(page.getByTestId("nav-dashboard")).toBeVisible();
  await expect(page.getByTestId("nav-admin")).toHaveCount(0);

  for (const url of ["/admin", "/admin?tab=audit", "/admin?tab=members"]) {
    await page.goto(url);
    await expect(page.getByRole("heading", { name: "Administrators only" })).toBeVisible();
    await expect(page.getByTestId("audit-table")).toHaveCount(0);
    await expect(page.getByTestId("invite-open")).toHaveCount(0);
  }

  // The API refuses as well, whatever the UI shows.
  for (const path of ["/admin/audit-events", "/admin/usage", "/organization/invitations"]) {
    expect((await employee.api.getRaw(path)).status(), path).toBe(403);
  }
  await employee.context.close();
});
