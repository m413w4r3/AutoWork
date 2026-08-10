#!/bin/sh
set -eu

# Projet isolé : le test n'arrête pas la stack de développement habituelle.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-cti-bridge-lifecycle}"
export BACKEND_PORT="${BACKEND_PORT:-18080}"
export FRONTEND_PORT="${FRONTEND_PORT:-15174}"
export MINIO_API_PORT="${MINIO_API_PORT:-19010}"
export MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-19011}"
export BRIDGE_PORT="${BRIDGE_PORT:-18001}"
export OPENAI_BRIDGE_BASE_URL="http://chatgpt-bridge:8001/v1"
export OPENAI_BRIDGE_API_KEY="${OPENAI_BRIDGE_API_KEY:-lifecycle-http-test-only}"
export BRIDGE_WS_TOKEN="${BRIDGE_WS_TOKEN:-lifecycle-websocket-test-only}"
export BRIDGE_SHUTDOWN_GRACE_SECONDS="${BRIDGE_SHUTDOWN_GRACE_SECONDS:-0.2}"

cleanup() {
    docker compose down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker compose down --remove-orphans

# Le lancement complet doit être sain même sans extension Chrome.
docker compose up -d --build --wait
docker compose ps --status running --services | grep -qx chatgpt-bridge
docker compose exec -T chatgpt-bridge python -c \
    'import json,urllib.error,urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8001/ready", timeout=2)
except urllib.error.HTTPError as exc:
    body=json.load(exc)
    assert exc.code == 503 and body["status"] == "extension_absent"'
docker compose exec -T chatgpt-bridge python -c \
    'from pathlib import Path; Path("/data/lifecycle-marker").write_text("survives-down")'

# `down` arrête le bridge sans supprimer son volume.
docker compose down
test -z "$(docker compose ps -q chatgpt-bridge)"

# Le lancement ciblé du worker doit recréer et attendre le bridge.
docker compose up -d worker
docker compose ps --status running --services | grep -qx chatgpt-bridge
docker compose ps --status running --services | grep -qx worker
docker compose exec -T worker python -c \
    'import os,socket
assert socket.gethostbyname("chatgpt-bridge")
url=os.environ["OPENAI_BRIDGE_BASE_URL"]
assert url == "http://chatgpt-bridge:8001/v1" and "localhost" not in url and "127.0.0.1" not in url'
docker compose exec -T chatgpt-bridge python -c \
    'from pathlib import Path; assert Path("/data/lifecycle-marker").read_text() == "survives-down"'

# SIGTERM interrompt prudemment un run actif ; le replay SQLite ne renvoie
# jamais le prompt à une nouvelle extension.
sigterm_id="compose-sigterm-$(date +%s%N)-$$"
sigterm_client="${COMPOSE_PROJECT_NAME}-sigterm-client"
docker compose --profile bridge-test run -d --no-deps --name "$sigterm_client" \
    -e SIGTERM_REQUEST_ID="$sigterm_id" bridge-smoke \
    python examples/sigterm_smoke.py active >/dev/null
attempt=0
until docker logs "$sigterm_client" 2>&1 | grep -q 'sigterm smoke: prompt received'; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 100 ]; then
        echo "le client SIGTERM n'a reçu aucun prompt" >&2
        exit 1
    fi
    sleep 0.1
done
bridge_log_output=$(docker compose logs chatgpt-bridge 2>&1)
if printf '%s' "$bridge_log_output" | grep -Fq "$OPENAI_BRIDGE_API_KEY" || \
    printf '%s' "$bridge_log_output" | grep -Fq "$BRIDGE_WS_TOKEN"; then
    echo "un secret est apparu dans les logs du bridge" >&2
    exit 1
fi
docker compose stop -t 30 chatgpt-bridge
# La connexion HTTP originelle peut être coupée par Uvicorn avant de recevoir
# le corps 503 ; le verdict durable est contrôlé par le replay après restart.
docker wait "$sigterm_client" >/dev/null
docker compose up -d --wait chatgpt-bridge
docker compose --profile bridge-test run --rm --no-deps \
    -e SIGTERM_REQUEST_ID="$sigterm_id" bridge-smoke \
    python examples/sigterm_smoke.py replay

docker compose down
test -z "$(docker compose ps -q chatgpt-bridge)"
trap - EXIT INT TERM
echo "bridge compose lifecycle: ok"
