# One image for the API, the Celery worker, Celery beat and the migration job: the
# command is the only difference (docker-compose.yml, infra/aws/ecs/).
#
#   docker build -t eka-backend:dev backend
#
# No secrets are baked in; all configuration is read from the environment at runtime.

# --- builder: install exactly what uv.lock says ---------------------------------------------
FROM python:3.14-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.20@sha256:100047e74f30778ab704942321a09750d6158739573ff58bf3924085cc6cd2d8 /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# uv.lock is the source of truth (--frozen never re-resolves). On Linux torch comes from
# the CPU wheel index configured in pyproject.toml, not the multi-GB CUDA build.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --frozen --no-dev --extra local-models

# --- runtime: interpreter, virtualenv and application code; no uv, no build tools ---------
FROM python:3.14-slim-trixie

# HF_HOME and LOCAL_STORAGE_PATH are the two places the app writes state. Mount a
# volume on each (docker-compose.yml) so models download once and uploads survive.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/lib/eka/huggingface \
    LOCAL_STORAGE_PATH=/var/lib/eka/storage

# Fixed uid/gid so volume ownership is predictable. The state directories are created
# here so that a new named volume mounted over them inherits the app user as owner.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app /var/lib/eka/huggingface /var/lib/eka/storage

COPY --from=builder /opt/venv /opt/venv

# Code stays root-owned and read-only for the app user. alembic.ini and alembic/ are
# needed at runtime too: /api/v1/ready compares the database with the migration head.
WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER app
EXPOSE 8081

# Only meaningful for the API. Worker, beat and migrate override or disable it
# (docker-compose.yml); ECS ignores image health checks and uses the task definition's.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8081/api/v1/health', timeout=3)"]

# One uvicorn process per container: scale by running more containers.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081", "--proxy-headers"]
