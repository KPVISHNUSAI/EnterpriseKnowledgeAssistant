"""Load the demo organization into the configured database (DATABASE_URL, storage, Redis and
model settings come from the environment / .env, exactly as for the API and worker).

    cd backend && uv run python scripts/seed_demo.py            # ingestion via the worker
    cd backend && uv run python scripts/seed_demo.py --inline   # ingest in this process

Creates the fictional company "Northwind Aerial Robotics (fictional)" from eval/corpus.py:
users admin@, manager@, employee@, finance@ and contractor@demo.example.com (org admin,
knowledge manager, three employees), three workspaces with memberships, and 18 synthetic
documents uploaded through the documents service. One document is restricted and granted
to finance@ only; employee@ cannot read it. Uploads are enqueued on the normal ingestion
queue for the worker; --inline runs the same ingestion jobs here instead (it loads the
configured embedding model). Safe to re-run: whatever exists is left as it is.

Passwords: every new user gets SEED_PASSWORD, or, when it is unset, a random password that
is printed once. Existing users keep their password.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import hash_password
from app.services.auth import MAX_PASSWORD_LENGTH
from app.storage import get_storage
from eval import corpus, loader


def main() -> int:
    parser = argparse.ArgumentParser(description="Load the demo organization and documents.")
    parser.add_argument(
        "--inline", action="store_true", help="ingest in this process instead of the worker"
    )
    args = parser.parse_args()
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("refusing to create demo accounts with APP_ENV=production")
    configure_logging(settings.log_level, settings.log_format)
    shared = os.environ.get("SEED_PASSWORD")
    if (
        shared is not None
        and not settings.password_min_length <= len(shared) <= MAX_PASSWORD_LENGTH
    ):
        raise SystemExit(
            f"SEED_PASSWORD must be {settings.password_min_length} to "
            f"{MAX_PASSWORD_LENGTH} characters long"
        )
    generated: dict[str, str] = {}

    def new_password_hash(person: corpus.Person) -> str:
        password = shared or secrets.token_urlsafe(18)
        if shared is None:
            generated[person.email] = password
        return hash_password(password)

    storage = get_storage()
    try:
        if args.inline:
            with loader.captured_ingestion_jobs() as jobs:
                loaded = loader.load(storage, new_password_hash)
            # Also finish jobs an earlier run left queued for a worker that never ran.
            jobs += loader.pending_jobs(loaded.organization_id, list(loaded.documents.values()))
            loader.ingest(jobs, loaded.organization_id, embedder=None, storage=storage)
        else:
            loaded = loader.load(storage, new_password_hash)

        for line in loaded.created or ["nothing: the demo organization is already complete"]:
            print(f"created {line}")
        print(f"\norganization: {corpus.ORG_NAME}")
        for person in corpus.PEOPLE:
            workspaces = ", ".join(f"{ws} ({role.value})" for ws, role in person.workspaces.items())
            print(f"  {person.email:32} {person.org_role.value:18} {workspaces or 'none'}")
        ingestion = "ingested in this process" if args.inline else "queued for the ingestion worker"
        print(f"documents: {len(loaded.documents)} ({ingestion})")
        if shared is not None and any(line.startswith("user ") for line in loaded.created):
            print("\nNew users sign in with SEED_PASSWORD.")
    finally:
        # Also when a later step fails: the users are committed first, and a re-run never
        # resets an existing user's password.
        if generated:
            print(
                "\nGenerated passwords (printed only now; store them before closing this window):"
            )
            for email, password in generated.items():
                print(f"  {email:32} {password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
