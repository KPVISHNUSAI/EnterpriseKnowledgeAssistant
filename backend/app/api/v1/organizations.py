from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, get_request_meta, require_org_admin
from app.schemas.common import Page, PageParams, errors
from app.schemas.organizations import (
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    MemberOut,
    OrganizationOut,
    OrganizationUpdate,
    UpdateMemberRoleRequest,
)
from app.services import organizations as org_service
from app.services.access import Principal
from app.services.audit import RequestMeta

router = APIRouter(prefix="/organization", tags=["organization"])


@router.get("", response_model=OrganizationOut, responses=errors(401))
def get_organization(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> OrganizationOut:
    """The caller's active organization."""
    return org_service.get_organization(db, principal)


@router.patch("", response_model=OrganizationOut, responses=errors(401, 403, 422))
def update_organization(
    body: OrganizationUpdate,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> OrganizationOut:
    return org_service.update_organization(db, principal, body, meta=meta)


@router.get("/members", response_model=Page[MemberOut], responses=errors(401))
def list_members(
    q: str | None = Query(None, max_length=200, description="Filter by email or name"),
    page: PageParams = Depends(),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Page[MemberOut]:
    """Organization directory (used to pick workspace members and document grantees)."""
    return org_service.list_members(db, principal, q=q, page=page)


@router.patch("/members/{user_id}", response_model=MemberOut, responses=errors(401, 403, 404, 409))
def update_member_role(
    user_id: uuid.UUID,
    body: UpdateMemberRoleRequest,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> MemberOut:
    """Change a member's organization role. 409 when it would leave no org admin."""
    return org_service.update_member_role(db, principal, user_id, body, meta=meta)


@router.delete("/members/{user_id}", status_code=204, responses=errors(401, 403, 404, 409))
def remove_member(
    user_id: uuid.UUID,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    """Remove a member (also removes their workspace memberships and conversations in
    this organization). 409 when it would leave no org admin."""
    org_service.remove_member(db, principal, user_id, meta=meta)


@router.get("/invitations", response_model=Page[InvitationOut], responses=errors(401, 403))
def list_invitations(
    page: PageParams = Depends(),
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> Page[InvitationOut]:
    return org_service.list_invitations(db, principal, page=page)


@router.post(
    "/invitations",
    response_model=InvitationCreated,
    status_code=201,
    responses=errors(401, 403, 409, 422),
)
def create_invitation(
    body: InvitationCreate,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> InvitationCreated:
    """Invite someone by email. The raw token is returned once (share the invite URL);
    only its hash is stored. 409 if the email is already a member."""
    return org_service.create_invitation(db, principal, body, meta=meta)


@router.delete("/invitations/{invitation_id}", status_code=204, responses=errors(401, 403, 404))
def revoke_invitation(
    invitation_id: uuid.UUID,
    principal: Principal = Depends(require_org_admin),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    org_service.revoke_invitation(db, principal, invitation_id, meta=meta)
