.PHONY: up down status logs bridge-status bridge-logs restart-bridge dev stop test test-integration lint format typecheck

UV ?= uv
PNPM ?= pnpm
COMPOSE ?= docker compose

up:
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

status:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs --tail=200 -f backend worker job-recovery frontend chatgpt-bridge

bridge-status:
	$(COMPOSE) ps chatgpt-bridge
	$(COMPOSE) exec -T chatgpt-bridge python tools/status.py

bridge-logs:
	$(COMPOSE) logs --tail=200 -f chatgpt-bridge worker

restart-bridge:
	$(COMPOSE) up -d --build --wait --force-recreate --no-deps chatgpt-bridge

dev:
	$(COMPOSE) up --build

stop: down

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
