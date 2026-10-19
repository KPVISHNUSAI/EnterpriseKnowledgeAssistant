import { expect, type APIResponse, type BrowserContext } from "@playwright/test";
import type { Account } from "./accounts";

/** For what the ingestion worker or a big transfer does: milliseconds on an idle machine, much longer on a busy one. */
export const LONG_TIMEOUT = 180_000;

/** The few response fields the tests read; the API has many more. */
export interface Me {
  user: { id: string; email: string; full_name: string };
  organization: { id: string; name: string };
  workspaces: Array<{ id: string; name: string; role: string }>;
}

export interface ApiDocument {
  id: string;
  title: string;
  version: number;
  file_size: number;
  processing_status: string;
  indexed_at: string | null;
  chunk_count: number;
  can_manage: boolean;
}

export interface UploadInput {
  workspaceId: string;
  name: string;
  mimeType: string;
  buffer: Buffer;
  visibility?: "workspace" | "restricted";
}

export interface ApiMessage {
  role: "user" | "assistant";
  content: string;
  status: string;
  answer_status: string | null;
  sources: Array<{ source_id: string; document_id: string; document_title: string; cited: boolean }> | null;
}

/**
 * The JSON API through the frontend's /api proxy, using the cookies of one browser context.
 * Tests use it for setup that another test already covers through the UI (registering, uploading,
 * waiting for indexing) and for reading state back (the stored messages of a conversation).
 */
export class Api {
  constructor(private readonly context: BrowserContext) {}

  private async csrfHeaders(): Promise<Record<string, string>> {
    const cookie = (await this.context.cookies()).find((c) => c.name === "eka_csrf");
    return cookie ? { "X-CSRF-Token": cookie.value } : {};
  }

  private async body<T>(response: APIResponse): Promise<T> {
    if (!response.ok()) {
      throw new Error(`${response.url()} -> ${response.status()}: ${await response.text()}`);
    }
    return (response.status() === 204 ? undefined : await response.json()) as T;
  }

  get<T>(path: string): Promise<T> {
    return this.context.request.get(`/api/v1${path}`).then((r) => this.body<T>(r));
  }

  async post<T>(path: string, data: unknown): Promise<T> {
    const headers = await this.csrfHeaders();
    return this.body<T>(await this.context.request.post(`/api/v1${path}`, { data, headers }));
  }

  /** The raw response, for tests that assert on status codes. */
  async getRaw(path: string): Promise<APIResponse> {
    return this.context.request.get(`/api/v1${path}`);
  }

  /** Any request with this browser's CSRF token; the raw response is returned. */
  async send(method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE", path: string, data?: unknown): Promise<APIResponse> {
    const headers = await this.csrfHeaders();
    return this.context.request.fetch(`/api/v1${path}`, { method, data, headers });
  }

  async register(account: Account, inviteToken?: string): Promise<Me> {
    return this.post<Me>("/auth/register", {
      full_name: account.fullName,
      email: account.email,
      password: account.password,
      ...(inviteToken ? { invite_token: inviteToken } : { organization_name: account.organization }),
    });
  }

  async createWorkspace(name: string): Promise<{ id: string; name: string }> {
    return this.post("/workspaces", { name });
  }

  async upload({ workspaceId, name, mimeType, buffer, visibility = "workspace" }: UploadInput): Promise<ApiDocument> {
    const headers = await this.csrfHeaders();
    const response = await this.context.request.post("/api/v1/documents/upload", {
      headers,
      multipart: { workspace_id: workspaceId, visibility, file: { name, mimeType, buffer } },
    });
    return (await this.body<{ document: ApiDocument }>(response)).document;
  }

  document(id: string): Promise<ApiDocument> {
    return this.get(`/documents/${id}`);
  }

  /** Polls the ingestion status; the worker needs a moment even for tiny files. */
  async waitUntilIndexed(documentId: string, timeout = LONG_TIMEOUT): Promise<void> {
    await expect
      .poll(
        async () =>
          (await this.get<{ processing_status: string }>(`/documents/${documentId}/status`)).processing_status,
        {
          message: `document ${documentId} should be indexed`,
          timeout,
          intervals: [250, 500, 1_000],
        },
      )
      .toBe("indexed");
  }

  async invite(
    email: string,
    role: "employee" | "knowledge_manager" | "org_admin" = "employee",
  ): Promise<{ id: string; token: string; invite_url: string }> {
    return this.post("/organization/invitations", { email, role });
  }

  async addWorkspaceMember(workspaceId: string, userId: string, role: "viewer" | "editor" | "owner" = "viewer") {
    return this.post(`/workspaces/${workspaceId}/members`, { user_id: userId, role });
  }

  messages(conversationId: string): Promise<{ items: ApiMessage[] }> {
    return this.get(`/conversations/${conversationId}/messages`);
  }
}
