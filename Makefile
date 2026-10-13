SHELL := /bin/sh
.DEFAULT_GOAL := help

# ==============================================================================
# Tools
# ==============================================================================

DOCKER ?= docker
COMPOSE ?= $(DOCKER) compose
UV ?= uv
PNPM ?= pnpm
D2_BINARY ?= d2
TYPST_BINARY ?= typst
FONT_BUNDLE_ROOT ?= $(CURDIR)/chpTypst
export FONT_BUNDLE_ROOT

PYTHON_VERSION ?= 3.12

BACKEND_DIR := backend
FRONTEND_DIR := frontend

# Keep this in sync with compose.yaml "name:".
COMPOSE_PROJECT ?= cti-bulletin

# Application stack files. compose.models.yaml joins the external MetaHarness
# network so the services reach the ChatGPT Bridge by its Docker name; without it
# they fall back to host.docker.internal, which the loopback-only Bridge refuses.
# A recreate (make up) must never silently drop that connection, so the override
# follows the network:
#   COMPOSE_MODELS=auto  (default) use it when the network exists
#   COMPOSE_MODELS=on    require it (fails if the network is missing)
#   COMPOSE_MODELS=off   standalone compose.yaml only
MODEL_PROVIDERS_NETWORK ?= metaharness-models
COMPOSE_MODELS ?= auto
ifeq ($(COMPOSE_MODELS),auto)
COMPOSE_MODELS_ACTIVE := $(shell $(DOCKER) network inspect $(MODEL_PROVIDERS_NETWORK) >/dev/null 2>&1 && echo on || echo off)
else
COMPOSE_MODELS_ACTIVE := $(COMPOSE_MODELS)
endif
ifeq ($(COMPOSE_MODELS_ACTIVE),on)
STACK_COMPOSE = $(COMPOSE) -f compose.yaml -f compose.models.yaml
else
STACK_COMPOSE = $(COMPOSE) -f compose.yaml
endif

# PostgreSQL dedicated to integration tests.
TEST_POSTGRES_PORT ?= 55432

# Export so docker compose and shell recipes see the same values.
export TEST_POSTGRES_PORT
export TEST_POSTGRES_ADMIN_DSN

# Some UV_* environment variables can silently prevent uv from syncing the
# project or loading dependency groups. Make commands should be reproducible,
# so neutralise those variables explicitly.
UV_ENV := env \
	-u UV_NO_SYNC \
	-u UV_NO_DEFAULT_GROUPS \
	-u UV_NO_DEV \
	-u UV_NO_GROUP \
	-u UV_FROZEN

# Exact project environment required for development and tests.
UV_SYNC := $(UV_ENV) $(UV) sync \
	--locked \
	--python $(PYTHON_VERSION) \
	--group dev \
	--group analysis

# Commands using the environment synchronised by backend-sync.
# --no-sync is deliberate: backend-sync already validated and prepared it.
UV_RUN := $(UV_ENV) $(UV) run --no-sync


# ==============================================================================
# Test options
# ==============================================================================

# Additional arguments for ordinary backend pytest runs:
#
#   make test-backend PYTEST_ARGS="-x -vv"
#   make test-backend PYTEST_ARGS="tests/test_editorial.py -vv"
#
PYTEST_ARGS ?=

# Integration tests live here. Can be narrowed explicitly:
#
#   make test-integration \
#       INTEGRATION_TEST_PATH=tests/integration/production \
#       INTEGRATION_PYTEST_ARGS="-x -vv"
#
INTEGRATION_TEST_PATH ?= tests/integration
INTEGRATION_PYTEST_ARGS ?=

# Workflow emulator floors include W13's three FAST cases and W12's FULL case.
WORKFLOW_FAST_MIN ?= 10
WORKFLOW_FULL_MIN ?= 16
WORKFLOW_RESULTS_DIR ?= $(CURDIR)/var/workflow-results

# Optional command prefix used to measure the pytest process (for example,
# WORKFLOW_PYTEST_TIME_CMD="/usr/bin/time -v -o /tmp/pytest-time.txt").
WORKFLOW_PYTEST_TIME_CMD ?=


# ==============================================================================
# Application data
# ==============================================================================

CLEAN_VOLUMES := postgres_data redis_data minio_data
CLEAN_PATHS := \
	var/diagnostics/runs \
	var/workspaces/editions \
	var/workspaces/subjects


# ==============================================================================
# Phony targets
# ==============================================================================

.PHONY: \
	help \
	doctor \
	setup \
	backend-sync \
	backend-lock \
	frontend-sync \
	reset-backend-env \
	up \
	down \
	dev \
	stop \
	status \
	logs \
	clean \
	clean-dev \
	up-clean \
	test \
	test-all \
	test-backend \
	test-frontend \
	test-frontend-e2e \
	test-integration \
	test-workflow \
	test-workflow-full \
	test-workflow-browser \
	test-workflow-browser-investigation \
	test-ci-scripts \
	measure-workflow \
	integration-db-up \
	integration-db-down \
	integration-db-logs \
	lint \
	lint-backend \
	lint-frontend \
	typecheck \
	typecheck-backend \
	typecheck-frontend \
	format \
	model-run-diagnostics \
	diagnostics \
	ctx \
	ctx-dense \
	ctx-lexical \
	ctx-status \
	ctx-doctor \
	ctx-benchmark


# ==============================================================================
# Help
# ==============================================================================

help: ## Affiche les commandes disponibles
	@printf '\nAutoWork\n'
	@printf '========\n\n'
	@awk 'BEGIN {FS = ":.*## "} \
		/^[a-zA-Z0-9_.-]+:.*## / { \
			printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2 \
		}' $(MAKEFILE_LIST)
	@printf '\n'


# ==============================================================================
# Environment / bootstrap
# ==============================================================================

doctor: ## Vérifie les prérequis locaux
	@set -eu; \
	fail=0; \
	for command in "$(DOCKER)" "$(UV)" "$(PNPM)" node; do \
		if ! command -v "$$command" >/dev/null 2>&1; then \
			echo "ERREUR: commande introuvable: $$command" >&2; \
			fail=1; \
		fi; \
	done; \
	if [ "$$fail" -ne 0 ]; then \
		exit 1; \
	fi; \
	if ! $(COMPOSE) version >/dev/null 2>&1; then \
		echo "ERREUR: Docker Compose v2 est indisponible." >&2; \
		exit 1; \
	fi; \
	if ! $(DOCKER) info >/dev/null 2>&1; then \
		echo "ERREUR: le daemon Docker n'est pas accessible." >&2; \
		exit 1; \
	fi; \
	node_major="$$(node -p 'process.versions.node.split(".")[0]')"; \
	if [ "$$node_major" -lt 22 ]; then \
		echo "ERREUR: Node.js >= 22 requis, trouvé $$(node --version)." >&2; \
		exit 1; \
	fi; \
	pnpm_version="$$($(PNPM) --version)"; \
	pnpm_major="$$(printf '%s' "$$pnpm_version" | cut -d. -f1)"; \
	if [ "$$pnpm_major" -lt 10 ]; then \
		echo "ERREUR: pnpm >= 10 requis, trouvé $$pnpm_version." >&2; \
		exit 1; \
	fi; \
	./scripts/check-d2.sh "$(D2_BINARY)"; \
	./scripts/check-typst.sh "$(TYPST_BINARY)"; \
	echo "OK: Docker, Compose, uv, Node.js et pnpm sont disponibles."

setup: doctor backend-sync frontend-sync ## Prépare entièrement l'environnement de développement
	@echo
	@echo "Environnement AutoWork prêt."
	@echo "Tu peux lancer:"
	@echo "  make test"
	@echo "  make test-integration"
	@echo "  make up"

backend-sync: ## Synchronise exactement l'environnement Python depuis uv.lock
	cd $(BACKEND_DIR) && $(UV_SYNC)

backend-lock: ## Met volontairement à jour backend/uv.lock
	cd $(BACKEND_DIR) && $(UV_ENV) $(UV) lock

frontend-sync: ## Installe les dépendances frontend depuis pnpm-lock.yaml
	cd $(FRONTEND_DIR) && $(PNPM) install --frozen-lockfile

reset-backend-env: ## Supprime et recrée complètement backend/.venv
	rm -rf $(BACKEND_DIR)/.venv
	$(MAKE) backend-sync


# ==============================================================================
# Docker application stack
# ==============================================================================

up: ## Démarre la stack applicative
	@echo "Stack: compose.models.yaml override = $(COMPOSE_MODELS_ACTIVE)"
	$(STACK_COMPOSE) up -d --build --wait

down: ## Arrête la stack applicative
	$(STACK_COMPOSE) down

dev: ## Démarre la stack au premier plan
	@echo "Stack: compose.models.yaml override = $(COMPOSE_MODELS_ACTIVE)"
	$(STACK_COMPOSE) up --build

stop: down ## Alias de make down

status: ## Affiche l'état des conteneurs
	$(STACK_COMPOSE) ps

logs: ## Suit les logs principaux
	$(STACK_COMPOSE) logs --tail=200 -f \
		backend \
		worker \
		job-recovery \
		frontend


# ==============================================================================
# Cleanup
# ==============================================================================

# WARNING:
# This target deliberately removes application data.
clean: ## ATTENTION: supprime les données applicatives locales
	$(STACK_COMPOSE) down
	@for volume in $(CLEAN_VOLUMES); do \
		$(DOCKER) volume rm -f \
			"$(COMPOSE_PROJECT)_$$volume" \
			>/dev/null 2>&1 || true; \
	done
	@for path in $(CLEAN_PATHS); do \
		rm -rf -- "$$path"; \
	done
	@mkdir -p var/diagnostics
	@cat /dev/null > var/diagnostics/events.jsonl
	@echo "Données applicatives effacées."

clean-dev: ## Supprime uniquement les environnements de dépendances locaux
	rm -rf $(BACKEND_DIR)/.venv
	rm -rf $(FRONTEND_DIR)/node_modules
	@echo "Environnements de développement effacés."

up-clean: clean up ## Réinitialise les données puis redémarre la stack


# ==============================================================================
# Tests — ordinary / fast
# ==============================================================================

# Normal tests deliberately exclude tests/integration.
#
# This has two advantages:
#   1. `make test` does not unexpectedly require PostgreSQL.
#   2. pytest does not collect integration modules just to deselect them.
#
test-backend: backend-sync ## Lance les tests backend hors intégration
	cd $(BACKEND_DIR) && \
		$(UV_RUN) pytest \
			--ignore=tests/integration \
			-m "not integration" \
			$(PYTEST_ARGS)

test-frontend: frontend-sync ## Lance les tests frontend
	cd $(FRONTEND_DIR) && $(PNPM) test --run

test-frontend-e2e: frontend-sync ## Lance les tests Playwright frontend
	cd $(FRONTEND_DIR) && $(PNPM) exec playwright test

test-workflow-browser: backend-sync frontend-sync ## Vérifie W10 au navigateur avec PostgreSQL réel jetable
	@set -eu; \
	if [ -z "$${TEST_POSTGRES_ADMIN_DSN:-}" ]; then \
		echo "ERREUR: TEST_POSTGRES_ADMIN_DSN est requis pour test-workflow-browser." >&2; \
		exit 2; \
	fi; \
	results="$(abspath $(WORKFLOW_RESULTS_DIR))/browser"; \
	api_port="$${WORKFLOW_BROWSER_API_PORT:-18765}"; \
	state="$$results/state.json"; \
	server_log="$$results/server.log"; \
	mkdir -p "$$results"; \
	rm -f -- "$$state"; \
	export WORKFLOW_BROWSER_STATE="$$state"; \
	export WORKFLOW_BROWSER_API_PORT="$$api_port"; \
	export VITE_API_PROXY_TARGET="http://127.0.0.1:$$api_port"; \
	export WORKFLOW_BROWSER_OUTPUT_DIR="$$results/playwright-results"; \
	started_at="$$(date +%s)"; \
	(cd $(BACKEND_DIR) && exec $(UV_RUN) python -m tests.workflow_emulator.browser_server) >"$$server_log" 2>&1 & \
	server_pid=$$!; \
	status=0; \
	cleanup() { \
		result=$$?; \
		stop_requested=0; \
		trap - EXIT; \
		if kill -0 "$$server_pid" 2>/dev/null; then \
			if curl --silent --fail --request POST "http://127.0.0.1:$$api_port/__workflow_browser/shutdown" >/dev/null; then \
				stop_requested=1; \
			else \
				if ! kill -TERM "$$server_pid"; then \
					echo "ERREUR: impossible d'arrêter le serveur W10." >&2; \
					if [ "$$result" -eq 0 ]; then result=1; fi; \
				else \
					stop_requested=1; \
				fi; \
			fi; \
		fi; \
		if wait "$$server_pid"; then :; else \
			server_status=$$?; \
			if [ "$$result" -eq 0 ] && [ "$$stop_requested" -eq 0 ]; then result=$$server_status; fi; \
		fi; \
		exit "$$result"; \
	}; \
	trap cleanup EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	ready=0; \
	attempt=0; \
	while [ "$$attempt" -lt 90 ]; do \
		if curl --silent --fail "http://127.0.0.1:$$api_port/api/health" >/dev/null; then ready=1; break; fi; \
		if ! kill -0 "$$server_pid" 2>/dev/null; then break; fi; \
		sleep 1; \
		attempt=$$((attempt + 1)); \
	done; \
	if [ "$$ready" -ne 1 ]; then \
		echo "ERREUR: serveur W10 indisponible sur le port $$api_port; journal:" >&2; \
		sed -n '1,240p' "$$server_log" >&2; \
		exit 1; \
	fi; \
	if (cd $(FRONTEND_DIR) && $(PNPM) exec playwright test -c playwright.real.config.ts e2e-real/exclusion-real-backend.spec.ts); then :; else status=$$?; fi; \
	if [ "$$status" -eq 0 ]; then \
		if (cd $(BACKEND_DIR) && $(UV_RUN) python -m tests.workflow_emulator.browser_verify); then :; else status=$$?; fi; \
	fi; \
	finished_at="$$(date +%s)"; \
	echo "W10-browser-exclusion / elapsed_seconds=$$((finished_at - started_at))"; \
	exit "$$status"

test-workflow-browser-investigation: backend-sync frontend-sync ## Vérifie J2 enquête/avis/repin/PDF au navigateur réel
	@set -eu; \
	if [ -z "$${TEST_POSTGRES_ADMIN_DSN:-}" ]; then \
		echo "ERREUR: TEST_POSTGRES_ADMIN_DSN est requis pour test-workflow-browser-investigation." >&2; \
		exit 2; \
	fi; \
	results="$(abspath $(WORKFLOW_RESULTS_DIR))/browser-investigation"; \
	api_port="$${WORKFLOW_BROWSER_INVESTIGATION_API_PORT:-18766}"; \
	state="$$results/state.json"; \
	server_log="$$results/server.log"; \
	mkdir -p "$$results"; \
	rm -f -- "$$state"; \
	export WORKFLOW_BROWSER_STATE="$$state"; \
	export WORKFLOW_BROWSER_API_PORT="$$api_port"; \
	export VITE_API_PROXY_TARGET="http://127.0.0.1:$$api_port"; \
	export WORKFLOW_BROWSER_OUTPUT_DIR="$$results/playwright-results"; \
	started_at="$$(date +%s)"; \
	(cd $(BACKEND_DIR) && exec $(UV_RUN) python -m tests.workflow_emulator.investigation_browser_server) >"$$server_log" 2>&1 & \
	server_pid=$$!; \
	status=0; \
	cleanup() { \
		result=$$?; \
		stop_requested=0; \
		trap - EXIT; \
		if kill -0 "$$server_pid" 2>/dev/null; then \
			if curl --silent --fail --request POST "http://127.0.0.1:$$api_port/__workflow_browser/shutdown" >/dev/null; then \
				stop_requested=1; \
			else \
				if ! kill -TERM "$$server_pid"; then \
					echo "ERREUR: impossible d'arrêter le serveur J2-browser-investigation." >&2; \
					if [ "$$result" -eq 0 ]; then result=1; fi; \
				else \
					stop_requested=1; \
				fi; \
			fi; \
		fi; \
		if wait "$$server_pid"; then :; else \
			server_status=$$?; \
			if [ "$$result" -eq 0 ] && [ "$$stop_requested" -eq 0 ]; then result=$$server_status; fi; \
		fi; \
		exit "$$result"; \
	}; \
	trap cleanup EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	ready=0; \
	attempt=0; \
	while [ "$$attempt" -lt 180 ]; do \
		if curl --silent --fail "http://127.0.0.1:$$api_port/api/health" >/dev/null; then ready=1; break; fi; \
		if ! kill -0 "$$server_pid" 2>/dev/null; then break; fi; \
		sleep 1; \
		attempt=$$((attempt + 1)); \
	done; \
	if [ "$$ready" -ne 1 ]; then \
		echo "ERREUR: serveur J2 indisponible sur le port $$api_port; journal:" >&2; \
		sed -n '1,260p' "$$server_log" >&2; \
		exit 1; \
	fi; \
	if (cd $(FRONTEND_DIR) && $(PNPM) exec playwright test -c playwright.real.config.ts investigation-opinion-repin.spec.ts); then :; else status=$$?; fi; \
	if [ "$$status" -eq 0 ]; then \
		if (cd $(BACKEND_DIR) && $(UV_RUN) python -m tests.workflow_emulator.browser_verify); then :; else status=$$?; fi; \
	fi; \
	finished_at="$$(date +%s)"; \
	echo "J2-browser-investigation / elapsed_seconds=$$((finished_at - started_at))"; \
	exit "$$status"

test: test-backend test-frontend ## Lance les tests rapides backend + frontend


# ==============================================================================
# PostgreSQL integration test service
# ==============================================================================

integration-db-up: ## Démarre PostgreSQL réservé aux tests d'intégration
	$(COMPOSE) \
		--profile integration-test \
		up -d --wait postgres-test

integration-db-down: ## Supprime PostgreSQL réservé aux tests d'intégration
	$(COMPOSE) \
		--profile integration-test \
		rm -sf postgres-test

integration-db-logs: ## Affiche les logs PostgreSQL d'intégration
	$(COMPOSE) \
		--profile integration-test \
		logs --tail=200 -f postgres-test


# ==============================================================================
# Integration tests
# ==============================================================================

test-integration: backend-sync ## Lance uniquement les tests PostgreSQL d'intégration
	@set -eu; \
	owned_postgres=0; \
	cleanup() { \
		if [ "$$owned_postgres" -eq 1 ]; then \
			$(COMPOSE) \
				--profile integration-test \
				rm -sf postgres-test \
				>/dev/null 2>&1 || true; \
		fi; \
	}; \
	trap cleanup EXIT INT TERM; \
	\
	if [ -n "$${TEST_POSTGRES_ADMIN_DSN:-}" ]; then \
		test_dsn="$$TEST_POSTGRES_ADMIN_DSN"; \
		echo "Integration DB: DSN externe fourni."; \
	else \
		running_id="$$( \
			$(COMPOSE) \
				--profile integration-test \
				ps --status running -q postgres-test \
				2>/dev/null || true \
		)"; \
		\
		if [ -z "$$running_id" ]; then \
			owned_postgres=1; \
		fi; \
		\
		$(COMPOSE) \
			--profile integration-test \
			up -d --wait postgres-test; \
		\
		test_dsn="postgresql+asyncpg://postgres:postgres@127.0.0.1:$${TEST_POSTGRES_PORT}/postgres"; \
	fi; \
	\
	echo "Integration tests: $(INTEGRATION_TEST_PATH)"; \
	cd $(BACKEND_DIR); \
	TEST_POSTGRES_ADMIN_DSN="$$test_dsn" \
		$(UV_RUN) $(WORKFLOW_PYTEST_TIME_CMD) pytest \
			-m integration \
			$(INTEGRATION_TEST_PATH) \
			$(INTEGRATION_PYTEST_ARGS)

test-workflow: ## Émule les scénarios workflow_fast et vérifie JUnit + traces
	@set -u; \
	results="$(abspath $(WORKFLOW_RESULTS_DIR))/fast"; \
	rm -rf -- "$$results"; \
	mkdir -p "$$results/traces"; \
	if WORKFLOW_EMULATOR_REQUIRED=1 \
		WORKFLOW_TRACE_DIR="$$results/traces" \
		WORKFLOW_PYTEST_TIME_CMD="$(WORKFLOW_PYTEST_TIME_CMD)" \
		$(MAKE) test-integration \
			INTEGRATION_TEST_PATH=tests/workflow_emulator \
			INTEGRATION_PYTEST_ARGS="-m workflow_fast --durations=0 -o xfail_strict=true --junitxml=$$results/junit.xml"; then \
		pytest_status=0; \
	else \
		pytest_status=$$?; \
	fi; \
	if cd backend && $(UV_ENV) $(UV) run --no-sync --python $(PYTHON_VERSION) -- python -I \
		../scripts/ci/check_workflow_results.py \
			--junitxml "$$results/junit.xml" \
			--expression "workflow_fast" \
			--min-count "$(WORKFLOW_FAST_MIN)" \
			--uv "$(UV)"; then \
		guard_status=0; \
	else \
		guard_status=$$?; \
	fi; \
	if [ "$$pytest_status" -ne 0 ]; then exit "$$pytest_status"; fi; \
	exit "$$guard_status"

test-workflow-full: ## Émule workflow_fast + workflow_full et vérifie JUnit + traces
	@set -u; \
	results="$(abspath $(WORKFLOW_RESULTS_DIR))/full"; \
	rm -rf -- "$$results"; \
	mkdir -p "$$results/traces"; \
	if WORKFLOW_EMULATOR_REQUIRED=1 \
		WORKFLOW_TRACE_DIR="$$results/traces" \
		WORKFLOW_PYTEST_TIME_CMD="$(WORKFLOW_PYTEST_TIME_CMD)" \
		$(MAKE) test-integration \
			INTEGRATION_TEST_PATH=tests/workflow_emulator \
			INTEGRATION_PYTEST_ARGS="-m 'workflow_fast or workflow_full' --durations=0 -o xfail_strict=true --junitxml=$$results/junit.xml"; then \
		pytest_status=0; \
	else \
		pytest_status=$$?; \
	fi; \
	if cd backend && $(UV_ENV) $(UV) run --no-sync --python $(PYTHON_VERSION) -- python -I \
		../scripts/ci/check_workflow_results.py \
			--junitxml "$$results/junit.xml" \
			--expression "workflow_fast or workflow_full" \
			--min-count "$(WORKFLOW_FULL_MIN)" \
			--uv "$(UV)"; then \
		guard_status=0; \
	else \
		guard_status=$$?; \
	fi; \
	if [ "$$pytest_status" -ne 0 ]; then exit "$$pytest_status"; fi; \
	exit "$$guard_status"

test-ci-scripts: ## Teste sans base les scripts de garde CI
	cd backend && $(UV_ENV) $(UV) run --no-sync --python $(PYTHON_VERSION) -- python -I \
		../scripts/ci/test_check_workflow_results.py

measure-workflow: ## Mesure trois exécutions de chaque profil workflow
	./scripts/ci/measure_workflow_profiles.sh

test-all: test test-integration ## Lance tests rapides puis tests d'intégration


# ==============================================================================
# Lint
# ==============================================================================

lint-backend: backend-sync ## Ruff backend + vérification du format
	cd $(BACKEND_DIR) && $(UV_RUN) ruff check .
	cd $(BACKEND_DIR) && $(UV_RUN) ruff format --check .

lint-frontend: frontend-sync ## ESLint frontend
	cd $(FRONTEND_DIR) && $(PNPM) lint

lint: lint-backend lint-frontend ## Lance tous les linters


# ==============================================================================
# Type checking
# ==============================================================================

typecheck-backend: backend-sync ## mypy backend
	cd $(BACKEND_DIR) && $(UV_RUN) mypy src tests

typecheck-frontend: frontend-sync ## TypeScript typecheck frontend
	cd $(FRONTEND_DIR) && $(PNPM) typecheck

typecheck: typecheck-backend typecheck-frontend ## Lance tous les typechecks


# ==============================================================================
# Formatting
# ==============================================================================

format: backend-sync frontend-sync ## Formate automatiquement backend + frontend
	cd $(BACKEND_DIR) && $(UV_RUN) ruff format .
	cd $(BACKEND_DIR) && $(UV_RUN) ruff check --fix .
	cd $(FRONTEND_DIR) && $(PNPM) format


# ==============================================================================
# Diagnostics
# ==============================================================================

model-run-diagnostics: ## Diagnostic d'un model run: RUN_ID=<uuid>
	@test -n "$(RUN_ID)" || ( \
		echo "Usage: make model-run-diagnostics RUN_ID=<uuid>" >&2; \
		exit 2 \
	)
	$(STACK_COMPOSE) exec -T backend \
		python -m cti_app.model_run_diagnostics "$(RUN_ID)"

# Timeline of var/diagnostics/events.jsonl.
#
# Examples:
#   make diagnostics ARGS="--failures -v"
#   make diagnostics ARGS="merge. -n 100"
#
diagnostics: ## Explore les événements de diagnostic
	@python3 scripts/diagnostics.py $(ARGS)


# ==============================================================================
# Context tooling
# ==============================================================================

ctx: ctx-dense ## Construit le contexte dense

ctx-dense: ## Construit explicitement l'index dense
	$(UV) run scripts/ctx/ctx.py build

ctx-lexical: ## Construit uniquement l'index lexical
	python3 scripts/ctx/ctx.py build --lexical-only

ctx-status: ## Affiche l'état de l'index de contexte
	python3 scripts/ctx/ctx.py status

ctx-doctor: ## Diagnostique l'index de contexte
	python3 scripts/ctx/ctx.py doctor

ctx-benchmark: ctx-lexical ## Lance le benchmark lexical R67
	env \
		-u BASE_URL \
		-u EMBEDDING_API_KEY \
		python3 scripts/ctx/benchmark.py --lexical-only
