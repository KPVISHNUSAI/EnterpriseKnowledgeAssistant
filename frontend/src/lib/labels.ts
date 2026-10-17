import type { AnswerStatus, DocumentStatus, FileType, OrgRole, WorkspaceRole } from "./api/types";

export const ORG_ROLE_LABELS: Record<OrgRole, string> = {
  org_admin: "Organization admin",
  knowledge_manager: "Knowledge manager",
  employee: "Employee",
};

export const ORG_ROLES: OrgRole[] = ["org_admin", "knowledge_manager", "employee"];

export const WORKSPACE_ROLE_LABELS: Record<WorkspaceRole, string> = {
  owner: "Owner",
  editor: "Editor",
  viewer: "Viewer",
};

export const WORKSPACE_ROLES: WorkspaceRole[] = ["owner", "editor", "viewer"];

export const DOCUMENT_STATUS_LABELS: Record<DocumentStatus, string> = {
  queued: "Queued",
  processing: "Processing",
  indexed: "Indexed",
  failed: "Failed",
};

export const DOCUMENT_STATUSES: DocumentStatus[] = ["queued", "processing", "indexed", "failed"];

export const isActiveStatus = (status: DocumentStatus) => status === "queued" || status === "processing";

export const ANSWER_STATUS_LABELS: Record<AnswerStatus, string> = {
  answered: "Answered",
  partial: "Partial answer",
  insufficient_evidence: "Not enough evidence",
  unverified: "Unverified answer",
  refused: "Declined",
  error: "Error",
};

export const FILE_TYPES: FileType[] = ["pdf", "docx", "txt", "md"];
