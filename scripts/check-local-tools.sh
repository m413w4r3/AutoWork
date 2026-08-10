#!/usr/bin/env sh
set -eu

missing=0
for tool in docker uv node pnpm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "outil manquant: $tool" >&2
    missing=1
  fi
done

exit "$missing"

