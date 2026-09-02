# Convenience wrappers. Every target maps to a command documented in README.md,
# so make is optional -- it is not installed by default on Windows.

PY := apps/api/.venv/Scripts/python.exe

.PHONY: help up down logs install migrate run seed test test-pg lint typecheck check reset

help:
	@echo "up        Start PostgreSQL and Redis"
	@echo "down      Stop them"
	@echo "install   Create venv deps (run 'py -3.13 -m venv apps/api/.venv' first)"
	@echo "migrate   Apply Alembic migrations"
	@echo "run       Start the API with reload on :8000"
	@echo "seed      Seed 120 synthetic demo cases"
	@echo "test      Run tests (SQLite, fast)"
	@echo "test-pg   Run tests against PostgreSQL (as CI does)"
	@echo "check     lint + typecheck + test"

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

install:
	$(PY) -m pip install -e "apps/api[dev]" -e .

migrate:
	cd apps/api && ../../$(PY) -m alembic upgrade head

run:
	cd apps/api && ../../$(PY) -m uvicorn app.main:app --reload --port 8000

seed:
	curl -X POST http://localhost:8000/api/v1/demo/seed -H "Content-Type: application/json" -d "{\"count\":120,\"reset\":true}"

test:
	cd apps/api && ../../$(PY) -m pytest -q

test-pg:
	cd apps/api && TEST_DATABASE_URL=postgresql+psycopg://revrec:revrec@localhost:5432/revrec_test ../../$(PY) -m pytest -q

lint:
	$(PY) -m ruff check apps/api/app apps/api/tests ml
	$(PY) -m ruff format --check apps/api/app apps/api/tests ml

typecheck:
	cd apps/api && ../../$(PY) -m mypy app

check: lint typecheck test
