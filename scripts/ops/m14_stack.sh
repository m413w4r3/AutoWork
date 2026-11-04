#!/usr/bin/env bash
# Independent M14 stack (option A of docs/mission14/M14_RESET_PLAN.md).
#
# A separate git checkout (own var/ bind mounts), a separate Compose project (own volumes and
# network) and separate host ports. The historical stack `cti-bulletin` is never touched:
# no `down`, no `-v`, no `make clean`, no deletion. `.env` is copied as a file (never printed).
#
#   scripts/ops/m14_stack.sh prepare <git-ref>   # checkout + .env overrides
#   scripts/ops/m14_stack.sh up                  # build + start (docker compose -p cti-m14)
#   scripts/ops/m14_stack.sh status              # containers, ports, volumes
#   scripts/ops/m14_stack.sh isolation-check     # proves nothing is shared with cti-bulletin
#   scripts/ops/m14_stack.sh down                # stop only the M14 project (volumes kept)
set -euo pipefail

OLD_ROOT="${M14_OLD_ROOT:-/home/nill/perso/work/AutoWork}"
STACK_DIR="${M14_STACK_DIR:-/home/nill/perso/work/AutoWork-m14-stack}"
PROJECT="${M14_PROJECT:-cti-m14}"
OLD_PROJECT="cti-bulletin"
BACKEND_PORT="${M14_BACKEND_PORT:-8100}"
FRONTEND_PORT="${M14_FRONTEND_PORT:-5273}"
MINIO_API_PORT="${M14_MINIO_API_PORT:-9100}"
MINIO_CONSOLE_PORT="${M14_MINIO_CONSOLE_PORT:-9101}"
TEST_POSTGRES_PORT="${M14_TEST_POSTGRES_PORT:-55532}"

compose() {
  local files=(-f compose.yaml)
  if docker network inspect metaharness-models >/dev/null 2>&1; then
    files+=(-f compose.models.yaml)
  fi
  (cd "$STACK_DIR" && docker compose -p "$PROJECT" "${files[@]}" "$@")
}

die() { echo "ERROR: $*" >&2; exit 1; }

cmd_prepare() {
  local ref="${1:?git ref required}"
  [ "$PROJECT" != "$OLD_PROJECT" ] || die "project name must differ from $OLD_PROJECT"
  [ "$STACK_DIR" != "$OLD_ROOT" ] || die "stack dir must differ from the historical checkout"
  if [ ! -d "$STACK_DIR/.git" ] && [ ! -f "$STACK_DIR/.git" ]; then
    git -C "$OLD_ROOT" worktree add --detach "$STACK_DIR" "$ref"
  else
    git -C "$STACK_DIR" checkout --detach "$ref"
  fi
  # Git-ignored assets the images/tests need (copied, not edited).
  for asset in chpTypst/CascadiaCode chpTypst/HankenGrotesk; do
    if [ -e "$OLD_ROOT/$asset" ] && [ ! -e "$STACK_DIR/$asset" ]; then
      cp -r "$OLD_ROOT/$asset" "$STACK_DIR/$asset"
    fi
  done
  if [ ! -f "$STACK_DIR/.env" ]; then
    cp "$OLD_ROOT/.env" "$STACK_DIR/.env"
    chmod 600 "$STACK_DIR/.env"
  fi
  if ! grep -q '^# --- M14 stack overrides ---' "$STACK_DIR/.env"; then
    {
      echo ''
      echo '# --- M14 stack overrides ---'
      echo "COMPOSE_PROJECT_NAME=$PROJECT"
      echo "BACKEND_PORT=$BACKEND_PORT"
      echo "FRONTEND_PORT=$FRONTEND_PORT"
      echo "MINIO_API_PORT=$MINIO_API_PORT"
      echo "MINIO_CONSOLE_PORT=$MINIO_CONSOLE_PORT"
      echo "TEST_POSTGRES_PORT=$TEST_POSTGRES_PORT"
      echo 'RULE_WORKBENCH_ENABLED=true'
      echo 'ADVISOR_DISCLOSURE_PROFILE=m14_research_permissive'
    } >>"$STACK_DIR/.env"
  fi
  mkdir -p "$STACK_DIR/var/workspaces/subjects" "$STACK_DIR/var/workspaces/editions" \
    "$STACK_DIR/var/diagnostics" "$STACK_DIR/var/pdf-inspections"
  echo "prepared $STACK_DIR @ $(git -C "$STACK_DIR" rev-parse --short HEAD) (project $PROJECT)"
}

cmd_up() {
  [ -f "$STACK_DIR/.env" ] || die "run prepare first"
  compose up -d --build --wait
}

cmd_down() {
  compose down
}

cmd_status() {
  compose ps
  docker volume ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}'
}

cmd_isolation_check() {
  local fail=0
  [ "$PROJECT" != "$OLD_PROJECT" ] || { echo "FAIL project name"; fail=1; }
  # 1. no volume shared: every M14 volume carries the M14 project label only.
  local shared
  shared=$(comm -12 \
    <(docker volume ls --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Name}}' | sort) \
    <(docker volume ls --filter "label=com.docker.compose.project=$OLD_PROJECT" --format '{{.Name}}' | sort))
  [ -z "$shared" ] || { echo "FAIL shared volumes: $shared"; fail=1; }
  # 2. no container of the M14 project mounts a volume/bind of the historical stack.
  local ids id
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT")
  for id in $ids; do
    docker inspect "$id" --format '{{range .Mounts}}{{.Name}} {{.Source}}{{"\n"}}{{end}}' \
      | grep -E "^${OLD_PROJECT}_|$OLD_ROOT/var" >/dev/null && { echo "FAIL $id mounts historical data"; fail=1; }
  done
  # 3. host ports differ and are bound by the M14 project only.
  for port in "$BACKEND_PORT" "$FRONTEND_PORT" "$MINIO_API_PORT" "$MINIO_CONSOLE_PORT" "$TEST_POSTGRES_PORT"; do
    owner=$(docker ps --filter "publish=$port" --format '{{.Label "com.docker.compose.project"}}')
    [ -z "$owner" ] || [ "$owner" = "$PROJECT" ] || { echo "FAIL port $port owned by $owner"; fail=1; }
  done
  # 4. var/ is not the historical var/.
  [ "$(readlink -f "$STACK_DIR/var")" != "$(readlink -f "$OLD_ROOT/var")" ] || { echo "FAIL var/ shared"; fail=1; }
  # 5. the new database is empty of investigations / invocations (fresh volumes).
  local pg
  pg=$(compose ps -q postgres 2>/dev/null || true)
  if [ -n "$pg" ]; then
    docker exec "$pg" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select (select count(*) from subjects)||chr(47)||(select count(*) from analyst_investigations)||chr(47)||(select count(*) from tool_invocations)||chr(47)||(select count(*) from model_runs)"' \
      | sed 's/^/subjects\/investigations\/invocations\/model_runs = /'
  fi
  [ "$fail" = 0 ] && echo "ISOLATION OK" || { echo "ISOLATION FAILED"; exit 1; }
}

case "${1:-}" in
  prepare) shift; cmd_prepare "$@" ;;
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  isolation-check) cmd_isolation_check ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
