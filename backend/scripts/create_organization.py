"""Create an organization and invite its first administrator, from the server. This is how
the first account is made when public registration is off (ALLOW_PUBLIC_REGISTRATION, false
by default). Settings come from the environment / .env.

    cd backend && uv run python scripts/create_organization.py \
        --organization "Acme Ltd" --email admin@acme.example

It prints a one-time link (valid INVITATION_TTL_HOURS, built from PUBLIC_BASE_URL). The
administrator opens it, signs up and chooses their own password, so no password passes
through a command line, an environment variable or a log. Until it is used, the link is
as good as a password: hand it over privately. Run the script again while nobody has joined
the organization to replace a lost or expired link, or one sent to a wrong address; every
earlier link then stops working.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.core.errors import AppError
from app.db.session import session_scope
from app.models import OrgRole
from app.schemas.organizations import InvitationCreate, OrganizationUpdate
from app.services import organizations
from app.services.audit import RequestMeta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an organization, invite its admin.")
    parser.add_argument("--organization", required=True, help="organization name")
    parser.add_argument("--email", required=True, help="the administrator's email")
    args = parser.parse_args(argv)
    try:
        name = OrganizationUpdate(name=args.organization).name
        email = InvitationCreate(email=args.email, role=OrgRole.ORG_ADMIN).email
        with session_scope() as db:
            invitation = organizations.invite_first_admin(
                db, name, email, meta=RequestMeta(user_agent="scripts/create_organization.py")
            )
    except ValidationError as exc:
        raise SystemExit(f"invalid {', '.join(str(e['loc'][0]) for e in exc.errors())}") from None
    except AppError as exc:
        raise SystemExit(exc.message) from None
    print(f"Organization {name!r}: {email} is invited as its administrator.")
    print(f"One-time sign-up link, valid until {invitation.expires_at:%Y-%m-%d %H:%M %Z}:")
    print(invitation.invite_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
