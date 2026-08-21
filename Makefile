.PHONY: up down clean up-clean status logs bridge-status bridge-logs bridge-soak \
	restart-bridge model-run-diagnostics diagnostics dev stop test test-integration \
	lint format typecheck ctx ctx-status ctx-doctor

UV ?= uv
PNPM ?= pnpm
COMPOSE ?= docker compose

up:
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

# Wipe the application data (postgres, redis, minio, subject workspaces) and
# start over. Destructive.
#
# `down -v` is deliberately NOT used: it would also drop bridge_data, which
# holds the authenticated ChatGPT browser profile. Losing it means logging the
# bridge back in by hand, which no data reset should ever require.
# Kept in step with the `name:` at the top of compose.yaml.
COMPOSE_PROJECT ?= cti-bulletin
CLEAN_VOLUMES = postgres_data redis_data minio_data subject_workspaces

clean:
	$(COMPOSE) down
	@for volume in $(CLEAN_VOLUMES); do \
		docker volume rm -f "$(COMPOSE_PROJECT)_$$volume" >/dev/null 2>&1 || true; \
	done
	@echo "Données applicatives effacées. Session du bridge ChatGPT conservée."

# Full reset: wipe the application data, then bring the stack back up.
up-clean: clean up

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

# Timeline of var/diagnostics/events.jsonl. ARGS is passed through, e.g.
#   make diagnostics ARGS="--failures -v"
#   make diagnostics ARGS="merge. -n 100"
diagnostics:
	@python3 scripts/diagnostics.py $(ARGS)

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

ctx:
	$(UV) run scripts/ctx/ctx.py build --prune

ctx-status:
	$(UV) run scripts/ctx/ctx.py status

ctx-doctor:
	$(UV) run scripts/ctx/ctx.py doctor
