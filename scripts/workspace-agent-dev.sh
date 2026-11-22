#!/usr/bin/env bash
# Run the AutoWork host workspace agent from this checkout for development and
# tests. The runtime directory and the workspace root default to this
# repository's gitignored ./var tree; no systemd unit is installed or started
# and ~/work/autowork is never used implicitly.

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
repository_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
socket_dir=${AUTOWORK_WORKSPACE_SOCKET_DIR:-"$repository_root/var/run/workspace-agent"}
workspace_root=${AUTOWORK_WORKSPACE_ROOT:-"$repository_root/var/workspace-agent-root"}
python_bin=${AUTOWORK_PYTHON:-/usr/bin/python3.12}

install -d -m 0750 -- "$socket_dir"
install -d -m 0700 -- "$workspace_root"

printf 'workspace agent dev run\n  repository: %s\n  socket dir: %s\n  workspace root: %s\n' \
    "$repository_root" "$socket_dir" "$workspace_root" >&2

cd -- "$repository_root"
export PYTHONPATH="$repository_root${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m sidecars.workspace_agent \
    --root "$workspace_root" \
    --socket-dir "$socket_dir" \
    --log-level "${LOG_LEVEL:-INFO}"
