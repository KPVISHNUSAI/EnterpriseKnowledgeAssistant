import { test as base, expect, type BrowserContext, type Page } from "@playwright/test";
import { newAccount, type Account } from "./accounts";
import { Api, type Me } from "./api";

export interface Org {
  account: Account;
  context: BrowserContext;
  page: Page;
  api: Api;
  me: Me;
  /** The "General" workspace every new organization starts with. */
  workspaceId: string;
}

/**
 * `org`: a brand-new organization whose admin is already signed in on `page`. The account is
 * created through the API (registering through the UI is covered by auth.spec.ts), so tests
 * never share data and can run in any order.
 */
export const test = base.extend<{ org: Org }>({
  org: async ({ context, page }, use) => {
    const api = new Api(context);
    const account = newAccount("admin");
    const me = await api.register(account);
    await use({ account, context, page, api, me, workspaceId: me.workspaces[0]!.id });
  },
});

export { expect };
