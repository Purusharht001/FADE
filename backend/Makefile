.PHONY: install dev test lint format typecheck migrate migrate-new seed docker-up docker-down clean

install:
	uv sync

dev:
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test:
	uv run pytest -v --cov=app --cov-report=term-missing

lint:
	uv run ruff check app tests

format:
	uv run ruff format app tests
	uv run ruff check --fix app tests

typecheck:
	uv run mypy app

migrate:
	uv run alembic upgrade head

migrate-new:
	uv run alembic revision --autogenerate -m "$(m)"

seed:
	uv run python scripts/seed.py

docker-up:
	docker compose up --build

docker-down:
	docker compose down -v

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage fade.db
