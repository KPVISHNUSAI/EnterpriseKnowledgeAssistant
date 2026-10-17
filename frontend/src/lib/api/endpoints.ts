import type { components } from "./schema";
import { apiUrl, request } from "./client";
import type {
  AuditEvent,
  AuditQuery,
  ChatRequest,
  ChatResponse,
  Chunk,
  Conversation,
  Dashboard,
  DocumentDetail,
  DocumentItem,
  DocumentListQuery,
  DocumentStatusInfo,
  DocumentVisibility,
  Invitation,
  InvitationCreated,
  InvitationPreview,
  Me,
  Message,
  OrgMember,
  OrgRole,
  Organization,
  Page,
  PermissionLevel,
  SearchRequest,
  SearchResponse,
  Usage,
  User,
  Workspace,
  WorkspaceMember,
  WorkspaceRole,
} from "./types";

type S = components["schemas"];
type Paging = { page?: number; page_size?: number };

const seg = encodeURIComponent;

/** One typed function per endpoint. Request/response shapes come from schema.d.ts. */
export const api = {
  auth: {
    register: (body: S["RegisterRequest"]) => request<Me>("POST", "/auth/register", { json: body }),
    login: (body: S["LoginRequest"]) => request<Me>("POST", "/auth/login", { json: body }),
    logout: () => request<void>("POST", "/auth/logout"),
    me: (signal?: AbortSignal) => request<Me>("GET", "/auth/me", { signal }),
    updateProfile: (full_name: string) => request<User>("PATCH", "/auth/me", { json: { full_name } }),
    changePassword: (body: S["ChangePasswordRequest"]) => request<void>("POST", "/auth/me/password", { json: body }),
    switchOrganization: (organization_id: string) =>
      request<Me>("POST", "/auth/switch-organization", { json: { organization_id } }),
    previewInvitation: (token: string) => request<InvitationPreview>("GET", `/auth/invitations/${seg(token)}`),
    acceptInvitation: (token: string) => request<Me>("POST", "/auth/invitations/accept", { json: { token } }),
  },

  organization: {
    get: () => request<Organization>("GET", "/organization"),
    members: (query: Paging & { q?: string }) => request<Page<OrgMember>>("GET", "/organization/members", { query }),
    updateMemberRole: (userId: string, role: OrgRole) =>
      request<OrgMember>("PATCH", `/organization/members/${seg(userId)}`, { json: { role } }),
    removeMember: (userId: string) => request<void>("DELETE", `/organization/members/${seg(userId)}`),
    invitations: (query: Paging) => request<Page<Invitation>>("GET", "/organization/invitations", { query }),
    createInvitation: (body: { email: string; role: OrgRole }) =>
      request<InvitationCreated>("POST", "/organization/invitations", { json: body }),
    revokeInvitation: (id: string) => request<void>("DELETE", `/organization/invitations/${seg(id)}`),
  },

  workspaces: {
    list: () => request<Workspace[]>("GET", "/workspaces"),
    get: (id: string) => request<Workspace>("GET", `/workspaces/${seg(id)}`),
    create: (body: { name: string; description?: string | null }) =>
      request<Workspace>("POST", "/workspaces", { json: body }),
    update: (id: string, body: { name?: string; description?: string | null }) =>
      request<Workspace>("PATCH", `/workspaces/${seg(id)}`, { json: body }),
    remove: (id: string) => request<void>("DELETE", `/workspaces/${seg(id)}`),
    members: (id: string) => request<WorkspaceMember[]>("GET", `/workspaces/${seg(id)}/members`),
    addMember: (id: string, body: { user_id: string; role: WorkspaceRole }) =>
      request<WorkspaceMember>("POST", `/workspaces/${seg(id)}/members`, { json: body }),
    updateMember: (id: string, userId: string, role: WorkspaceRole) =>
      request<WorkspaceMember>("PATCH", `/workspaces/${seg(id)}/members/${seg(userId)}`, {
        json: { role },
      }),
    removeMember: (id: string, userId: string) =>
      request<void>("DELETE", `/workspaces/${seg(id)}/members/${seg(userId)}`),
  },

  documents: {
    list: (query: DocumentListQuery) => request<Page<DocumentItem>>("GET", "/documents", { query }),
    get: (id: string) => request<DocumentDetail>("GET", `/documents/${seg(id)}`),
    rename: (id: string, title: string) => request<DocumentItem>("PATCH", `/documents/${seg(id)}`, { json: { title } }),
    remove: (id: string) => request<void>("DELETE", `/documents/${seg(id)}`),
    reindex: (id: string) => request<DocumentStatusInfo>("POST", `/documents/${seg(id)}/reindex`),
    chunks: (id: string, query: Paging) => request<Page<Chunk>>("GET", `/documents/${seg(id)}/chunks`, { query }),
    updatePermissions: (
      id: string,
      body: {
        visibility: DocumentVisibility;
        users: Array<{ user_id: string; permission: PermissionLevel }>;
      },
    ) => request<DocumentDetail>("PUT", `/documents/${seg(id)}/permissions`, { json: body }),
  },

  search: (body: SearchRequest, signal?: AbortSignal) =>
    request<SearchResponse>("POST", "/search", { json: body, signal }),

  chat: {
    send: (body: ChatRequest, signal?: AbortSignal) => request<ChatResponse>("POST", "/chat", { json: body, signal }),
    conversations: (query: Paging) => request<Page<Conversation>>("GET", "/conversations", { query }),
    messages: (id: string, query: Paging) =>
      request<Page<Message>>("GET", `/conversations/${seg(id)}/messages`, { query }),
    deleteConversation: (id: string) => request<void>("DELETE", `/conversations/${seg(id)}`),
  },

  dashboard: () => request<Dashboard>("GET", "/dashboard"),

  admin: {
    auditEvents: (query: AuditQuery) => request<Page<AuditEvent>>("GET", "/admin/audit-events", { query }),
    usage: (days: number) => request<Usage>("GET", "/admin/usage", { query: { days } }),
  },
};

/** Same-origin URL of the original file (may redirect to a short-lived presigned URL). */
export function documentFileUrl(id: string, options: { inline?: boolean; page?: number } = {}): string {
  const url = apiUrl(`/documents/${seg(id)}/download`, options.inline ? { inline: true } : undefined);
  return options.page && options.page > 0 ? `${url}#page=${options.page}` : url;
}
