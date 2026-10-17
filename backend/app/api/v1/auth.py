from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_principal, get_request_meta, rate_limit_ip
from app.core.config import get_settings
from app.schemas.auth import (
    AcceptInvitationRequest,
    ChangePasswordRequest,
    InvitationPreview,
    LoginRequest,
    MeResponse,
    RegisterRequest,
    SwitchOrganizationRequest,
    UpdateProfileRequest,
    UserOut,
)
from app.schemas.common import errors
from app.services import auth as auth_service
from app.services.access import Principal
from app.services.audit import RequestMeta
from app.services.sessions import clear_session_cookies, set_session_cookies

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=MeResponse,
    status_code=201,
    responses=errors(403, 409, 422, 429),
    dependencies=[rate_limit_ip("register", "rate_limit_register_per_hour", 3600)],
)
def register(
    body: RegisterRequest,
    response: Response,
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> MeResponse:
    """Create an account and sign in.

    With `invite_token` the user joins the inviting organization with the invited role
    (the email must match the invitation). Without it a new organization is created with
    the caller as organization admin; this requires ALLOW_PUBLIC_REGISTRATION=true.
    Sets the HttpOnly session cookie and the CSRF cookie.
    """
    me, token = auth_service.register(db, body, meta=meta)
    set_session_cookies(response, token)
    return me


@router.post(
    "/login",
    response_model=MeResponse,
    responses=errors(401, 422, 429),
    dependencies=[rate_limit_ip("login", "rate_limit_login_per_minute")],
)
def login(
    body: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> MeResponse:
    """Sign in with email and password. Unknown emails and wrong passwords return the
    same 401 after the same amount of work."""
    me, token = auth_service.login(db, body, meta=meta)
    set_session_cookies(response, token)
    return me


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> Response:
    """Revoke the current session (when it is valid and the CSRF token matches) and
    clear the cookies. Always succeeds, so a stale client can always sign out."""
    settings = get_settings()
    auth_service.logout(
        db,
        request.cookies.get(settings.session_cookie_name),
        request.headers.get(settings.csrf_header_name),
        meta=meta,
    )
    response = Response(status_code=204)
    clear_session_cookies(response)
    return response


@router.get("/me", response_model=MeResponse, responses=errors(401))
def me(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> MeResponse:
    """The signed-in user, their active organization, organizations they belong to,
    accessible workspaces and UI capability hints."""
    return auth_service.get_me(db, principal)


@router.patch("/me", response_model=UserOut, responses=errors(401, 403, 422))
def update_profile(
    body: UpdateProfileRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> UserOut:
    return auth_service.update_profile(db, principal, body, meta=meta)


@router.post("/me/password", status_code=204, responses=errors(401, 403, 422))
def change_password(
    body: ChangePasswordRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> None:
    """Change the password (requires the current one). Revokes all other sessions."""
    auth_service.change_password(db, principal, body, meta=meta)


@router.post("/switch-organization", response_model=MeResponse, responses=errors(401, 403, 404))
def switch_organization(
    body: SwitchOrganizationRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> MeResponse:
    """Make another organization the caller belongs to the active one for this session."""
    return auth_service.switch_organization(db, principal, body.organization_id, meta=meta)


@router.get(
    "/invitations/{token}",
    response_model=InvitationPreview,
    responses=errors(404, 429),
    dependencies=[rate_limit_ip("invitation_preview", "rate_limit_login_per_minute")],
)
def preview_invitation(token: str, db: Session = Depends(get_db)) -> InvitationPreview:
    """Public: describe a pending invitation (404 if unknown, expired, revoked or used)."""
    return auth_service.preview_invitation(db, token)


@router.post("/invitations/accept", response_model=MeResponse, responses=errors(401, 403, 404, 409))
def accept_invitation(
    body: AcceptInvitationRequest,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    meta: RequestMeta = Depends(get_request_meta),
) -> MeResponse:
    """An existing user joins the inviting organization (email must match) and switches
    to it."""
    return auth_service.accept_invitation(db, principal, body.token, meta=meta)
