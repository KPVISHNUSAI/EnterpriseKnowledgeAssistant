import { expect, test } from "../support/test";
import { LONG_TIMEOUT, type Me } from "../support/api";
import { newAccount, uniqueName } from "../support/accounts";
import { compensationMemo } from "../support/documents";
import { createWorkspace, expectIndexedInLibrary, invite, newBrowserSession, uploadFile } from "../support/ui";

test("the leak test passes on the real permissions and catches a simulated mis-share", async ({ org, browser }) => {
  const admin = org.page;
  const board = uniqueName("Board");
  const eli = newAccount("employee");
  const colleague = await newBrowserSession(browser);

  await test.step("a board paper the employee, who is in no workspace, cannot read", async () => {
    await createWorkspace(admin, board);
    await uploadFile(admin, compensationMemo, { workspace: board });
    await expectIndexedInLibrary(admin, "Executive Compensation Memo");
    const link = await invite(admin, eli.email);
    await colleague.api.register(eli, link.split("/").pop());
  });

  const report = admin.getByTestId("proof-report");
  const verdict = admin.getByTestId("proof-verdict");

  await test.step("a run over the real permissions passes", async () => {
    await admin.goto("/admin?tab=proof");
    await expect(admin.getByText("No leak test yet")).toBeVisible();
    await admin.getByTestId("proof-run").click();
    await expect(report).toHaveAttribute("data-verdict", "passed", {
      timeout: LONG_TIMEOUT,
    });
    await expect(verdict).toContainText(/No leak found in [1-9][\d,]* questions? asked as 2 access groups/);
    await expect(admin.getByTestId("proof-no-findings")).toBeVisible();
    await expect(admin.getByTestId("proof-group-row").filter({ hasText: eli.fullName })).toBeVisible();
  });

  await test.step("a simulation that wrongly gives the employee the board workspace is caught", async () => {
    await admin.getByRole("button", { name: "All leak tests" }).click();
    await admin.getByTestId("proof-simulate-open").click();
    const dialog = admin.getByRole("dialog", { name: "Simulate a mis-share" });
    await dialog.getByTestId("proof-person").click();
    await admin.getByRole("textbox", { name: "Search people" }).fill(eli.email);
    await admin.getByTestId("user-picker-results").getByRole("button", { name: eli.fullName }).click();
    await dialog.getByTestId("proof-workspace").selectOption({ label: board });
    await dialog.getByTestId("proof-simulate-submit").click();

    await expect(report).toHaveAttribute("data-verdict", "failed", {
      timeout: LONG_TIMEOUT,
    });
    await expect(verdict).toContainText("Caught");
    await expect(verdict).toContainText(`${eli.fullName}, who was treated as wrongly given ${board}`);
    // The admin owns the board workspace, so the document is named; the report holds no text.
    const findings = admin.getByTestId("proof-finding-row");
    await expect(findings.filter({ hasText: "Its text was in the model request" })).toContainText(
      "Executive Compensation Memo",
    );
    await expect(report).not.toContainText("bonus pool");
  });

  await test.step("the simulation is in the history but never the latest result", async () => {
    await admin.getByRole("button", { name: "All leak tests" }).click();
    await expect(admin.getByTestId("proof-run-row")).toHaveCount(2);
    await expect(admin.getByTestId("proof-run-row").first()).toHaveAttribute("data-kind", "simulation");
    await expect(admin.getByTestId("proof-latest")).toContainText("Latest result: Passed");
  });

  await test.step("the employee's real access is unchanged", async () => {
    expect((await colleague.api.get<Me>("/auth/me")).workspaces).toEqual([]);
    await colleague.context.close();
  });
});
