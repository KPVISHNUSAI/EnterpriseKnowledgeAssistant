"""Home dashboard summary, limited to what the caller can see."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Conversation, Document, DocumentStatus, Message, User, Workspace
from app.schemas.chat import ConversationOut
from app.schemas.common import PageParams
from app.schemas.dashboard import ActivityItem, DashboardResponse
from app.schemas.documents import DocumentSort, SortOrder
from app.services import documents
from app.services.access import Principal, readable_documents_clause
from app.services.audit import AuditAction

_RECENT_DOCUMENTS = 5
_RECENT_CONVERSATIONS = 5
_RECENT_ACTIVITY = 10
_MAX_TITLE_CHARS = 80

# "{document}" becomes the quoted title from the event metadata (never document text).
_PHRASES: dict[str, str] = {
    AuditAction.AUTH_REGISTER: "Created an account",
    AuditAction.AUTH_LOGIN: "Signed in",
    AuditAction.AUTH_LOGIN_FAILED: "Failed sign-in attempt",
    AuditAction.AUTH_LOGOUT: "Signed out",
    AuditAction.AUTH_PASSWORD_CHANGED: "Changed their password",
    AuditAction.AUTH_ORGANIZATION_SWITCHED: "Switched organization",
    AuditAction.ORG_MEMBER_ROLE_CHANGED: "Changed a member's role",
    AuditAction.ORG_MEMBER_REMOVED: "Removed a member from the organization",
    AuditAction.ORG_INVITATION_CREATED: "Invited someone to the organization",
    AuditAction.ORG_INVITATION_REVOKED: "Revoked an invitation",
    AuditAction.ORG_INVITATION_ACCEPTED: "Joined the organization",
    AuditAction.WORKSPACE_CREATED: "Created a workspace",
    AuditAction.WORKSPACE_UPDATED: "Updated a workspace",
    AuditAction.WORKSPACE_DELETED: "Deleted a workspace",
    AuditAction.WORKSPACE_MEMBER_ADDED: "Added a workspace member",
    AuditAction.WORKSPACE_MEMBER_ROLE_CHANGED: "Changed a workspace member's role",
    AuditAction.WORKSPACE_MEMBER_REMOVED: "Removed a workspace member",
    AuditAction.DOCUMENT_UPLOADED: "Uploaded {document}",
    AuditAction.DOCUMENT_UPDATED: "Updated {document}",
    AuditAction.DOCUMENT_REPLACED: "Uploaded a new version of {document}",
    AuditAction.DOCUMENT_DELETED: "Deleted {document}",
    AuditAction.DOCUMENT_REINDEX_REQUESTED: "Requested re-indexing of {document}",
    AuditAction.DOCUMENT_PERMISSIONS_CHANGED: "Changed who can access {document}",
    AuditAction.DOCUMENT_DOWNLOADED: "Downloaded {document}",
    AuditAction.CONVERSATION_DELETED: "Deleted a conversation",
    AuditAction.ACCESS_DENIED: "Was denied access",
}


def summarize(action: str, metadata: dict[str, Any]) -> str:
    """One short human line per audit event."""
    phrase = _PHRASES.get(action)
    if phrase is None:  # an action added later: "workspace.archived" -> "Workspace: archived"
        return action.replace(".", ": ", 1).replace("_", " ").capitalize()
    title = metadata.get("title")
    if not isinstance(title, str) or not title:
        return phrase.replace("{document}", "a document")
    if len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
    return phrase.replace("{document}", f"“{title}”")


def get_dashboard(db: Session, principal: Principal) -> DashboardResponse:
    by_status = dict.fromkeys(DocumentStatus, 0)
    for status, count in db.execute(
        select(Document.processing_status, func.count())
        .where(readable_documents_clause(principal))
        .group_by(Document.processing_status)
    ):
        by_status[status] = count

    if principal.is_org_admin:
        workspaces = db.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.organization_id == principal.organization_id)
        )
    else:
        workspaces = len(principal.workspace_roles)

    recent = documents.list_documents(
        db,
        principal,
        workspace_ids=None,
        statuses=None,
        file_types=None,
        q=None,
        uploaded_by=None,
        sort=DocumentSort.CREATED_AT,
        order=SortOrder.DESC,
        page=PageParams(page=1, page_size=_RECENT_DOCUMENTS),
    )
    return DashboardResponse(
        documents_total=sum(by_status.values()),
        documents_by_status={status.value: count for status, count in by_status.items()},
        workspaces_count=workspaces or 0,
        recent_documents=recent.items,
        recent_conversations=_recent_conversations(db, principal),
        recent_activity=_recent_activity(db, principal),
    )


def _recent_conversations(db: Session, principal: Principal) -> list[ConversationOut]:
    message_count = (
        select(func.count())
        .where(
            Message.conversation_id == Conversation.id,
            Message.organization_id == Conversation.organization_id,
        )
        .scalar_subquery()
    )
    rows = db.execute(
        select(Conversation, message_count)
        .where(
            Conversation.organization_id == principal.organization_id,
            Conversation.user_id == principal.user_id,
        )
        .order_by(Conversation.updated_at.desc())
        .limit(_RECENT_CONVERSATIONS)
    )
    return [
        ConversationOut(
            id=conversation.id,
            title=conversation.title,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            message_count=count,
        )
        for conversation, count in rows
    ]


def _recent_activity(db: Session, principal: Principal) -> list[ActivityItem]:
    """Admins see the organization's events; everyone else only their own actions."""
    stmt = (
        select(AuditEvent, User.email)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(AuditEvent.organization_id == principal.organization_id)
    )
    if not principal.is_org_admin:
        stmt = stmt.where(AuditEvent.actor_user_id == principal.user_id)
    rows = db.execute(stmt.order_by(AuditEvent.created_at.desc()).limit(_RECENT_ACTIVITY))
    return [
        ActivityItem(
            id=event.id,
            action=event.action,
            actor_email=email,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            summary=summarize(event.action, event.event_metadata or {}),
            created_at=event.created_at,
        )
        for event, email in rows
    ]
