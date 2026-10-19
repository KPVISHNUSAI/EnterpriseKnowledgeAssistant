"""Create an organization and its first administrator from the server. This is how the first
account is made when public registration is off (ALLOW_PUBLIC_REGISTRATION, false by
default); everyone else then joins by invitation. Settings come from the environment / .env.

    cd backend && uv run python scripts/create_organization.py \
        --organization "Acme Ltd" --email admin@acme.example --name "Asha Admin"

The password is read from ADMIN_PASSWORD, or asked for when that is unset. It is never printed.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.core.errors import AppError
from app.db.session import session_scope
from app.schemas.auth import RegisterRequest
from app.services import auth
from app.services.audit import RequestMeta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an organization and its first admin.")
    parser.add_argument("--organization", required=True, help="organization name")
    parser.add_argument("--email", required=True, help="the administrator's email")
    parser.add_argument("--name", required=True, help="the administrator's full name")
    args = parser.parse_args(argv)
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Administrator password: ")
    try:
        body = RegisterRequest(
            email=args.email,
            password=password,
            full_name=args.name,
            organization_name=args.organization,
        )
        with session_scope() as db:
            org_id = auth.create_organization(
                db, body, meta=RequestMeta(user_agent="scripts/create_organization.py")
            )
    except ValidationError as exc:
        # Only the field names: the input may contain the password.
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors())
        raise SystemExit(f"invalid {fields}") from None
    except AppError as exc:
        raise SystemExit(exc.message) from None
    print(f"created organization {args.organization!r} ({org_id}) with admin {body.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
