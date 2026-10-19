import { uniqueName } from "../support/accounts";
import { travelPolicy } from "../support/documents";
import { expect, test } from "../support/test";
import { createWorkspace, toast } from "../support/ui";

test("a workspace can be created, renamed and deleted once it is empty", async ({ org }) => {
  const { page } = org;
  const name = uniqueName("Marketing");
  const renamed = `${name} Team`;

  const id = await createWorkspace(page, name);
  await expect(page.getByText("1 member", { exact: true })).toBeVisible();

  await test.step("a name that is taken is refused, whatever its case", async () => {
    await page.goto("/workspaces");
    await page.getByTestId("new-workspace").click();
    await page.getByTestId("workspace-name").fill(name.toUpperCase());
    await page.getByTestId("workspace-save").click();
    await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
      "A workspace with this name already exists",
    );
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    await expect(page.getByTestId("workspace-list").getByRole("heading", { name })).toHaveCount(1);
  });

  await test.step("rename and describe it", async () => {
    await page.goto(`/workspaces/${id}`);
    await page.getByRole("button", { name: "Edit" }).click();
    await page.getByTestId("workspace-name").fill(renamed);
    await page.getByTestId("workspace-description").fill("Campaign material");
    await page.getByTestId("workspace-save").click();
    await expect(toast(page, "Workspace updated")).toBeVisible();
    await expect(page.getByTestId("workspace-title")).toHaveText(renamed);
    await page.goto("/workspaces");
    const card = page.getByTestId("workspace-list").getByRole("listitem").filter({ hasText: renamed });
    await expect(card).toContainText("Campaign material");
    await expect(card).toContainText("0 documents");
  });

  const document = await org.api.upload({ workspaceId: id, ...travelPolicy });

  await test.step("a workspace that still holds documents is not deleted", async () => {
    await page.goto(`/workspaces/${id}`);
    await expect(page.getByText("1 document", { exact: true })).toBeVisible();
    await page.getByTestId("delete-workspace").click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("still contains documents");
    await dialog.getByTestId("confirm-action").click();
    await expect(page.getByTestId("toast-error")).toContainText("Delete the workspace's documents first");
    await expect(page.getByTestId("workspace-title")).toHaveText(renamed);
  });

  await test.step("an empty workspace is deleted after confirmation", async () => {
    expect((await org.api.send("DELETE", `/documents/${document.id}`)).status()).toBe(204);
    await page.goto(`/workspaces/${id}`);
    await page.getByTestId("delete-workspace").click();
    await expect(page.getByRole("alertdialog")).toContainText("permanently deleted");
    await page.getByTestId("confirm-action").click();
    await expect(toast(page, "Workspace deleted")).toBeVisible(); // it only lasts 5 s, the page may take longer
    await expect(page).toHaveURL(/\/workspaces$/);
    await expect(page.getByTestId("workspace-list")).not.toContainText(renamed);
    await page.goto(`/workspaces/${id}`);
    await expect(page.getByRole("heading", { name: "Workspace not found" })).toBeVisible();
  });
});
