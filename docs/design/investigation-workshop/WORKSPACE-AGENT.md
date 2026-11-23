# M24-02 — Host workspace agent and backend client

**Status:** design, normative for implementers. Base `m24/integration` @ `33891bf8`.
**Scope:** the host-side `autowork-workspace-agent` and the backend client that speaks to it.
**Not in scope:** task/plan/handoff aggregates, the VT bootstrap, CLI session launch (M24-05),
evidence import validators, cockpit UI. This document creates no production code.
PostgreSQL/MinIO remain canonical; nothing here promotes workspace files to evidence.

## 1. Invariants

1. The agent owns the workspace tree, opens **no network listener** (AF_UNIX only) and holds
   no DB, MinIO or provider credential; it never pushes workspace state. Only the backend
   initiates calls on the workspace routes.
   **M24.1 session exception (normative):** inside the same `autowork-workspace-agent`
   process, the session supervisor runs a per-session broker relay (`ipc/broker.sock`) that
   forwards a closed signed-route set to a fixed loopback backend URL
   (`AGENT_SESSIONS_BROKER_BACKEND_URL`, default `http://127.0.0.1:8000`), and a host CONNECT
   proxy (`ipc/egress.sock`) that tunnels allowlisted provider hosts. Those loopback/provider
   connections are made by the supervisor on behalf of the session; the sandboxed CLI agent
   (`bwrap --unshare-net`, only `lo`, no route) has **no direct outbound connection** and never
   holds a backend URL. See `sessions/relay.py`, `sessions/egress.py` and
   [BROKER-RELAY-EGRESS.md](BROKER-RELAY-EGRESS.md) §3–§5.
2. The backend container mounts **only the runtime socket directory**, never the workspace
   tree; the browser never touches the filesystem.
3. Local files are observations, not evidence. Content becomes canonical only through
   `EvidenceRecorder` (`backend/src/cti_app/application/evidence_recorder.py:58`) and
   `FindingService` (`backend/src/cti_app/application/investigation_findings.py:564`).
4. No agent operation deletes, truncates or rewrites a user file; only its own
   `.autowork/incoming/*.part` temporaries are removed.
5. `notes/agents/<task_id>/` is single-writer: the backend validates the task lease (M24-01)
   and the agent re-checks the path/task binding as defence in depth.

## 2. Decisions (no options left open)

| Topic | Decision |
|---|---|
| Location / language | `sidecars/workspace_agent/`, package `sidecars.workspace_agent`, Python 3.12 **stdlib only** |
| Distribution / start | run from the checkout: `/usr/bin/python3.12 -m sidecars.workspace_agent`; `systemd --user` unit `autowork-workspace-agent.service`; dev script instead of the unit |
| Protocol | HTTP/1.1 + JSON over AF_UNIX, versioned `/v1`, stdlib `ThreadingHTTPServer`; raw bytes for file/sample bodies |
| Auth | per-start bearer token file in the runtime dir + fs perms; no token in the workspace |
| Watcher | polling reconcile is the only watcher; no inotify, no new dependency |
| Backend client | `application/workspace_agent.py` (port, DTOs, errors) + `infrastructure/workspace_agent.py` (httpx UDS adapter) |
| Fail-closed | missing socket/token/agent → API `503 workspace_local_unavailable`, UI `Workspace local indisponible`; actions are blocked, never queued |

## 3. Package, install, start, reachability

### 3.1 Package

`sidecars/workspace_agent/` is a standalone service like `sidecars/playwright_acquirer/`
(imported by backend tests through a repository-root `sys.path` insert,
`backend/tests/test_browser_acquisition.py:16-19`). Modules: `__main__` (argparse, signals),
`protocol` (codecs, validators), `http_server` (routing, auth, framing), `auth`, `paths`
(dirfd resolution), `workspace` (manifest/ensure/layout), `scan`, `index`, `events`, `files`
(read/write/CAS/conflicts), `policies`, `samples`, `agent` (wiring, instance lock). No `pydantic`, `httpx`,
`watchdog` or `inotify`: `backend/.venv` has `httpx 0.28.1`, `pydantic 2.13.4`, `pytest 8.4.2`
and **no** watcher library.

CLI/env: `--root`/`AUTOWORK_WORKSPACE_ROOT` (default `~/work/autowork`, resolved at start,
never at import), `--socket-dir`/`AUTOWORK_WORKSPACE_SOCKET_DIR`, `--group`/`AUTOWORK_WORKSPACE_GID`,
`--poll-interval-ms 1000`, `--stability-window-ms 400`, `--log-level`.

### 3.2 Runtime directory, socket, token (exact)

- Runtime dir `${XDG_RUNTIME_DIR}/autowork-workspace` (`%t/autowork-workspace`), mode **0750**,
  owner the host user, group `autowork-workspace`. Refuse to start if group/other-writable
  (`mode & 0o022`), not a directory, or not owned by the running uid (tests may use
  `--allow-foreign-runtime-dir`). Instance lock `agent.lock` (`fcntl.flock(LOCK_EX|LOCK_NB)`; a
  second instance exits 73).
- Socket `workspace-agent.sock` mode **0660**: `umask(0o007)` then `chmod`/`chgrp`; stale path
  handling copies the acquirer (`lstat`, require `S_ISSOCK`, unlink, else fail;
  `sidecars/playwright_acquirer/server.py:200-215`).
- Token `auth-token`: `secrets.token_urlsafe(32)`, `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW` mode
  **0640**, fsync, `chgrp`; rotated on every start; never in the workspace or logs, compared
  with `hmac.compare_digest`.
- Per-workspace tree is **0700** except `base_samples/` **0750** with **0440** files (§8);
  analysis processes see it only through an explicit read-only bind mount.

### 3.3 systemd --user unit (shipped by `scripts/install-workspace-agent.sh`)

`Type=simple`, `ExecStart=/usr/bin/python3.12 -m sidecars.workspace_agent --root %h/work/autowork
--socket-dir %t/autowork-workspace --group autowork-workspace`, `Environment=PYTHONPATH=<repo>`,
`RuntimeDirectory=autowork-workspace`, `RuntimeDirectoryMode=0750`, `UMask=0007`,
`Restart=on-failure`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=read-only`,
`ReadWritePaths=%h/work/autowork %t/autowork-workspace`, `WantedBy=default.target`. Boot start
needs `loginctl enable-linger`. The install script (`scripts/install-workspace-agent.sh`) is
idempotent with a `--dry-run` printing the unit (the only mode tested in CI); sudo group creation
is printed, never executed silently. `scripts/workspace-agent-dev.sh` runs the same module on
`./var/run/workspace-agent` for dev and tests, with no unit.

### 3.4 Backend reachability (compose)

Precedent: `playwright-acquirer` binds `/run/browser/acquirer.sock` 0770; the backend mounts it
`:ro` and connects with `httpx.AsyncHTTPTransport(uds=…)` (`compose.yaml:300,592-613,652-691`;
`backend/src/cti_app/application/browser_acquisition.py:247-253`). Add to `backend` only:

```yaml
environment: {WORKSPACE_AGENT_SOCKET: /run/workspace-agent/workspace-agent.sock,
              WORKSPACE_AGENT_TOKEN_FILE: /run/workspace-agent/auth-token}
volumes: ["${AUTOWORK_WORKSPACE_SOCKET_DIR:-./var/run/workspace-agent}:/run/workspace-agent:ro"]
```

Default is the gitignored dev dir (`/var/` is ignored, `.gitignore:87`); production sets
`AUTOWORK_WORKSPACE_SOCKET_DIR=/run/user/<uid>/autowork-workspace`. `:ro` still permits UDS
`connect` (acquirer precedent) and stops backend-side create/unlink. The image has no `USER`,
so fs perms do not constrain the container root; the token and mount are the boundary; a
future non-root uid needs `group_add: ["${AUTOWORK_WORKSPACE_GID}"]`. The analysis-worker gets
**no** workspace mount here; `base_samples` reach the confined profile only through M24-05's
per-run read-only bind (§8).

### 3.5 Health and fail-closed

- `GET /health` (unauthenticated, no host path): `{status:"ready", protocol:"workspace-agent.v1",
  instance_id, pid, started_at}` — liveness only. All `/v1/**` require
  `Authorization: Bearer <token>` (401 `unauthorized`).
- Backend `GET /api/workspace-agent/status` returns `available|unavailable|unconfigured` plus
  `instance_id`; it is **not** a global readiness dependency (`/api/health/ready` keeps its
  meaning, `backend/src/cti_app/api/health.py:61`).
- Any workspace action when unavailable returns `503 {"error":"workspace_local_unavailable"}`
  and the UI text `Workspace local indisponible`. No offline write queue.

## 4. Protocol

HTTP/JSON over UDS is chosen over JSON-RPC: the backend already has a bounded UDS HTTP client
pattern (`browser_acquisition.py:247-253,322`), the repo runs a stdlib UDS HTTP server
(`sidecars/playwright_acquirer/server.py:193-220`), streaming bodies are native for samples,
and status codes/`Idempotency-Key`/`ETag` map onto the required failure and replay semantics.

Framing: `protocol_version = "HTTP/1.1"`; every response carries `Content-Length`; request
bodies require it (411 `length_required` on chunked). Limits: JSON 64 KiB; editable file
8 MiB; generated file and sample 512 MiB (streamed, never buffered whole); JSON response
16 MiB; content read 32 MiB; pages 500. Every `PUT`/`POST` requires `Idempotency-Key`
(`^[A-Za-z0-9._:-]{8,128}$`), retained 24 h or 1000 keys per workspace; replays return the
stored status and body. Errors: `{"error":code,"message":text,"retryable":bool,"details":{…}}`
with codes `invalid_request`, `invalid_path`, `unauthorized`, `workspace_not_found`,
`workspace_identity_conflict`, `workspace_unmanaged_directory`, `revision_conflict`,
`concurrent_change`, `policy_denied`, `busy`, `length_required`, `size_limit`, `hash_mismatch`,
`cursor_expired`, `not_implemented`, `internal_error`. Messages never echo absolute host paths;
logs record request id, route, workspace id, status and duration only.

### 4.1 Operations

| Method / path | Request | Response (200/201) |
|---|---|---|
| `POST /v1/workspaces/ensure` | `EnsureWorkspaceV1{delivery_code,article_slug,subject_id,investigation_id,synthesis_sha256}` | `WorkspaceV1{workspace_id,host_path,created,manifest}` |
| `GET /v1/workspaces` | — | `{workspaces:[{workspace_id,delivery_code,article_slug,host_path,generation}]}` (manifest discovery) |
| `GET /v1/workspaces/{wid}` | — | `WorkspaceV1` |
| `POST /v1/workspaces/{wid}/scan` | `{full:bool}` | `ScanResultV1{generation,latest_cursor,emitted,policy_violations}` |
| `GET /v1/workspaces/{wid}/snapshot?page_token=&limit=` | — | `SnapshotPageV1{generation,files:[FileStateV1],next_page_token}` |
| `GET /v1/workspaces/{wid}/events?after=&limit=` | — | `EventsPageV1{generation,events:[EventV1],next_cursor,oldest_retained,resync_required}` |
| `GET /v1/workspaces/{wid}/files?prefix=&page_token=&limit=` | — | `{generation,files:[FileStateV1],next_page_token}` |
| `GET /v1/workspaces/{wid}/files/content?path=` | — | raw bytes; `ETag:"sha256:<hex>"`, `X-Autowork-Sha256`, `X-Autowork-Mtime-Ns`, `X-Autowork-Size` |
| `GET /v1/workspaces/{wid}/files/revisions?path=` | — | `{revisions:[RevisionV1]}` (index-known, bounded) |
| `PUT /v1/workspaces/{wid}/files?path=` | raw bytes; `X-Autowork-Expected-Sha256` (hex or `new`), `X-Autowork-Origin: ui\|agent`, optional `X-Autowork-Task-Id` | `WriteResultV1{revision,created,already_present,conflict}` |
| `PUT /v1/workspaces/{wid}/generated?path=` | plan/VT/results export: raw bytes; `X-Autowork-Expected-Sha256`, `X-Autowork-Origin: system`, `X-Autowork-Generated-By`, `X-Autowork-Source-Sha256`, optional task id | `WriteResultV1` |
| `POST /v1/workspaces/{wid}/base-samples/{sha256}` | streamed raw bytes, `Content-Length` required | `SampleMaterializationV1{sha256,path,size,mode:"0440",already_present}` |
| `GET /v1/workspaces/{wid}/base-samples/{sha256}` | — | raw bytes (read-only source) |
| `/v1/sessions/**` | — | **reserved**: 404 `not_implemented` (M24-05) |

`FileStateV1{path,revision_id,sha256,size,mode,mtime_ns,origin,observed_at,policy_class,task_id}`;
`RevisionV1{revision_id,path,sha256,size,observed_at,origin,task_id,supersedes_revision_id}`;
`EventV1{event_id,seq,generation,kind,path,previous_path,revision_id,previous_revision_id,
sha256,size,origin,task_id,observed_at,policy_class}`. Event kinds: `file_created`,
`file_modified`, `file_renamed`, `file_deleted`, `conflict_created`, `policy_violation`,
`sample_materialized`, `sample_rejected`, `scan_completed`. `origin ∈ filesystem|ui|agent|system`;
`task_id` is only set by an API write that carried it — the watcher never infers authorship
from a folder name. On 401 the client reloads the token and retries once only when the body
can be replayed; `base-samples` POST is not retried there (the caller re-dispatches; the
operation is content-addressed by SHA-256).

## 5. Workspace identity and layout

### 5.1 Identity and manifest

`workspace_id = "ws-" + sha256(delivery_code + "\x00" + article_slug).hexdigest()[:32]`; `delivery_code`
matches `^[A-Z0-9][A-Z0-9._-]{0,63}$` (uppercase avoids case-folding collisions);
`article_slug` reuses `SLUG_PATTERN` (`backend/src/cti_app/domain/entities.py:12`), max 80
characters; `subject_id`/`investigation_id` are UUIDs; `synthesis_sha256` is 64 lowercase hex
and an **immutable creation pin**.

`.autowork/manifest.json` (atomic write; no other writer):

```json
{"schema":"autowork-workspace-manifest.v1","workspace_id":"ws-…","delivery_code":"IR2609","article_slug":"bluemoon-cn",
 "subject_id":"<uuid>","investigation_id":"<uuid>","synthesis_sha256":"<64hex>","created_at":"<RFC3339Z>",
 "created_by":"autowork-workspace-agent","layout_version":1,"identity_sha256":"<64hex>"}
```

`identity_sha256 = sha256(canonical_json(schema,workspace_id,delivery_code,article_slug,
subject_id,investigation_id,synthesis_sha256))`. An existing manifest with a different identity
(including `subject_id`/`investigation_id`) → 409 `workspace_identity_conflict`; never rename,
move or reassign. A path without a manifest is initialized only if empty, else 409
`workspace_unmanaged_directory` (no adoption of human directories, no repair of a corrupt
manifest). `ensure` is idempotent and concurrent-safe under the instance and workspace locks.

### 5.2 Layout

```
<root>/<DELIVERY_CODE>/<article-slug>/
  .autowork/{manifest.json,file-index.json,sync-state.json,incoming/}
  base_samples/  pcaps/  scripts/  regles/  discover_sample/  plans/  results/
  notes/{humaines/,bridge/,agents/<task_id>/}
  vt/{files/,ips/,domains/,urls/,behaviours/,relationships/}
```

`notes/agents/<task_id>/` is reserved for M24-05 but writable here; `.autowork/` is unaddressable by clients (`invalid_path`).

### 5.3 File policies

| Prefix | `policy_class` | Writable through | Mode |
|---|---|---|---|
| `notes/humaines/**`, `scripts/**`, `regles/**`, `discover_sample/**` | `human_editable` | `PUT files` | 0640 |
| `notes/agents/<task_id>/**` | `agent_owned` | `PUT files`, header task id must equal the path component | 0640 |
| `notes/bridge/**`, `plans/**`, `vt/**`, `results/**`, `pcaps/**` | `generated_immutable` | `PUT generated` only | 0440 |
| `base_samples/**` | `read_only` | `POST base-samples` only | 0440 |
| `.autowork/**` | `agent_internal` | none | 0600/0640 |

Route/prefix mismatch → 403 `policy_denied`. Filesystem edits to generated/read-only files are
**preserved** and reported as `policy_violation` (x-bit or post-generation modification); the
agent never repairs or deletes them, and never executes rules or scripts.

### 5.4 Path validation and race safety (exact)

1. Decode `path` once; require UTF-8 NFC; reject NUL/control characters, backslash, absolute
   paths, empty/`.`/`..` components, components > 255 bytes, total > 1024 bytes, and a first
   component `.autowork`; split on `/` only.
2. Allowlist the §5.2 top-level directories and `notes/{humaines,bridge,agents}` (`notes/agents/<task_id>`,
   task id `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`).
3. Pin the root (`os.open(root, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)`) and walk components with
   `os.open(name, O_RDONLY|O_DIRECTORY|O_NOFOLLOW, dir_fd=parent)`. Intermediate directories
   are created only in `ensure`/`scan` (`os.mkdir(name, 0o700, dir_fd=parent)`, `EEXIST`
   accepted and re-opened with `O_NOFOLLOW`).
4. After every open compare `os.fstat(fd)` `(st_dev,st_ino)` with
   `os.stat(name, dir_fd=parent, follow_symlinks=False)`; a mismatch (rename or symlink swap)
   closes, retries once, then 409 `concurrent_change`.
5. Reads: `os.open(name, O_RDONLY|O_NOFOLLOW, dir_fd=parent)`, `fstat` before/after a bounded
   read; size/mtime drift discards the read until the next scan.
6. Never use `Path.resolve()` for authorization (TOCTOU); it only produces diagnostics after a
   dirfd open. The root fd is re-verified per request; a moved/deleted workspace → 404.

Writes use the same parent fd (`O_CREAT|O_EXCL|O_NOFOLLOW`), so a swapped parent or symlink
never redirects a write outside the workspace — stricter than the `resolve()`-based checks in
`SubjectWorkspaceMaterializer` (`application/workspace.py:88-105,130-150`), kept as idioms,
not as the security model.

### 5.5 Atomic writes and mid-write detection

Write `.{name}.{uuid4}.tmp` in the destination dir with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`
0600; stream while hashing; `os.fchmod(fd, final_mode)`; `os.fsync(fd)`; close;
`os.rename(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)`; `os.fsync(parent)`; then
`stat(name, dir_fd=parent, follow_symlinks=False)` must be a regular file with the inode seen
before rename (same idiom as `_write_manifest`, `application/workspace.py:241-250`, but
openat-based). The watcher ignores `.*.tmp` and imports only after
`(st_dev,st_ino,st_size,st_mtime_ns)` is unchanged across two polls ≥ `--stability-window-ms`
(400 ms) apart, plus the §5.4.5 `fstat` check.

### 5.6 Watcher: polling reconcile (decided)

A single `scan(workspace)` is the only change detector: every `--poll-interval-ms` (1000 ms),
on `POST scan`, and at first access after start. It walks with dirfds, skips `.autowork/`,
`base_samples/` content (registered at materialization) and symlinks (reported, never
followed), and indexes files > 32 MiB metadata-only (`sha256=null`) to bound hashing. Only
`(dev,ino,size,mtime_ns)` changes trigger hashing; rename detection is best-effort on
`(dev,ino)` with a `file_deleted`+`file_created` fallback. No inotify: no offline package
exists, ctypes/libc watch management adds failure modes, and polling makes restart scan,
periodic reconcile and steady state one tested path. Injected clocks and an FS seam keep tests
deterministic. Startup discovers manifests read-only (`<root>/*/*/.autowork/manifest.json`);
each first full scan is lazy. `ensure`/`scan` are the refresh path: recreate missing layout
dirs, rewrite projections, import changes — never regenerate, rewrite or delete content files.

## 6. Revisions, events, projections, canonical ingest

`revision_id = "rev-" + sha256(workspace_id|"\x00"|path|"\x00"|content_sha256).hexdigest()[:32]` —
deterministic and restart-stable; a revision is minted only when `(path, sha256)` changes
(mtime-only changes do not). Fields: `revision_id, path, sha256, size, mode, mtime_ns,
observed_at, origin, task_id, supersedes_revision_id`. `import_status` is **canonical-only**
(the agent cannot know it); local projections record observation, never import.

Local projections (rebuildable, never authoritative): `file-index.json` =
`{schema:"autowork-workspace-index.v1",workspace_id,generation,files:{path:{revision_id,sha256,
size,mtime_ns,mode,origin,observed_at,policy_class,task_id}}}`; `sync-state.json` =
`{schema:"autowork-workspace-sync.v1",workspace_id,generation,last_seq,oldest_seq,
events:[…≤10000],idempotency:{…≤1000},conflicts:[…],policy_violations:[…]}`. Deleting either
is safe: the next start mints a new `generation`, rescans, and the backend reconciles via a
snapshot. The projection is persisted (tmp+rename+fsync) before the event is visible over
HTTP. `event_id = "evt-" + sha256(workspace_id|path|previous_revision_id|null|revision_id|
kind).hexdigest()[:32]`; replays are byte-identical. The cursor is the per-workspace `seq`;
`oldest_retained` bounds the ring; an older `after`, or a different `generation`, returns
`resync_required:true` + `cursor_expired`.

Backend canonical record (names normative; DDL owned by the M24-07 contributions lot, additive after `0018`):
`workspace_file_events` unique `(workspace_id,event_id)` with `import_status ∈
received|applied|ignored|rejected` and `imported_at`; `workspace_file_revisions` unique
`(workspace_id,revision_id)`, metadata only (**no bytes**); `workspace_sync_cursors`
`(workspace_id,generation,cursor,last_scan_at)`. Ingest is one transaction per page: insert
an event if new, apply the revision transition, advance the cursor only for a higher `seq` in
the same generation, return the stored row on replay. Bytes enter MinIO only when a human or
validated flow imports a revision into observations/findings; that flow re-reads
`GET files/content`, verifies the SHA, and uses the existing validators (invariant 4). A file
revision alone never creates an observation.

## 7. Conflicts and ownership

`PUT files` with `X-Autowork-Expected-Sha256` is CAS. If the current SHA differs and the body's
SHA differs from it, the agent writes the body to `<same dir>/<stem>.conflict-<sha8><suffix>`
(atomic, 0640; 16 hex if that name holds another sha), leaves the on-disk file untouched,
emits `conflict_created`, and returns 409 `revision_conflict` with `current`, `proposed_sha256`,
`conflict_path`, `conflict_id`. Replaying reuses the conflict file (idempotent); a body equal
to current content returns 200 `already_present:true`. The agent never auto-resolves. A human
resolution is a normal write with the chosen content and current sha; the backend records the
decision (M24-01), and the losing conflict file stays until a human moves/deletes it outside
the agent. `notes/agents/<task_id>/` writes require `X-Autowork-Task-Id == <task_id>` (403
otherwise); one task owns the subtree, backend lease enforcement authoritative.

## 8. `base_samples` isolation

Bytes come from the backend (streamed from canonical quarantine through
`POST base-samples/{sha256}`); the agent never fetches from the network. It writes
`.autowork/incoming/<uuid>.part` (0600), hashes while streaming, `fsync`s, then `chmod 0440` +
`chgrp <analysis gid>` and renames to `base_samples/<sha256>` (no extension). After rename it
re-opens with `O_RDONLY|O_NOFOLLOW`, re-hashes and requires equality; a mismatch unlinks the
file, emits `sample_rejected` and returns 422 `hash_mismatch`, leaving nothing in
`base_samples`. Re-materialization with the same hash returns `already_present:true` after
verification. `base_samples/` is 0750 with 0440 files: the owner creates entries, the analysis
group only reads; execute bits are forbidden and scans flag any x-bit file or symlink as
`policy_violation`. The agent never executes a sample or script. Exposure to the confined
profile is a per-run read-only bind mount of the exact file/directory (`bwrap --ro-bind`,
`infrastructure/local_command_runner.py:878-888`); no blanket rw mount, and the backend
container never mounts the tree.

## 9. Test plan (all offline, no root, no systemd, no Docker, no network)

Tests live in `backend/tests/workspace_agent/`, import `sidecars.workspace_agent` with the
repository-root `sys.path` precedent, and run under `backend/.venv` pytest.

- **Fakes**: `FakeClock` (monotonic + wall); an in-memory FS seam for dirfd helpers in
  scan/event tests; a scripted watcher batch driver for rename/delete (never for security).
- **Real tmpdir tests**: traversal/absolute/NUL/backslash; symlink component and swap between stat
  and open (deterministic hook + 100-iteration loop); concurrent rename; crash leaves only `.tmp`;
  mid-write not imported; restart revision ids; manifest collision; modes (0440/no x-bit, 0640,
  0750); oversized metadata-only; cursor expiry; replay.
- **Shared contract test**: golden JSON (`EnsureWorkspaceV1`, `FileStateV1`, `EventV1`,
  `WriteResultV1`, error envelope) parsed by both sides with identical accept/reject; a real
  `ThreadingHTTPServer` on a tmpdir UDS driven end to end by the real client.
- **Static oracles**: the agent imports only an allowlisted stdlib set (never
  `httpx`/`requests`/`socket.AF_INET`); the deployment test parses `compose.yaml`, the unit, `.env.example`.

## 10. Implementation tickets (each: allowed paths + red tests first)

Dependencies: W1 → W2 → {W3, W5}; W3 → W4; W1 → W6 and W7; W8 after W4/W5/W6.

- **W1 — skeleton, protocol, auth, server (M).** `sidecars/workspace_agent/{__init__,__main__,protocol,auth,http_server,agent}.py`;
  `backend/tests/workspace_agent/{conftest,test_protocol,test_http_server,test_architecture}.py`; `Makefile`.
  Red: health leaks no host path; `/v1` without token → 401; modes 0660/0640/0750; no
  `Content-Length` → 411; second instance exits 73; stdlib-only import test.
- **W2 — paths, manifest, ensure (M).** `sidecars/workspace_agent/{paths,workspace}.py`;
  `backend/tests/workspace_agent/{test_paths,test_ensure}.py`. Red: traversal/absolute/symlink
  rejected; non-empty unmanaged dir refused; idempotent ensure with stable manifest bytes;
  same path other identity → 409; deterministic `workspace_id`.
- **W3 — scan, index, events (L).** `sidecars/workspace_agent/{scan,index,events}.py`;
  `backend/tests/workspace_agent/{test_scan,test_events,test_restart_scan}.py`. Red: mid-write
  not imported until stable; one rename event (or delete+create); restart reuses revision ids;
  cursor expiry → resync; deterministic event ids; large file metadata-only.
- **W4 — files, CAS, generated, conflicts (L).** `sidecars/workspace_agent/{files,policies}.py`;
  `backend/tests/workspace_agent/{test_files,test_conflicts}.py`. Red: CAS conflict keeps both
  versions and is idempotent; generated overwrite needs matching expected sha; task-id mismatch
  → 403; replay returns the same revision; edited generated file preserved + `policy_violation`.
- **W5 — base samples (M).** `sidecars/workspace_agent/samples.py`;
  `backend/tests/workspace_agent/test_samples.py`. Red: hash mismatch leaves no file; 0440/no
  x-bit; dir 0750; re-materialization idempotent; chunked upload refused.
- **W6 — backend client + status route (L).** `backend/src/cti_app/application/workspace_agent.py`,
  `backend/src/cti_app/infrastructure/workspace_agent.py`, `backend/src/cti_app/api/workspace_agent.py`,
  `backend/src/cti_app/api/main.py`, new fields only in `config.py`;
  `backend/tests/workspace_agent/{test_backend_client,test_backend_status_api}.py`. Red:
  unavailable → 503 `workspace_local_unavailable`; token reload once on 401; bounded response
  and unknown wire fields rejected; empty socket = unconfigured; no global readiness coupling.
- **W7 — deployment (S).** `infra/systemd/autowork-workspace-agent.service`,
  `scripts/{install-workspace-agent,workspace-agent-dev}.sh`, `compose.yaml` (backend mount + env
  only), `.env.example`, `docs/operations/workspace-agent.md`,
  `backend/tests/workspace_agent/test_deployment_contract.py`. Red: socket dir `:ro`; unit has
  `RuntimeDirectory` + hardening; `.env.example` has the three vars; `--dry-run` prints a unit.
- **W8 — contract e2e and race hardening (M).** `backend/tests/workspace_agent/{test_contract_e2e,test_races}.py`,
  fixtures under `backend/tests/workspace_agent/fixtures/`. Red: golden wire fixtures round-trip
  both sides; real client↔server UDS e2e; symlink-swap race never writes outside the workspace;
  rename race neither loses nor duplicates a revision.

Validators after each ticket: `cd backend && PYTHONPATH=$PWD/src .venv/bin/python -m pytest
tests/workspace_agent -q --tb=short`; ruff/format on ticket paths; mypy strict on `src` for W6.

## 11. Open questions for the human (max 5)

1. `delivery_code` has no producer today (`rg delivery_code` finds none): is it the edition business code (e.g. `IR2609`) from M24-01, and is the uppercase regex acceptable?
2. Is `synthesis_sha256` immutable for the workspace lifetime, or must a revised synthesis be recordable without a new workspace?
3. `systemd --user` with linger (decided here) or a system-wide unit under an `autowork` user?
4. Are 1000 ms polling / 400 ms stability acceptable on the production host (NFS may differ)?
5. If the backend later runs non-root, who owns the `autowork-workspace` gid mapping?

## 12. Verified code facts (read at this HEAD)

- UDS client `browser_acquisition.py:247-253`, bounded reads `:322`, typed errors `:56-112`;
  stdlib UDS server `sidecars/playwright_acquirer/server.py:193-220` (chmod at `:215`).
- Compose `compose.yaml:300,592-613,652-691,713-718`; no `USER` in `infra/backend.Dockerfile`;
  sys.path precedent `backend/tests/test_browser_acquisition.py:16-19`; env gate `backend/tests/conftest.py:8-24`.
- Slug `domain/entities.py:12`; filenames `application/source_filenames.py:54`; workspace
  idioms `application/workspace.py:88-105,130-150,241-250`; ro-bind
  `infrastructure/local_command_runner.py:878-888`; config `config.py:77,225`; canonical ports
  `evidence_recorder.py:58,111`, `investigation_findings.py:564`; router `api/main.py:767-792`; readiness `api/health.py:61`.
- `backend/.venv`: `httpx 0.28.1`, `pydantic 2.13.4`, `pytest 8.4.2`, `ruff 0.16.3`, `mypy 1.20.2`,
  no watcher/inotify; `/usr/bin/python3.12` is 3.12.3; `os.open/stat/rename/mkdir/unlink` support
  `dir_fd`/`follow_symlinks` (checked).
