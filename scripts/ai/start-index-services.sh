#!/usr/bin/env bash
set -euo pipefail

QDRANT_CONTAINER="autowork-zoo-qdrant"
QDRANT_VOLUME="autowork_zoo_qdrant_data"
EMBEDDING_MODEL="qwen3-embedding:4b"

echo "== AutoWork AI index services =="

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed"
    exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
    echo "ERROR: ollama is not installed"
    exit 1
fi

echo
echo "== Ollama =="

if ! curl -fsS http://localhost:11434 >/dev/null 2>&1; then
    echo "ERROR: Ollama API is not reachable on http://localhost:11434"
    echo "Try: sudo systemctl start ollama"
    exit 1
fi

if ! ollama ls | grep -Fq "$EMBEDDING_MODEL"; then
    echo "Pulling $EMBEDDING_MODEL..."
    ollama pull "$EMBEDDING_MODEL"
else
    echo "$EMBEDDING_MODEL already installed."
fi

echo
echo "== Qdrant =="

if docker inspect "$QDRANT_CONTAINER" >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' "$QDRANT_CONTAINER")" != "true" ]; then
        echo "Starting existing Qdrant container..."
        docker start "$QDRANT_CONTAINER" >/dev/null
    else
        echo "Qdrant already running."
    fi
else
    echo "Creating Qdrant container..."

    docker run -d \
        --name "$QDRANT_CONTAINER" \
        --restart unless-stopped \
        -p 6333:6333 \
        -v "$QDRANT_VOLUME:/qdrant/storage" \
        qdrant/qdrant >/dev/null
fi

echo
echo "Waiting for Qdrant..."

for _ in $(seq 1 30); do
    if curl -fsS http://localhost:6333/readyz >/dev/null 2>&1; then
        break
    fi

    sleep 1
done

if ! curl -fsS http://localhost:6333/readyz >/dev/null 2>&1; then
    echo "ERROR: Qdrant did not become ready."
    exit 1
fi

echo
echo "OK"
echo "Ollama : http://localhost:11434"
echo "Model  : $EMBEDDING_MODEL"
echo "Qdrant : http://localhost:6333"
