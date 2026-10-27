/** Friendly aliases for the generated OpenAPI types (schema.d.ts is the source of truth). */
import type { components, paths } from "./schema";

type S = components["schemas"];

export type Me = S["MeResponse"];
export type Capabilities = S["Capabilities"];
export type OrgMembership = S["OrganizationMembershipOut"];
export type WorkspaceAccess = S["WorkspaceAccessOut"];
export type User = S["UserOut"];
export type UserRef = S["UserRef"];
export type OrgRole = S["OrgRole"];
export type WorkspaceRole = S["WorkspaceRole"];

export type Workspace = S["WorkspaceOut"];
export type WorkspaceMember = S["WorkspaceMemberOut"];
export type OrgMember = S["MemberOut"];
export type Organization = S["OrganizationOut"];
export type Invitation = S["InvitationOut"];
export type InvitationCreated = S["InvitationCreated"];
export type InvitationPreview = S["InvitationPreview"];

export type DocumentItem = S["DocumentOut"];
export type DocumentDetail = S["DocumentDetail"];
export type DocumentStatus = S["DocumentStatus"];
export type DocumentVisibility = S["DocumentVisibility"];
export type DocumentPermission = S["DocumentPermissionOut"];
export type PermissionLevel = S["DocumentPermissionOut"]["permission"];
export type DocumentUploadResult = S["DocumentUploadResponse"];
export type DocumentStatusInfo = S["DocumentStatusOut"];
export type IngestionJob = S["IngestionJobOut"];
export type FileType = S["FileType"];
export type Chunk = S["ChunkOut"];
export type ChunkFlag = S["ChunkFlag"];
export type FlagRule = S["FlagRule"];
export type DocumentSort = S["DocumentSort"];
export type SortOrder = S["SortOrder"];

export type SearchRequest = S["SearchRequest"];
export type SearchResponse = S["SearchResponse"];
export type SearchResult = S["SearchResult"];
export type SearchMode = NonNullable<S["SearchRequest"]["mode"]>;

export type ChatRequest = S["ChatRequest"];
export type ChatResponse = S["ChatResponse"];
export type ChatScope = S["ChatScope"];
export type Conversation = S["ConversationOut"];
export type Message = S["MessageOut"];
export type SourceRef = S["SourceRef"];
export type Citation = S["CitationOut"];
export type Claim = S["ClaimOut"];
export type DocumentChange = S["DocumentChange"];
export type AnswerStatus = S["AnswerStatus"];
export type MessageStatus = S["MessageStatus"];
// The SSE event payloads (backend/app/schemas/chat.py: Stream*Event) are not part of the
// OpenAPI document because no route returns them as a response model, so they are mirrored here.
export type StreamMeta = {
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  search_query?: string | null;
};
export type StreamSources = { sources: SourceRef[] };
export type StreamToken = { text: string };
export type StreamDone = { message: Message };
export type StreamError = { code: string; message: string };

export type Dashboard = S["DashboardResponse"];
export type AuditEvent = S["AuditEventOut"];
export type Usage = S["UsageResponse"];
export type ProofRun = S["RunOut"];
export type ProofRunDetail = S["RunDetail"];
export type ProofRunCreate = S["RunCreate"];
export type ProofGroup = S["GroupOut"];
export type ProofFinding = S["FindingOut"];

export type Page<T> = { items: T[]; total: number; page: number; page_size: number };

type Query<P extends keyof paths, M extends keyof paths[P]> = paths[P][M] extends {
  parameters: { query?: infer Q };
}
  ? NonNullable<Q>
  : never;

export type DocumentListQuery = Query<"/api/v1/documents", "get">;
export type AuditQuery = Query<"/api/v1/admin/audit-events", "get">;
