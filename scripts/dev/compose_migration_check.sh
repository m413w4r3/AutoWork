#!/usr/bin/env bash
set -Eeuo pipefail

# Run the real Compose migrate service against the profile's tmpfs PostgreSQL.
# Sanitize the environment so Compose cannot read .env or inherit provider keys.

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

project="m243-migcheck-$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/m243-migcheck.XXXXXX")"
env_file="$tmp_dir/compose.env"
override_file="$tmp_dir/compose.override.yaml"
before_dump="$tmp_dir/schema-before.sql"
after_dump="$tmp_dir/schema-after.sql"
cleanup_started=0

cat >"$env_file" <<'EOF'
POSTGRES_DB=cti_app
POSTGRES_USER=cti_app
POSTGRES_PASSWORD=local-postgres-only
TEST_POSTGRES_PORT=0
APP_ENV=test
VIRUSTOTAL_FILE_REPORT_ENABLED=false
VIRUSTOTAL_OBJECT_RELATIONSHIPS_ENABLED=false
VIRUSTOTAL_INTELLIGENCE_SEARCH_ENABLED=false
VIRUSTOTAL_BEHAVIOUR_REPORTS_ENABLED=false
VIRUSTOTAL_FILE_DOWNLOAD_ENABLED=false
VIRUSTOTAL_RETROHUNT_ENABLED=false
EOF
chmod 600 "$env_file"

# Compose interpolates from this explicit file only; env -i prevents exported
# shell values from taking precedence over the disposable database settings.
compose() {
  env -i PATH="${PATH:-/usr/bin:/bin}" HOME="${HOME:-/tmp}" \
    docker compose --env-file "$env_file" -p "$project" -f compose.yaml \
    -f "$override_file" "$@"
}

cleanup() {
  result=$?
  if [[ "$cleanup_started" -eq 1 ]]; then
    trap - EXIT
    if compose --profile integration-test rm -sf postgres-test >/dev/null && \
      compose --profile integration-test down --remove-orphans >/dev/null; then
      printf 'Disposable Compose project cleaned: %s\n' "$project"
    else
      printf 'ERROR: cleanup failed for disposable project %s\n' "$project" >&2
      [[ "$result" -ne 0 ]] || result=1
    fi
    if [[ "${backend_image:-}" == "$project-migrate" ]]; then
      if docker image rm "$backend_image" >/dev/null; then
        printf 'Disposable backend image removed: %s\n' "$backend_image"
      elif ! docker image inspect "$backend_image" >/dev/null 2>&1; then
        printf 'No disposable backend image to remove: %s\n' "$backend_image"
      else
        printf 'ERROR: failed to remove disposable image %s\n' "$backend_image" >&2
        [[ "$result" -ne 0 ]] || result=1
      fi
    fi
  fi
  rm -rf -- "$tmp_dir"
  exit "$result"
}
trap cleanup EXIT

if [[ "$project" == cti-bulletin || "$project" == cti-m14* ]]; then
  printf 'ERROR: refusing non-disposable Compose project name\n' >&2
  exit 2
fi

backend_image="$project-migrate"
baseline_sha256="$(sha256sum backend/migrations/frozen/baseline_0001.py | cut -d ' ' -f 1)"

cat >"$override_file" <<EOF
services:
  postgres:
    volumes: !reset []
  postgres-test:
    environment:
      POSTGRES_DB: cti_app
      POSTGRES_USER: cti_app
      POSTGRES_PASSWORD: local-postgres-only
    ports: !reset []
    networks:
      default:
        aliases: [postgres]
  migrate:
    image: $backend_image
    network_mode: service:postgres-test
    environment:
      POSTGRES_DSN: postgresql+asyncpg://cti_app:local-postgres-only@postgres:5432/cti_app
EOF

cleanup_started=1
compose --profile integration-test up -d --wait --pull never postgres-test

printf 'Building migrate from this checkout as %s.\n' "$backend_image"
compose --profile integration-test build migrate

compose --profile integration-test run --rm --no-deps --entrypoint sh migrate \
  -ec 'expected="$1"; actual="$(sha256sum /app/migrations/frozen/baseline_0001.py | cut -d " " -f 1)"; \
      if [ "$actual" != "$expected" ]; then \
        printf "ERROR: frozen baseline SHA-256 mismatch: checkout=%s image=%s\\n" "$expected" "$actual" >&2; \
        exit 1; \
      fi; \
      printf "Frozen baseline SHA-256 verified: %s\\n" "$actual"' \
  sh "$baseline_sha256"

compose --profile integration-test run --rm --no-deps --entrypoint python migrate \
  -c 'import os; from sqlalchemy.engine import make_url; url = make_url(os.environ["POSTGRES_DSN"]); print("Compose migrate DSN endpoint:", url.host, url.port)'

printf 'Running the Compose migrate service (alembic upgrade head).\n'
compose --profile integration-test run --rm --no-deps migrate

compose --profile integration-test exec -T postgres-test pg_dump -U cti_app -d cti_app --schema-only \
  --no-owner --no-privileges | sed '/^\\restrict /d; /^\\unrestrict /d' >"$before_dump"

POSTGRES_CONTAINER="$(compose --profile integration-test ps -q postgres-test)" \
  "${UV_PROJECT_ENVIRONMENT:-$repo_root/backend/.venv}/bin/python" \
  "$repo_root/scripts/dev/compose_migration_verify.py"

printf 'Re-running the Compose migrate service to check upgrade idempotence.\n'
compose --profile integration-test run --rm --no-deps migrate
compose --profile integration-test exec -T postgres-test pg_dump -U cti_app -d cti_app --schema-only \
  --no-owner --no-privileges | sed '/^\\restrict /d; /^\\unrestrict /d' >"$after_dump"
if ! cmp -s "$before_dump" "$after_dump"; then
  printf 'ERROR: schema-only PostgreSQL dump changed after the second upgrade.\n' >&2
  diff -u "$before_dump" "$after_dump" >&2 || true
  exit 1
fi
printf 'Idempotence verified: the second upgrade left the schema unchanged.\n'
