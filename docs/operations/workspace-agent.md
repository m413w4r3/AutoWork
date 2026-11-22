# Host workspace agent

`autowork-workspace-agent` is the host-side process that owns the investigation
workshop tree (`~/work/autowork/<DELIVERY_CODE>/<article-slug>/`). It speaks
HTTP/1.1 + JSON over an AF_UNIX socket and nothing else. The backend container
mounts only its runtime directory (read-only); the browser and the
`analysis-worker` container never see the tree.

Local workspace files are **observations, not evidence**. A file becomes
canonical only through the backend's existing `EvidenceRecorder` /
`FindingService` flows, with their usual provenance, SHA-256, G2/G3, TLP and
human-approval gates. Nothing in this document changes those rules.

## Boundary

The agent:

- opens **no TCP listener** and makes **no outbound connection** (no database,
  MinIO, provider or Bridge call) and holds no backend credential;
- only ever deletes its own `.autowork/incoming/*.part` temporaries — it never
  deletes, truncates or rewrites a user file;
- preserves and reports edits made on disk to generated/read-only files as
  `policy_violation`; it never repairs them;
- never executes samples, scripts or rules, never promotes a file to evidence,
  never auto-resolves a conflict and never infers task authorship from a path;
- is single-writer for `notes/agents/<task_id>/`; the backend task lease stays
  authoritative.

The backend and the analysis-worker get **no** workspace-tree mount. Base
samples reach a confined analysis profile only through M24-05's per-run
read-only bind.

## Runtime directory and permissions

The systemd user unit uses `%t/autowork-workspace`, i.e.
`/run/user/<uid>/autowork-workspace`:

| Entry | Mode | Notes |
|---|---|---|
| directory | `0750` | owner = host user, group = `autowork-workspace` |
| `workspace-agent.sock` | `0660` | AF_UNIX listener |
| `auth-token` | `0640` | new bearer token on **every start**; never logged |
| `agent.lock` | `0640` | `flock` instance lock |

The agent refuses to start if the directory is group/other-writable, is not a
real directory, or is owned by another uid. The token is rotated per start and
compared with `hmac.compare_digest`; the backend reloads it once on `401`.

## Install

Prerequisites: a checkout of this repository on the host, `/usr/bin/python3.12`
(3.12), and a login session for the operator (systemd `--user`). Create the
workspace root once; the unit's `ReadWritePaths=%h/work/autowork` entry must
exist before the service starts (the service then creates per-workspace dirs):

```bash
mkdir -p ~/work/autowork
```

Preview first — this writes nothing and runs no `sudo`:

```bash
scripts/install-workspace-agent.sh --dry-run
```

The script always prints, but never executes, the one-time group setup. Run it
yourself, then start a new login session (or `newgrp autowork-workspace`) so the
agent can `chgrp` its socket and token:

```bash
sudo groupadd --system autowork-workspace
sudo usermod -aG autowork-workspace "$USER"
```

Write the rendered unit (idempotent; it does **not** enable or start anything):

```bash
scripts/install-workspace-agent.sh --apply
systemctl --user daemon-reload
systemctl --user enable --now autowork-workspace-agent.service
```

Boot start without an active login additionally needs linger:

```bash
loginctl enable-linger "$USER"
```

The unit is rendered from
[`infra/systemd/autowork-workspace-agent.service`](../../infra/systemd/autowork-workspace-agent.service);
the installer substitutes `@AUTOWORK_REPOSITORY_ROOT@` for this checkout. Edit
the template, not the copy under `~/.config/systemd/user`.

For development, run the same module without a unit:

```bash
scripts/workspace-agent-dev.sh   # ./var/run/workspace-agent + ./var/workspace-agent-root
```

## Configure the backend mount

The backend container mounts the socket directory read-only at
`/run/workspace-agent` and receives:

```text
WORKSPACE_AGENT_SOCKET=/run/workspace-agent/workspace-agent.sock
WORKSPACE_AGENT_TOKEN_FILE=/run/workspace-agent/auth-token
```

The Compose default is the gitignored dev directory
`${AUTOWORK_WORKSPACE_SOCKET_DIR:-./var/run/workspace-agent}`. Production sets:

```dotenv
AUTOWORK_WORKSPACE_SOCKET_DIR=/run/user/<uid>/autowork-workspace
```

It must match the unit's `%t/autowork-workspace`. After changing it, recreate
the backend so the bind mount is refreshed (`docker compose up -d backend`).
Start the agent **before** the backend: if the source directory does not exist,
Docker creates a root-owned directory and the agent then refuses it.

`:ro` still permits AF_UNIX `connect` and forbids backend-side create/unlink,
the same precedent as `playwright-acquirer`. The backend image has no `USER`, so
file permissions do not constrain the container root; the token and the
read-only mount are the boundary. A future non-root backend uid needs
`group_add: ["${AUTOWORK_WORKSPACE_GID}"]`.

## Verify

Liveness is unauthenticated and leaks no host path:

```bash
socket=${AUTOWORK_WORKSPACE_SOCKET_DIR:-./var/run/workspace-agent}
curl --silent --unix-socket "$socket/workspace-agent.sock" http://localhost/health
# {"status":"ready","protocol":"workspace-agent.v1","instance_id":…,"pid":…,"started_at":…}
```

Authenticated routes need the per-start bearer token. Read it locally; never
paste it into a prompt, an issue, a log or a CTI report:

```bash
curl --silent --unix-socket "$socket/workspace-agent.sock" \
  -H "Authorization: Bearer $(cat "$socket/auth-token")" \
  http://localhost/v1/workspaces
```

`GET /api/workspace-agent/status` on the backend reports
`available | unavailable | unconfigured`. It is not part of global readiness.

## Fail-closed behaviour

If the socket, token or agent is missing, every workspace action returns
`503 {"error":"workspace_local_unavailable"}` and the UI shows
**Workspace local indisponible**. Actions are blocked, never queued: there is no
offline write queue. Fix the agent, then retry the action explicitly.

Typical causes:

| Symptom | Check |
|---|---|
| status `unavailable`, backend sees an empty directory | `AUTOWORK_WORKSPACE_SOCKET_DIR` matches `%t/autowork-workspace`; backend recreated after the change; source dir existed before start (owner is the host user, not root) |
| status `unconfigured` | `WORKSPACE_AGENT_SOCKET` / `WORKSPACE_AGENT_TOKEN_FILE` are empty in the backend environment |
| `401 unauthorized` after a restart | token rotated as designed; the backend retries once after reloading it — retry the action if a long request raced the restart |
| `503 workspace_local_unavailable` right after `systemctl --user start` | `systemctl --user status autowork-workspace-agent.service`; `journalctl --user -u autowork-workspace-agent.service` |
| start fails with exit 73 | another instance owns `agent.lock`; stop the duplicate (a second instance never invalidates the running token) |
| start fails on the runtime directory | wrong owner, symlink, or group/other-writable mode; recreate `%t/autowork-workspace` (`0700` is corrected to `0750`) |
| `chgrp`/`EPERM` on socket or token | the operator is not in `autowork-workspace` yet; re-login or `newgrp` |
| no agent after reboot | `loginctl enable-linger "$USER"` not set |

## Rotate or stop

The token rotates on every start; no manual rotation step exists. To restart:

```bash
systemctl --user restart autowork-workspace-agent.service
```

Uninstall:

```bash
systemctl --user disable --now autowork-workspace-agent.service
rm -f "$HOME/.config/systemd/user/autowork-workspace-agent.service"
systemctl --user daemon-reload
```

Removing the unit and `autowork-workspace` group is optional; verify no other
consumer uses them first. The workspace tree is never touched by uninstall.
