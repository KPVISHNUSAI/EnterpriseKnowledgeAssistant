import type { DocumentItem, Me, SourceRef, Workspace } from "@/lib/api/types";

let counter = 0;
const uuid = () => `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`;

export function makeMe(overrides: Partial<Me> = {}): Me {
  const org = { id: uuid(), name: "Acme Corp", role: "org_admin" as const };
  return {
    user: {
      id: uuid(),
      email: "ada@acme.test",
      full_name: "Ada Admin",
      created_at: "2026-01-01T00:00:00Z",
      last_login_at: null,
    },
    organization: org,
    organizations: [org],
    workspaces: [],
    capabilities: {
      can_create_workspace: true,
      can_manage_organization: true,
      can_view_audit_log: true,
      can_upload: true,
    },
    ...overrides,
  };
}

export function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: uuid(),
    name: "Policies",
    description: null,
    created_at: "2026-01-01T00:00:00Z",
    my_role: "owner",
    member_count: 2,
    document_count: 3,
    can_manage: true,
    can_upload: true,
    ...overrides,
  };
}

export function makeDocument(overrides: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: uuid(),
    workspace_id: uuid(),
    workspace_name: "Policies",
    title: "Employee handbook",
    original_filename: "handbook.pdf",
    file_type: "pdf",
    mime_type: "application/pdf",
    file_size: 2048,
    content_hash: "a".repeat(64),
    version: 1,
    processing_status: "indexed",
    chunk_count: 12,
    visibility: "workspace",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    can_manage: true,
    flagged_chunks: 0,
    flagged_text_included: false,
    ...overrides,
  };
}

export function makeSource(id: string, overrides: Partial<SourceRef> = {}): SourceRef {
  return {
    source_id: id,
    chunk_id: uuid(),
    document_id: uuid(),
    document_title: "Employee handbook",
    workspace_id: uuid(),
    file_type: "pdf",
    page_number: 4,
    section_title: "Leave",
    excerpt: "Employees accrue 1.5 days of paid leave per month.",
    relevance: { rerank_score: 0.91, vector_similarity: 0.8 },
    cited: true,
    redacted: false,
    ...overrides,
  };
}
