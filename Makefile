# Developer shortcuts. Ports default to the values in .env.example; override on the
# command line when a port is taken, e.g. `make api API_PORT=8082`.
UV := uv run --directory backend
API_PORT ?= 8081

.PHONY: help check-ports infra-up infra-down migrate api worker beat test test-unit lint format typecheck check

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

check-ports: ## Bind-test the host ports this project uses
	python3 scripts/check_ports.py

infra-up: ## Start PostgreSQL (pgvector) and Redis in Docker
	docker compose up -d postgres redis

infra-down: ## Stop the Docker services (data volumes are kept)
	docker compose down

migrate: ## Apply database migrations (as the schema owner role)
	$(UV) alembic upgrade head

api: ## Run the API with auto-reload
	python3 scripts/check_ports.py $(API_PORT)
	$(UV) uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port $(API_PORT)

worker: ## Run the ingestion/maintenance worker
	$(UV) celery -A app.worker.celery_app worker -Q ingestion,maintenance -l INFO

beat: ## Run the periodic scheduler (stale-job reaper, retention)
	$(UV) celery -A app.worker.celery_app beat -l INFO

test: ## All backend tests (needs `make infra-up`)
	$(UV) pytest

test-unit: ## Backend tests that need no database
	$(UV) pytest -m "not integration"

lint: ## Lint and format check
	$(UV) ruff check .
	$(UV) ruff format --check .

format: ## Auto-format and fix lint
	$(UV) ruff format .
	$(UV) ruff check --fix .

typecheck: ## Static type check
	$(UV) mypy

check: lint typecheck test ## Everything CI runs for the backend
