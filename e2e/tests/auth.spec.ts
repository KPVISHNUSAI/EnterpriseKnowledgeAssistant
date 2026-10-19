import { newAccount, PASSWORD } from "../support/accounts";
import { expect, test } from "../support/test";
import { newBrowserSession, toast } from "../support/ui";
import { FRONTEND_ORIGIN } from "../stack/config";

test("register an organization, sign out, reject a wrong password, sign in again", async ({ page, context }) => {
  const account = newAccount();

  await test.step("register through the form", async () => {
    await page.goto("/register");
    await page.getByTestId("register-name").fill(account.fullName);
    await page.getByTestId("register-email").fill(account.email);
    await page.getByTestId("register-password").fill(account.password);
    await page.getByTestId("register-org").fill(account.organization);
    await page.getByTestId("register-submit").click();

    await expect(page).toHaveURL(/\/dashboard$/);
    await expect(page.getByRole("heading", { level: 1, name: "Welcome back, Ada" })).toBeVisible();
    await expect(page.getByTestId("org-switcher")).toContainText(account.organization);
    // A new organization starts with its "General" workspace and no documents.
    await expect(page.getByTestId("dashboard-stats")).toContainText("Workspaces1");
    await expect(page.getByText("No documents yet")).toBeVisible();
  });

  await test.step("the session cookie is HttpOnly and the CSRF cookie is readable", async () => {
    const cookies = await context.cookies();
    expect(cookies.find((c) => c.name === "eka_session")).toMatchObject({ httpOnly: true, sameSite: "Lax" });
    expect(cookies.find((c) => c.name === "eka_csrf")).toMatchObject({ httpOnly: false });
  });

  await test.step("sign out", async () => {
    const copiedCookies = await context.cookies();
    await page.getByTestId("user-menu").click();
    await page.getByTestId("sign-out").click();
    await expect(page).toHaveURL(/\/login$/);

    // The server revoked the session itself: a copy of the old cookies is worth nothing.
    await context.addCookies(copiedCookies);
    expect((await page.request.get("/api/v1/auth/me")).status()).toBe(401);
    await context.clearCookies();

    // The session is gone: a protected page sends the visitor back to the sign-in form.
    await page.goto("/documents");
    await expect(page).toHaveURL(/\/login\?next=%2Fdocuments$/);
  });

  await test.step("a wrong password is rejected", async () => {
    await page.getByTestId("login-email").fill(account.email);
    await page.getByTestId("login-password").fill(`${PASSWORD}-wrong`);
    await page.getByTestId("login-submit").click();

    await expect(page.getByTestId("login-error")).toContainText("Invalid email or password");
    await expect(page).toHaveURL(/\/login\?next=/);
  });

  await test.step("the right password signs in and returns to the page that was asked for", async () => {
    await page.getByTestId("login-password").fill(account.password);
    await page.getByTestId("login-submit").click();

    await expect(page).toHaveURL(/\/documents$/);
    await expect(page.getByRole("heading", { level: 1, name: "Documents" })).toBeVisible();
  });
});

test("registration refuses a weak password and an email that is already registered", async ({ page }) => {
  const account = newAccount();

  await page.goto("/register");
  await page.getByTestId("register-name").fill(account.fullName);
  await page.getByTestId("register-email").fill(account.email);
  await page.getByTestId("register-org").fill(account.organization);

  await page.getByTestId("register-password").fill("short");
  await page.getByTestId("register-submit").click();
  await expect(page.getByRole("alert").filter({ hasText: /at least 12 characters/i })).toBeVisible();
  await expect(page).toHaveURL(/\/register$/);

  await page.getByTestId("register-password").fill(account.password);
  await page.getByTestId("register-submit").click();
  await expect(page).toHaveURL(/\/dashboard$/);

  // Same email again, from a signed-out browser.
  await page.context().clearCookies();
  await page.goto("/register");
  await page.getByTestId("register-name").fill(account.fullName);
  await page.getByTestId("register-email").fill(account.email);
  await page.getByTestId("register-password").fill(account.password);
  await page.getByTestId("register-org").fill(`${account.organization} again`);
  await page.getByTestId("register-submit").click();
  await expect(page.getByTestId("register-error")).toContainText("already exists");
  await expect(page).toHaveURL(/\/register$/);
});

test("changing the password signs out the other sessions and retires the old password", async ({ browser }) => {
  const account = newAccount();
  const newPassword = "Another-Correct-Horse-9";
  const first = await newBrowserSession(browser);
  await first.api.register(account);

  // A second browser signs in with the same account.
  const second = await newBrowserSession(browser);
  await second.page.goto("/login");
  await second.page.getByTestId("login-email").fill(account.email);
  await second.page.getByTestId("login-password").fill(account.password);
  await second.page.getByTestId("login-submit").click();
  await expect(second.page).toHaveURL(/\/dashboard$/);

  await test.step("the password is changed in the first browser", async () => {
    await first.page.goto("/settings");
    await first.page.getByTestId("current-password").fill(account.password);
    await first.page.getByTestId("new-password").fill(newPassword);
    await first.page.getByTestId("confirm-password").fill(newPassword);
    await first.page.getByTestId("password-save").click();
    await expect(toast(first.page, "Your other sessions were signed out")).toBeVisible();
  });

  await test.step("the other browser is signed out, this one stays signed in", async () => {
    await second.page.goto("/documents");
    await expect(second.page).toHaveURL(/\/login\?next=%2Fdocuments$/);
    await first.page.goto("/documents");
    await expect(first.page.getByRole("heading", { level: 1, name: "Documents" })).toBeVisible();
  });

  await test.step("only the new password signs in", async () => {
    await second.page.getByTestId("login-email").fill(account.email);
    await second.page.getByTestId("login-password").fill(account.password);
    await second.page.getByTestId("login-submit").click();
    await expect(second.page.getByTestId("login-error")).toContainText("Invalid email or password");
    await second.page.getByTestId("login-password").fill(newPassword);
    await second.page.getByTestId("login-submit").click();
    await expect(second.page).toHaveURL(/\/documents$/);
  });

  await first.context.close();
  await second.context.close();
});

test("the profile name can be changed", async ({ org }) => {
  const { page } = org;
  await page.goto("/settings");
  await expect(page.getByLabel("Email")).toHaveValue(org.account.email);
  await expect(page.getByLabel("Email")).toBeDisabled();

  await page.getByTestId("profile-name").fill("Grace Hopper");
  await page.getByTestId("profile-save").click();
  await expect(toast(page, "Profile updated")).toBeVisible();
  await expect(page.getByTestId("user-menu")).toHaveAccessibleName("Account menu for Grace Hopper");

  await page.goto("/dashboard");
  await expect(page.getByRole("heading", { level: 1, name: "Welcome back, Grace" })).toBeVisible();
});

test("the address to return to after signing in cannot leave the site", async ({ browser }) => {
  const account = newAccount();
  const { page, api, context } = await newBrowserSession(browser);
  await api.register(account);
  await context.clearCookies(); // registered, but signed out
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });

  const signIn = async (next: string) => {
    await page.goto(`/login?next=${encodeURIComponent(next)}`);
    await page.getByTestId("login-email").fill(account.email);
    await page.getByTestId("login-password").fill(account.password);
    await page.getByTestId("login-submit").click();
  };

  await test.step("a relative path is followed", async () => {
    await signIn("/search?q=travel");
    await expect(page).toHaveURL(/\/search\?q=travel$/);
  });

  await test.step("anything else lands on the dashboard (the visitor is already signed in, so it redirects at once)", async () => {
    const hostile = [
      "https://evil.example/phish",
      "//evil.example/phish",
      "/\\evil.example",
      "javascript:alert(1)",
      "/login",
      "/register",
    ];
    for (const next of hostile) {
      await page.goto(`/login?next=${encodeURIComponent(next)}`);
      await expect(page, next).toHaveURL(new RegExp(`^${FRONTEND_ORIGIN}/dashboard$`));
    }
  });

  await test.step("the sign-in form itself does not follow them either", async () => {
    await context.clearCookies();
    await signIn("https://evil.example/phish");
    await expect(page).toHaveURL(new RegExp(`^${FRONTEND_ORIGIN}/dashboard$`));
  });

  expect(dialogs).toEqual([]);
  await context.close();
});
