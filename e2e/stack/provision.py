"""Reset the end-to-end database and Redis DB before every run.

Run through the backend environment (psycopg, SQLAlchemy and redis are backend dependencies):

    uv run --directory backend --frozen python ../e2e/stack/provision.py

It reads the same variables as the application (DATABASE_URL, MIGRATION_DATABASE_URL,
REDIS_URL) plus E2E_ADMIN_DATABASE_URL, a superuser URL used only to create the roles, the
database and the vector extension when missing, like backend/tests/conftest.py does. The schema
is then dropped and recreated empty; `alembic upgrade head` follows in the launcher.

Guard rails: it refuses any database whose name does not start with "eka_e2e" (so never the
development database "eka") and Redis DB 0.
"""

from __future__ import annotations

import os
import sys

import psycopg
import redis
from psycopg import sql
from sqlalchemy.engine import make_url

CONNECT_TIMEOUT = 30  # seconds: fail instead of waiting for a server that is not there
LOCK_TIMEOUT = "30s"  # the schema reset fails instead of waiting for a lock for ever


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"provision: {name} is not set")
    return value


def _plain(url: str) -> str:
    """psycopg wants postgresql://, the application URLs say postgresql+psycopg://."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> None:
    app_url = make_url(_require("DATABASE_URL"))
    owner_url = make_url(_require("MIGRATION_DATABASE_URL"))
    admin_url = make_url(_plain(_require("E2E_ADMIN_DATABASE_URL")))
    redis_url = _require("REDIS_URL")

    database = app_url.database or ""
    if not database.startswith("eka_e2e") or owner_url.database != database:
        sys.exit(f"provision: refusing to reset database {database!r} (must start with eka_e2e)")
    redis_client = redis.Redis.from_url(
        redis_url, socket_connect_timeout=CONNECT_TIMEOUT, socket_timeout=CONNECT_TIMEOUT
    )
    if redis_client.connection_pool.connection_kwargs.get("db", 0) == 0:
        sys.exit("provision: refusing to flush Redis DB 0")

    admin = admin_url.render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as conn:
        roles = {row[0] for row in conn.execute("SELECT rolname FROM pg_roles")}
        for url, options in ((owner_url, ""), (app_url, " NOSUPERUSER NOBYPASSRLS")):
            if url.username not in roles:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}" + options).format(
                        sql.Identifier(url.username or ""), sql.Literal(url.password or "")
                    )
                )
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if not exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(owner_url.username or "")
                )
            )
        # Sessions of a run that was killed would hold locks and block the schema reset below.
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        ).fetchall()

    target = admin_url.set(database=database).render_as_string(hide_password=False)
    with psycopg.connect(target, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as conn:
        conn.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(LOCK_TIMEOUT)))
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute(
            sql.SQL("CREATE SCHEMA public AUTHORIZATION {}").format(
                sql.Identifier(owner_url.username or "")
            )
        )
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(app_url.username or "")
            )
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(app_url.username or "")
            )
        )

    redis_client.flushdb()  # this DB only (the connection selects the DB from the URL)
    print(f"provision: database {database} reset, Redis flushed")


if __name__ == "__main__":
    main()
