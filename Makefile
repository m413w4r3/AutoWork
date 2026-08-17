.PHONY: up down status logs bridge-status bridge-logs bridge-soak restart-bridge model-run-diagnostics dev stop test test-integration lint format typecheck

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
	$(COMPOSE) logs --tail=200 -f chatgpt-bridge worker backend

model-run-diagnostics:
	@test -n "$(RUN_ID)" || (echo "Usage: make model-run-diagnostics RUN_ID=<uuid>" >&2; exit 2)
	$(COMPOSE) exec -T backend python -m cti_app.model_run_diagnostics "$(RUN_ID)"

bridge-soak:
	$(COMPOSE) --profile bridge-test run --rm --build bridge-soak
	$(COMPOSE) --profile bridge-test down -v

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
