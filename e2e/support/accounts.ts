import { randomUUID } from "node:crypto";

/** The API rejects passwords shorter than 12 characters or containing the email's local part. */
export const PASSWORD = "Correct-Horse-Battery-9";

export interface Account {
  fullName: string;
  email: string;
  password: string;
  organization: string;
}

/** A person (and, for admins, the organization they would create) that exists nowhere yet. */
export function newAccount(kind: "admin" | "employee" = "admin"): Account {
  const id = randomUUID().slice(0, 8);
  return {
    fullName: kind === "admin" ? "Ada Admin" : "Eli Employee",
    email: `e2e-${kind}-${id}@example.com`,
    password: PASSWORD,
    organization: `E2E Org ${id}`,
  };
}

/** A name that is unique per call, for workspaces and the like. */
export const uniqueName = (prefix: string) => `${prefix} ${randomUUID().slice(0, 6)}`;
