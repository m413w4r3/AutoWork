COMPOSE ?= docker compose
UV ?= uv

.PHONY: up down restart status logs test test-python test-js compose-check bridge-soak

up:
	$(COMPOSE) up -d --build --wait chatgpt-bridge

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) up -d --build --wait --force-recreate chatgpt-bridge

status:
	$(COMPOSE) ps
	$(COMPOSE) exec -T chatgpt-bridge python tools/status.py

logs:
	$(COMPOSE) logs --tail=200 -f chatgpt-bridge

test-python:
	$(UV) run --python 3.12 --with-requirements requirements-test.txt python -m pytest tests/ -q --tb=short

test-js:
	node --test \
		tests/completion.test.js \
		tests/content-dom.test.js \
		tests/final-output.test.js \
		tests/background-conversation.test.js \
		tests/content-background-tab.test.js

test: test-python test-js

compose-check:
	$(COMPOSE) config --quiet

bridge-soak:
	$(COMPOSE) -p chatgpt-bridge-soak-ci --profile bridge-test run --rm --build bridge-soak
	$(COMPOSE) -p chatgpt-bridge-soak-ci --profile bridge-test down -v
