.PHONY: up down clean up-clean status logs bridge-status bridge-logs bridge-soak \
	restart-bridge model-run-diagnostics diagnostics dev stop test test-integration \
	lint format typecheck ctx ctx-dense ctx-lexical ctx-status ctx-doctor ctx-benchmark

UV ?= uv
PNPM ?= pnpm
COMPOSE ?= docker compose
BRIDGE_CI_PROJECT ?= cti-bulletin-bridge-ci

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
CLEAN_VOLUMES = postgres_data redis_data minio_data
CLEAN_PATHS = var/diagnostics/runs

clean:
	$(COMPOSE) down
	@for volume in $(CLEAN_VOLUMES); do \
		docker volume rm -f "$(COMPOSE_PROJECT)_$$volume" >/dev/null 2>&1 || true; \
	done
	@for path in $(CLEAN_PATHS); do \
		rm -rf -- "$$path"; \
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
	$(COMPOSE) -p $(BRIDGE_CI_PROJECT) --profile bridge-test run --rm --build bridge-soak
	$(COMPOSE) -p $(BRIDGE_CI_PROJECT) --profile bridge-test down -v

restart-bridge:
	$(COMPOSE) up -d --build --wait --force-recreate --no-deps chatgpt-bridge

dev:
	$(COMPOSE) up --build

stop: down

PYTHON_VERSION ?= 3.12

test:
	cd backend && $(UV) run --python $(PYTHON_VERSION) pytest
	cd frontend && $(PNPM) test --run

test-integration:
	@set -eu; \
	started=0; \
	cleanup() { \
		if [ "$$started" -eq 1 ]; then \
			$(COMPOSE) --profile integration-test rm -sf postgres-test >/dev/null 2>&1 || true; \
		fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	if [ -n "$(TEST_POSTGRES_ADMIN_DSN)" ]; then \
		test_dsn="$(TEST_POSTGRES_ADMIN_DSN)"; \
	else \
		started=1; \
		$(COMPOSE) --profile integration-test up -d --wait postgres-test; \
		test_dsn="postgresql+asyncpg://postgres:postgres@127.0.0.1:$${TEST_POSTGRES_PORT:-55432}/postgres"; \
	fi; \
	cd backend; \
	TEST_POSTGRES_ADMIN_DSN="$$test_dsn" $(UV) run --python $(PYTHON_VERSION) pytest -m integration

lint:
	cd backend && $(UV) run --python $(PYTHON_VERSION) ruff check .
	cd frontend && $(PNPM) lint

typecheck:
	cd backend && $(UV) run --python $(PYTHON_VERSION) mypy src tests
	cd frontend && $(PNPM) typecheck

format:
	cd backend && $(UV) run ruff format .
	cd backend && $(UV) run ruff check --fix .
	cd frontend && $(PNPM) format

ctx:
	$(MAKE) ctx-dense

# Explicit dense build; the ordinary inspection targets below are stdlib-only.
ctx-dense:
	$(UV) run scripts/ctx/ctx.py build

# Fallback sans credentials d'embedding : construit/rafraîchit uniquement
# les chunks + l'index lexical, sans appeler le service d'embedding.
ctx-lexical:
	python3 scripts/ctx/ctx.py build --lexical-only

ctx-status:
	python3 scripts/ctx/ctx.py status

ctx-doctor:
	python3 scripts/ctx/ctx.py doctor

# Rejoue le benchmark de navigation gelé R67 (spec figée dans
# refacto_baseLine/R67_benchmark_spec.md) contre l'index lexical courant.
# Code de sortie non-zero si les seuils par défaut ne sont pas atteints.
ctx-benchmark: ctx-lexical
	env -u BASE_URL -u EMBEDDING_API_KEY python3 scripts/ctx/benchmark.py --lexical-only
