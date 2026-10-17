"""Audit trail for security-relevant actions.

`record` adds the event to the caller's transaction, so the event commits atomically
with the change it describes. Metadata must stay small and must never include document
text, prompts, answers, passwords or tokens.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent


class AuditAction(StrEnum):
    AUTH_REGISTER = "auth.register"
    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"  # noqa: S105 - not a secret
    AUTH_ORGANIZATION_SWITCHED = "auth.organization_switched"
    ORG_UPDATED = "organization.updated"
    ORG_MEMBER_ROLE_CHANGED = "organization.member_role_changed"
    ORG_MEMBER_REMOVED = "organization.member_removed"
    ORG_INVITATION_CREATED = "organization.invitation_created"
    ORG_INVITATION_REVOKED = "organization.invitation_revoked"
    ORG_INVITATION_ACCEPTED = "organization.invitation_accepted"
    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_UPDATED = "workspace.updated"
    WORKSPACE_DELETED = "workspace.deleted"
    WORKSPACE_MEMBER_ADDED = "workspace.member_added"
    WORKSPACE_MEMBER_ROLE_CHANGED = "workspace.member_role_changed"
    WORKSPACE_MEMBER_REMOVED = "workspace.member_removed"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_UPDATED = "document.updated"
    DOCUMENT_REPLACED = "document.replaced"
    DOCUMENT_DELETED = "document.deleted"
    DOCUMENT_REINDEX_REQUESTED = "document.reindex_requested"
    DOCUMENT_PERMISSIONS_CHANGED = "document.permissions_changed"
    DOCUMENT_DOWNLOADED = "document.downloaded"
    CONVERSATION_DELETED = "conversation.deleted"
    ACCESS_DENIED = "security.access_denied"


@dataclass(frozen=True)
class RequestMeta:
    ip_address: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


def record(
    db: Session,
    action: AuditAction,
    *,
    organization_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
    meta: RequestMeta | None = None,
) -> None:
    meta = meta or RequestMeta()
    db.add(
        AuditEvent(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action.value,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            event_metadata=metadata or {},
            ip_address=meta.ip_address,
            user_agent=(meta.user_agent or "")[:512] or None,
            request_id=meta.request_id,
        )
    )
