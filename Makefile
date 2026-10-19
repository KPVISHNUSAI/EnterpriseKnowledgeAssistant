# Developer shortcuts. Ports default to the values in .env.example; override on the
# command line when a port is taken, e.g. `make api API_PORT=8082`.
UV := uv run --directory backend
API_PORT ?= 8081
EVAL_NAME ?= $(shell date +%Y-%m-%d)

.PHONY: help check-ports infra-up infra-down docker-build up-app down-app migrate seed api worker beat test test-unit e2e eval eval-llm lint format typecheck check

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

check-ports: ## Bind-test the host ports this project uses
	python3 scripts/check_ports.py

infra-up: ## Start PostgreSQL (pgvector) and Redis in Docker
	docker compose up -d postgres redis

infra-down: ## Stop the Docker services (data volumes are kept)
	docker compose down

docker-build: ## Build the backend and frontend images
	docker compose --profile app build

up-app: ## Run the full stack in containers (migrates, then starts api/worker/beat/frontend)
	docker compose --profile app up -d --build

down-app: ## Stop the full stack including postgres and redis (data volumes are kept)
	docker compose --profile app down

migrate: ## Apply database migrations (as the schema owner role)
	$(UV) alembic upgrade head

seed: ## Load the synthetic demo organization and documents (after migrate; the worker indexes them)
	$(UV) python scripts/seed_demo.py

api: ## Run the API with auto-reload
	python3 scripts/check_ports.py $(API_PORT)
	$(UV) uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port $(API_PORT)

worker: ## Run the ingestion/maintenance worker (solo pool: prefork cannot run on macOS)
	$(UV) celery -A app.worker.celery_app worker -Q ingestion,maintenance --pool solo -l INFO

beat: ## Run the periodic scheduler (stale-job reaper, retention)
	$(UV) celery -A app.worker.celery_app beat -l INFO

test: ## All backend tests (needs `make infra-up`)
	$(UV) pytest

test-unit: ## Backend tests that need no database
	$(UV) pytest -m "not integration"

e2e: ## Playwright end-to-end tests against a stack started from source (see e2e/README.md)
	cd e2e && npm test

eval: ## Retrieval + extractive evaluation (resets the eka_eval database; report in backend/eval/reports)
	$(UV) python -m eval run --name $(EVAL_NAME)

eval-llm: ## Add answers by an OpenAI-compatible LLM (Ollama llama3.1:8b by default) to the same report
	$(UV) python -m eval run --name $(EVAL_NAME) --no-retrieval --no-extractive --llm-configs hybrid_rerank

lint: ## Lint and format check
	$(UV) ruff check .
	$(UV) ruff format --check .

format: ## Auto-format and fix lint
	$(UV) ruff format .
	$(UV) ruff check --fix .

typecheck: ## Static type check
	$(UV) mypy

check: lint typecheck test ## Everything CI runs for the backend
