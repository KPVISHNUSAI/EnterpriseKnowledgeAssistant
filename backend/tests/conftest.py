"""Shared test harness.

* Unit tests need nothing running. Tests that request `db`, `client` or `factory` are
  integration tests (auto-marked `integration`) and need PostgreSQL + Redis
  (`docker compose up -d postgres redis`).
* Integration tests use their own database, `eka_test` by default. Set EKA_TEST_DB to
  give concurrent test runs separate databases. The database (and the eka_owner/eka_app
  roles) are created on demand through TEST_ADMIN_DATABASE_URL, the schema is rebuilt
  with Alembic once per session (upgrade -> downgrade -> upgrade), and every table is
  truncated before each test.
* The app runs as the least-privileged `eka_app` role, so row-level security is active
  in every test exactly as in production.
* Providers are deterministic: hashing embeddings, no reranker, extractive answers.
  Override per test with `app.dependency_overrides[get_llm] = lambda: FakeLLM()`.
* Settings are cached; change one for a test with
  `monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)`.
* No Celery broker: `enqueued` records sent tasks; `run_ingestion()` runs queued
  ingestion jobs inline.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

# --- Environment (must happen before any `app` import: settings are cached) ----------------
_PG_PORT = os.environ.get("EKA_POSTGRES_PORT", "5436")
_TEST_DB = os.environ.get("EKA_TEST_DB", "eka_test")
if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", _TEST_DB):
    raise RuntimeError("EKA_TEST_DB must be a lowercase identifier")
APP_URL = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+psycopg://eka_app:eka_app_dev_password@localhost:{_PG_PORT}/{_TEST_DB}",
)
OWNER_URL = os.environ.get(
    "TEST_MIGRATION_DATABASE_URL",
    f"postgresql+psycopg://eka_owner:eka_owner_dev_password@localhost:{_PG_PORT}/{_TEST_DB}",
)
ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL",
    f"postgresql+psycopg://postgres:postgres_dev_password@localhost:{_PG_PORT}/postgres",
)
_STORAGE_DIR = tempfile.mkdtemp(prefix="eka-test-storage-")

os.environ.update(
    {
        "APP_ENV": "test",
        "SECRET_KEY": "test-only-secret-key-0123456789abcdef0123456789abcdef",
        "DATABASE_URL": APP_URL,
        "MIGRATION_DATABASE_URL": OWNER_URL,
        "DB_APP_ROLE": APP_URL.split("://", 1)[1].split(":", 1)[0],
        "REDIS_URL": os.environ.get("TEST_REDIS_URL", "redis://localhost:6381/15"),
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_PATH": _STORAGE_DIR,
        "EMBEDDING_PROVIDER": "hashing",
        "EMBEDDING_DIMENSIONS": "384",
        "RERANKER_PROVIDER": "none",
        "RERANK_ENABLED": "false",
        "LLM_PROVIDER": "extractive",
        "RATE_LIMIT_ENABLED": "false",
        "ARGON2_TIME_COST": "1",
        "ARGON2_MEMORY_KIB": "1024",
        "ARGON2_PARALLELISM": "1",
        "LOG_FORMAT": "console",
        "LOG_LEVEL": "WARNING",
        "ALLOWED_ORIGINS": "http://localhost:3000",
        "PUBLIC_BASE_URL": "http://localhost:3000",
        "COOKIE_SECURE": "false",
        "METRICS_ENABLED": "true",
        "QUERY_REWRITE_ENABLED": "true",
    }
)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.api.deps import UNSAFE_METHODS  # noqa: E402
from app.core.config import BACKEND_DIR, get_settings  # noqa: E402
from app.core.security import csrf_token_for, hash_password  # noqa: E402
from app.db.base import utcnow  # noqa: E402
from app.db.session import get_sessionmaker, set_tenant  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    Document,
    DocumentChunk,
    DocumentPermission,
    DocumentStatus,
    OrganizationMembership,
    OrgRole,
    PermissionLevel,
    PrincipalType,
    User,
    Workspace,
    WorkspaceMembership,
    WorkspaceRole,
)
from app.models.organization import Organization  # noqa: E402
from app.providers.embeddings import EmbeddingProvider, HashingEmbedder  # noqa: E402
from app.services.access import Principal  # noqa: E402
from app.services.audit import RequestMeta  # noqa: E402
from app.services.sessions import create_session  # noqa: E402

DEFAULT_PASSWORD = "Correct-Horse-Battery-9"


# --- Database lifecycle --------------------------------------------------------------------


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _provision_database() -> None:
    """Create roles, the test database and the vector extension when missing, then
    reset the public schema. Skipped (best effort) if the admin URL is unusable."""
    app_url, owner_url = make_url(APP_URL), make_url(OWNER_URL)
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as conn:
            roles = set(conn.scalars(text("SELECT rolname FROM pg_roles")))
            for url, extra in ((owner_url, ""), (app_url, " NOSUPERUSER NOBYPASSRLS")):
                if url.username not in roles:
                    conn.execute(
                        text(
                            f'CREATE ROLE "{url.username}" LOGIN PASSWORD '
                            f"{_quote_literal(url.password or '')}{extra}"
                        )
                    )
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": _TEST_DB}
            )
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{_TEST_DB}" OWNER "{owner_url.username}"'))
    except Exception as exc:  # pragma: no cover - environment specific
        admin.dispose()
        print(f"[conftest] admin provisioning skipped: {type(exc).__name__}")
        return
    admin.dispose()

    db_admin = create_engine(
        make_url(ADMIN_URL).set(database=_TEST_DB), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    with db_admin.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text(f'CREATE SCHEMA public AUTHORIZATION "{owner_url.username}"'))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{_TEST_DB}" TO "{app_url.username}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{app_url.username}"'))
    db_admin.dispose()


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["database_url"] = OWNER_URL
    return cfg


@pytest.fixture(scope="session")
def _database() -> Iterator[Engine]:
    """Migrated test database; yields an owner-role engine (for truncation only)."""
    _provision_database()
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # proves the migrations are reversible
    command.upgrade(cfg, "head")
    owner = create_engine(OWNER_URL, poolclass=NullPool)
    yield owner
    owner.dispose()
    shutil.rmtree(_STORAGE_DIR, ignore_errors=True)


_TABLES = ", ".join(table.name for table in Base.metadata.sorted_tables)


@pytest.fixture
def clean_db(_database: Engine) -> None:
    """Empty every table before the test (state after a failure stays inspectable)."""
    with _database.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TABLES} CASCADE"))


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "_database" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.integration)


# --- Broker stub ---------------------------------------------------------------------------


@dataclass
class EnqueuedTasks:
    tasks: list[tuple[str, list[Any]]] = field(default_factory=list)
    fail: bool = False  # simulate a broker outage

    def send_task(self, name: str, args: Sequence[Any] | None = None, **_: Any) -> None:
        if self.fail:
            raise ConnectionError("broker unavailable (simulated)")
        self.tasks.append((name, list(args or [])))

    def named(self, name: str) -> list[list[Any]]:
        return [args for task, args in self.tasks if task == name]


@pytest.fixture(autouse=True)
def enqueued(monkeypatch: pytest.MonkeyPatch) -> EnqueuedTasks:
    from app.worker.celery_app import celery_app

    recorder = EnqueuedTasks()
    monkeypatch.setattr(celery_app, "send_task", recorder.send_task)
    return recorder


@pytest.fixture
def run_ingestion(enqueued: EnqueuedTasks, clean_db: None) -> Any:
    """Process every enqueued ingestion job inline (as the worker would), in order."""

    def run() -> list[Any]:
        try:
            from app.ingestion.runner import process_job
        except ImportError:
            pytest.skip("ingestion runner not available yet (app.ingestion.runner)")
        results = []
        while pending := enqueued.named("eka.ingestion.process"):
            enqueued.tasks = [t for t in enqueued.tasks if t[0] != "eka.ingestion.process"]
            for job_id, org_id in pending:
                results.append(process_job(uuid.UUID(job_id), uuid.UUID(org_id)))
        return results

    return run


# --- App and HTTP client -------------------------------------------------------------------


@pytest.fixture(scope="session")
def app() -> Iterator[FastAPI]:
    from app.main import create_app

    application = create_app()
    yield application


@pytest.fixture(autouse=True)
def _reset_overrides(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if "app" in request.fixturenames:
        request.getfixturevalue("app").dependency_overrides.clear()


class ApiClient(TestClient):
    """TestClient that echoes the CSRF cookie in the X-CSRF-Token header, like the SPA."""

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        settings = get_settings()
        token = self.cookies.get(settings.csrf_cookie_name)
        if method.upper() in UNSAFE_METHODS and token:
            headers = dict(kwargs.pop("headers", None) or {})
            headers.setdefault(settings.csrf_header_name, token)
            kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def client(app: FastAPI, clean_db: None) -> Iterator[ApiClient]:
    with ApiClient(app) as test_client:
        yield test_client


@pytest.fixture
def anon_client(app: FastAPI) -> Iterator[ApiClient]:
    """Client for endpoints that need no database (health, OpenAPI, middleware)."""
    with ApiClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(clean_db: None) -> Iterator[Session]:
    """App-role session (RLS applies). Bind it to a tenant with `set_tenant(db, org.id)`."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


# --- Data factories ------------------------------------------------------------------------


class Factory:
    """Creates rows through the app role, setting the tenant context like the API does."""

    def __init__(self, maker: sessionmaker[Session]) -> None:
        self._maker = maker
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return self._n

    def _save(self, *rows: Any, org_id: uuid.UUID | None = None) -> None:
        with self._maker() as session:
            set_tenant(session, org_id)
            session.add_all(rows)
            session.commit()

    def user(
        self,
        email: str | None = None,
        *,
        password: str = DEFAULT_PASSWORD,
        full_name: str | None = None,
        is_active: bool = True,
    ) -> User:
        n = self._next()
        user = User(
            email=(email or f"user{n}@example.com").lower(),
            password_hash=hash_password(password),
            full_name=full_name or f"Test User {n}",
            is_active=is_active,
        )
        self._save(user)
        return user

    def org(self, name: str | None = None) -> Organization:
        org = Organization(name=name or f"Org {self._next()}")
        self._save(org)
        return org

    def member(
        self, org: Organization, user: User, role: OrgRole = OrgRole.EMPLOYEE
    ) -> OrganizationMembership:
        membership = OrganizationMembership(organization_id=org.id, user_id=user.id, role=role)
        self._save(membership)
        return membership

    def workspace(
        self,
        org: Organization,
        name: str | None = None,
        *,
        members: dict[User, WorkspaceRole] | None = None,
        created_by: User | None = None,
    ) -> Workspace:
        ws = Workspace(
            organization_id=org.id,
            name=name or f"Workspace {self._next()}",
            created_by=created_by.id if created_by else None,
        )
        self._save(ws, org_id=org.id)
        for user, role in (members or {}).items():
            self.workspace_member(ws, user, role)
        return ws

    def workspace_member(
        self, ws: Workspace, user: User, role: WorkspaceRole = WorkspaceRole.VIEWER
    ) -> WorkspaceMembership:
        row = WorkspaceMembership(
            workspace_id=ws.id, organization_id=ws.organization_id, user_id=user.id, role=role
        )
        self._save(row, org_id=ws.organization_id)
        return row

    def document(
        self,
        ws: Workspace,
        *,
        uploaded_by: User | None,
        title: str | None = None,
        status: DocumentStatus = DocumentStatus.INDEXED,
        restricted: bool = False,
        grants: dict[User, PermissionLevel] | None = None,
        mime_type: str = "text/plain",
        filename: str | None = None,
        content_hash: str | None = None,
    ) -> Document:
        """A document row (no stored file). Workspace-visible unless `restricted`."""
        n = self._next()
        doc_id = uuid.uuid4()
        doc = Document(
            id=doc_id,
            organization_id=ws.organization_id,
            workspace_id=ws.id,
            title=title or f"Document {n}",
            original_filename=filename or f"document-{n}.txt",
            storage_key=f"orgs/{ws.organization_id}/documents/{doc_id}/v1/test.txt",
            content_hash=content_hash or uuid.uuid4().hex + uuid.uuid4().hex,
            mime_type=mime_type,
            file_size=0,
            uploaded_by=uploaded_by.id if uploaded_by else None,
            processing_status=status,
            indexed_at=utcnow() if status == DocumentStatus.INDEXED else None,
        )
        rows: list[Any] = [doc]
        if not restricted:
            rows.append(self._grant(doc, PrincipalType.WORKSPACE, ws.id, PermissionLevel.READ))
        for user, level in (grants or {}).items():
            rows.append(self._grant(doc, PrincipalType.USER, user.id, level))
        self._save(*rows, org_id=ws.organization_id)
        return doc

    @staticmethod
    def _grant(
        doc: Document, kind: PrincipalType, principal_id: uuid.UUID, level: PermissionLevel
    ) -> DocumentPermission:
        return DocumentPermission(
            document_id=doc.id,
            organization_id=doc.organization_id,
            principal_type=kind,
            principal_id=principal_id,
            permission=level,
        )

    def chunks(
        self,
        doc: Document,
        items: Sequence[str | dict[str, Any]],
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> list[DocumentChunk]:
        """Index `items` (text, or dicts with content/page_number/section_title/...) as the
        document's chunks, embedded with `embedder` (hashing by default)."""
        embedder = embedder or HashingEmbedder(get_settings().embedding_dimensions)
        specs = [{"content": item} if isinstance(item, str) else dict(item) for item in items]
        vectors = embedder.embed_documents([spec["content"] for spec in specs])
        rows = [
            DocumentChunk(
                document_id=doc.id,
                organization_id=doc.organization_id,
                chunk_index=index,
                content=spec["content"],
                content_hash=uuid.uuid4().hex,
                token_count=spec.get("token_count", len(spec["content"].split())),
                embedding=vector,
                embedding_model=embedder.model_name,
                page_number=spec.get("page_number"),
                page_end=spec.get("page_end", spec.get("page_number")),
                section_title=spec.get("section_title"),
                heading_path=spec.get("heading_path", spec.get("section_title")),
                chunk_metadata=spec.get("metadata", {}),
            )
            for index, (spec, vector) in enumerate(zip(specs, vectors, strict=True))
        ]
        with self._maker() as session:
            set_tenant(session, doc.organization_id)
            session.add_all(rows)
            stored = session.get(Document, doc.id)
            assert stored is not None
            stored.chunk_count = len(rows)
            stored.processing_status = DocumentStatus.INDEXED
            stored.embedding_model = embedder.model_name
            stored.indexed_at = utcnow()
            session.commit()
        return rows

    def principal(self, user: User, org: Organization) -> Principal:
        with self._maker() as session:
            set_tenant(session, org.id)
            role = session.scalar(
                select(OrganizationMembership.role).where(
                    OrganizationMembership.organization_id == org.id,
                    OrganizationMembership.user_id == user.id,
                )
            )
            assert role is not None, "user is not a member of the organization"
            workspace_roles = {
                workspace_id: ws_role
                for workspace_id, ws_role in session.execute(
                    select(WorkspaceMembership.workspace_id, WorkspaceMembership.role).where(
                        WorkspaceMembership.organization_id == org.id,
                        WorkspaceMembership.user_id == user.id,
                    )
                )
            }
        return Principal(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            organization_id=org.id,
            org_role=role,
            workspace_roles=workspace_roles,
        )

    def authenticate(self, client: TestClient, user: User, org: Organization) -> TestClient:
        """Sign `client` in as `user` with `org` active, without the login endpoint."""
        with self._maker() as session:
            _, token = create_session(session, user, organization_id=org.id, meta=RequestMeta())
            session.commit()
        settings = get_settings()
        # Same domain the cookie jar records for cookies the API sets on "testserver".
        client.cookies.set(settings.session_cookie_name, token, domain="testserver.local")
        client.cookies.set(
            settings.csrf_cookie_name, csrf_token_for(token), domain="testserver.local"
        )
        return client

    def expire_sessions(self, user: User) -> None:
        from app.models import UserSession

        with self._maker() as session:
            for row in session.scalars(select(UserSession).where(UserSession.user_id == user.id)):
                row.expires_at = utcnow() - timedelta(seconds=1)
            session.commit()


@pytest.fixture
def factory(clean_db: None) -> Factory:
    return Factory(get_sessionmaker())


@dataclass
class Tenant:
    """A ready-made organization: admin, knowledge manager, editor, viewer, outsider."""

    org: Organization
    workspace: Workspace
    admin: User
    manager: User
    editor: User
    viewer: User
    outsider: User  # org member without workspace access


@pytest.fixture
def tenant(factory: Factory) -> Tenant:
    return make_tenant(factory, "acme")


@pytest.fixture
def other_tenant(factory: Factory) -> Tenant:
    return make_tenant(factory, "globex")


def make_tenant(factory: Factory, slug: str) -> Tenant:
    org = factory.org(f"{slug.title()} Corp")
    admin = factory.user(f"admin@{slug}.example.com", full_name=f"{slug.title()} Admin")
    manager = factory.user(f"manager@{slug}.example.com", full_name=f"{slug.title()} Manager")
    editor = factory.user(f"editor@{slug}.example.com", full_name=f"{slug.title()} Editor")
    viewer = factory.user(f"viewer@{slug}.example.com", full_name=f"{slug.title()} Viewer")
    outsider = factory.user(f"outsider@{slug}.example.com", full_name=f"{slug.title()} Outsider")
    factory.member(org, admin, OrgRole.ORG_ADMIN)
    factory.member(org, manager, OrgRole.KNOWLEDGE_MANAGER)
    for user in (editor, viewer, outsider):
        factory.member(org, user, OrgRole.EMPLOYEE)
    workspace = factory.workspace(
        org,
        f"{slug.title()} Engineering",
        members={
            manager: WorkspaceRole.OWNER,
            editor: WorkspaceRole.EDITOR,
            viewer: WorkspaceRole.VIEWER,
        },
        created_by=manager,
    )
    return Tenant(org, workspace, admin, manager, editor, viewer, outsider)


@pytest.fixture
def login_as(app: FastAPI, factory: Factory) -> Any:
    """`login_as(user, org)` -> a new signed-in ApiClient (several can coexist)."""
    clients: list[ApiClient] = []

    def make(user: User, org: Organization) -> ApiClient:
        test_client = ApiClient(app)
        clients.append(test_client)
        factory.authenticate(test_client, user, org)
        return test_client

    yield make
    for test_client in clients:
        test_client.close()
