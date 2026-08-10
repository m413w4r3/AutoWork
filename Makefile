.PHONY: dev stop test test-integration lint format typecheck

UV ?= uv
PNPM ?= pnpm
COMPOSE ?= docker compose

dev:
	$(COMPOSE) up --build

stop:
	$(COMPOSE) down

test:
	cd backend && $(UV) run pytest
	cd frontend && $(PNPM) test --run

test-integration:
	cd backend && $(UV) run pytest -m integration

lint:
	cd backend && $(UV) run ruff check .
	cd frontend && $(PNPM) lint

typecheck:
	cd backend && $(UV) run mypy src tests
	cd frontend && $(PNPM) typecheck

format:
	cd backend && $(UV) run ruff format .
	cd backend && $(UV) run ruff check --fix .
	cd frontend && $(PNPM) format
