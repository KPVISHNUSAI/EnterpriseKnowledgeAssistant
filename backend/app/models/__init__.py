from app.db.base import Base
from app.models.audit import AuditEvent, UsageKind, UsageRecord
from app.models.conversation import Conversation, Message, MessageRole, MessageStatus
from app.models.document import (
    ACTIVE_JOB_STATUSES,
    Document,
    DocumentChunk,
    DocumentPermission,
    DocumentStatus,
    IngestionJob,
    JobStatus,
    PermissionLevel,
    PrincipalType,
)
from app.models.organization import Invitation, Organization, OrganizationMembership, OrgRole
from app.models.user import User, UserSession
from app.models.workspace import Workspace, WorkspaceMembership, WorkspaceRole

# Tables protected by row-level security (all carry organization_id).
TENANT_TABLES = (
    "workspaces",
    "workspace_memberships",
    "documents",
    "document_permissions",
    "document_chunks",
    "ingestion_jobs",
    "conversations",
    "messages",
    "audit_events",
    "usage_records",
)

__all__ = [
    "ACTIVE_JOB_STATUSES",
    "TENANT_TABLES",
    "AuditEvent",
    "Base",
    "Conversation",
    "Document",
    "DocumentChunk",
    "DocumentPermission",
    "DocumentStatus",
    "IngestionJob",
    "Invitation",
    "JobStatus",
    "Message",
    "MessageRole",
    "MessageStatus",
    "OrgRole",
    "Organization",
    "OrganizationMembership",
    "PermissionLevel",
    "PrincipalType",
    "UsageKind",
    "UsageRecord",
    "User",
    "UserSession",
    "Workspace",
    "WorkspaceMembership",
    "WorkspaceRole",
]
