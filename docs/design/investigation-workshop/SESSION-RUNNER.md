# M24-05 — Host session runner: Codex CLI and Claude Code as investigation operators

**Status:** design, normative for implementers. Base `m24/d3-session-runner-design` (integration `e21de495`).
**Scope:** the host-side session supervisor inside `autowork-workspace-agent`, the per-session MCP
tool shim, the two execution environments, the reasoning-session OS profile, the
supervisor↔backend wire, and the provider profiles. Closes the gap between ADR-0011 D7
(port, session states, per-session HMAC secret) and an actual host process runner.
**Not in scope:** task/plan/handoff aggregates (04B1/05A), broker semantics (05B2), the
analysis-worker capability registry itself (H3 below), cockpit UI (09). No production code here.
PostgreSQL/MinIO remain canonical; host files, logs and model output are never evidence.

Terminology: [GLOSSARY.md](GLOSSARY.md). Workspace contract: [WORKSPACE-AGENT.md](WORKSPACE-AGENT.md).
Operator facts: `.autowork-runs/m24/00-inventory-host-frontend.md` §A/§B.

## 1. Decisions (no options left open)

| Topic | Decision |
|---|---|
| Where the supervisor lives | New subpackage `sidecars/workspace_agent/sessions/`, same process, same AF_UNIX socket, same bearer token as the workspace agent; `/v1/sessions/**` stops returning 404 (`sidecars/workspace_agent/http_server.py:192-193,314-316`). Stdlib only, like the rest (WORKSPACE-AGENT:28). |
| Session runtime location | `${XDG_RUNTIME_DIR}/autowork-workspace/sessions/<session_id>/` (0750 dir, 0600 files), **never** in the workspace. The workspace receives only captured notes/results through the existing file routes. |
| Session secret | Derived by the backend (ADR D7:189). It transits the UDS once in `POST /v1/sessions`, is held in supervisor memory only (never disk), and is handed once over a per-session 0600 UDS socket to the shim process. Never in argv, env, config file, workspace, prompt or log. |
| Reasoning session | The CLI runs inside a user-level `bwrap` sandbox in its **own network namespace** (`--unshare-net`) with a slirp4netns gateway that has `--disable-host-loopback`; allowlisted read-only binds, rw scratch, scrubbed env, per-session `HOME`/`CODEX_HOME`/`CLAUDE_CONFIG_DIR`, cwd = session scratch. The mode is the closed `AGENT_SESSIONS_CLI_NETWORK_MODE` (`isolated_egress` default; `host_network_unsafe` is an explicit dev override). Fail-closed if `bwrap`, `slirp4netns` or the loopback attestation is unavailable. |
| Technical execution | Agent-written scripts run **only** through the attested `analysis-worker` via a new typed broker tool `analysis.run_script` (H3). Host `bwrap` is never the script boundary; no arbitrary shell is ever exposed to the model. |
| What the CLI can call | Only the generated MCP tool catalogue served by the shim: the broker tools allowed for this session, `bridge.consult` (mapped to `advisor.consult`), `task.heartbeat`, `task.submit_result`, `handoff.submit`, `note.capture`. No generic shell, HTTP, URL or path passthrough. |
| Who talks to the backend | Only the shim, a separate per-session process signed with the ADR D7 HMAC. The workspace-agent service still opens no outbound connection (WORKSPACE-AGENT:11-13); the shim is a distinct narrow client, not a second agent. |
| Session model | One session = one or more **runs**. Claude prefers one long `-p --input-format stream-json` process; Codex `exec` is one-shot and instructions become `codex exec resume` runs. The business state stays ADR D2/D7's; the host mirrors it. |
| Interrupt | SIGTERM to the run's process group, 10 s grace, SIGKILL; no CLI stop flag is required (none is printed for Codex; `claude stop` targets background sessions only). |
| Resume | Only if the real probe (version/help) plus the host spike prove it. Otherwise `409 resume_unavailable`; backend closes the old session `lost` and starts a new one with a new handoff (ADR D7:187). Never auto-relaunch. |
| Restart reconciliation | Runs are launched with `--die-with-parent` and a supervisor watchdog. A supervisor restart therefore never adopts an orphan: every non-terminal session is marked `lost`, its process group killed if still alive, and the backend reconciles. CLI memory is never the continuity mechanism; handoff is. |
| Logs | Bounded ring per session (2 MiB / 10 000 events), sanitized, in the runtime dir; pulled on demand only. Never canonical, never a transcript archive, never in PostgreSQL. |
| Output capture | At `collect` the supervisor snapshots declared scratch files into `notes/agents/<task_id>/` (`PUT files`, origin `agent`, task header) and `results/` (`PUT generated`, `generated_by=session:<id>`). File capture is not evidence; canonical attribution still needs the signed `task.submit_result` (D3/M7). |
| Skills | An allowlisted `autowork-*` skill pack is resolved by the supervisor from a configured read-only `--skills-root`, hashed, and embedded in the bounded prompt brief for both CLIs. No unapproved repo doc, AGENTS.md/CLAUDE.md or user skill is loaded implicitly. |
| Fake CLI | A deterministic scripted process in the same repo honours the supervisor's argv contract; it proves lifecycle and wire, never CLI semantics. |

## 2. Architecture and trust boundaries

```
browser ──HTTP──> backend container
                    │  application/agentic_sessions.py (05B1: state, secret, lease)
                    │  infrastructure/agentic/{host_sessions,codex_cli,claude_code}.py
                    │        httpx over AF_UNIX, bearer token (WORKSPACE-AGENT §3.4)
                    ▼
host: autowork-workspace-agent  ── /v1/sessions/** ──┐
        │ workspace tree, watcher, files              │
        └─ sessions/supervisor: state, bwrap profile ─┤
             ├─ bwrap (netns + slirp4netns) ─ CLI process group  │
             │      └─ MCP stdio ─ shim (separate) ────┼── signed HTTP ─> backend
             │             └─ per-session UDS ────────┘   /api/investigations/... routes
             └─ bounded logs, scratch, state (runtime dir)

agent script ── shim tool analysis.run_script ──> backend broker ──> analysis-worker (Docker)
```

Three boundaries: (1) the UDS + bearer token between backend and host agent; (2) `bwrap` between
the model/CLI and the host (filesystem/env); (3) the analysis-worker container between agent code
and anything else (no egress, read-only rootfs, UID 10001, `cap_drop: ALL`; `compose.yaml:423-460`).
No boundary is a prompt instruction.

## 3. Reasoning session: OS profile, secrets, skills

**Sandbox.** Launch argv is `bwrap --unshare-pid --unshare-ipc --unshare-uts --die-with-parent`
(no `--unshare-net`), with `--ro-bind /usr /usr`, conditional `/lib`, `/lib64`, the resolved CLI
runtime prefixes, `--ro-bind` of each allowed workspace path, `--ro-bind` of the session IPC
subdirectory (`<session>/ipc/`, which contains only `shim.sock`, never state or logs; connect over
a read-only bind uses the acquirer precedent, WORKSPACE-AGENT:84-99),
`--bind` of the session scratch and
per-session config, `--tmpfs /tmp`, `--proc /proc`, `--dev /dev`, `--chdir <scratch>/work`.
The same idiom already builds the analysis bwrap command
(`backend/src/cti_app/infrastructure/local_command_runner.py:893-927`); the difference is that this
profile must keep the provider network.

**Environment (closed set).** `PATH`, `HOME=<session>/home`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
`TMPDIR`, `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, `CODEX_HOME=<session>/codex` or
`CLAUDE_CONFIG_DIR=<session>/claude`, `AUTOWORK_SESSION_ID`, `AUTOWORK_SESSION_SOCKET`,
`AUTOWORK_BACKEND_URL=http://127.0.0.1:8000` (the published backend port,
`00-inventory-host-frontend.md:93`). No DB DSN, MinIO key, VT/Bridge key, proxy or
`OPERATOR_CLIENT` token. The env allowlist idiom exists for child tools
(`.../analysis_subprocess.py:91-114`) but is reimplemented in the supervisor: it is a separate
stdlib process and must not import backend code.

**Provider credentials.** The CLI needs the user's provider auth. `prepare` seeds the per-session
config from an explicit credential allowlist (Codex `auth.json`; the Claude credential file(s),
exact set proven by H0), copied 0600, deleted at `close`. This is the user's provider credential,
not an AutoWork operational secret. Residual risk: Codex's own shell runs as the same uid inside
the sandbox and can read its config; that is accepted for Codex (H0 decides whether its shell can
be disabled) and is why Claude runs `--restricted`.

**Skills.** `skill_names` arrives in `PrepareSessionV1` (derived from the task's capabilities).
The supervisor walks `--skills-root` (default `<repo>/.agents/skills`), rejects symlinks/`..`,
accepts only `autowork-*` names, concatenates bounded text (64 KiB total, one hash), and embeds it
in the prompt brief. Native skill discovery is not trusted: Claude's flags are `--bare`,
`--setting-sources`, `--strict-mcp-config`; Codex runs with `--ignore-rules` and no AGENTS.md in
its cwd.

**Honest limits (FIX-S revision).** (a) Egress is not provider-only: the sandbox's slirp4netns
gateway reaches arbitrary internet hosts. It *cannot* reach the host loopback
(`--disable-host-loopback` also refuses the `10.0.2.2` gateway alias), so the Vite proxy that
injects `OPERATOR_API_TOKEN`, the published PostgreSQL/MinIO ports and every other host-local
service are unreachable even though the model has a shell; the real host attestation proves that
refusal before the first session. (b) Same-uid processes can signal each other and read
world-readable files; `PR_SET_DUMPABLE=0` on the shim blocks ptrace/procfs memory reads (idiom
`analysis_subprocess.py:116-121`), not signals. (c) No cgroup quota: CLI memory is an
`RLIMIT_AS`/wall-clock budget, scratch size is polled, not kernel-quota'd. (d) A malicious CLI
binary is out of scope; the probe records version/help hashes. (e) If user namespaces, `bwrap`,
`slirp4netns` or the loopback attestation are unavailable, `prepare` returns
`confinement_unavailable`; there is no unconfined fallback.

## 3bis. Security — network isolation and disclosure gates (FIX-S, R03 B1/B2)

**Network mode (closed).** `AGENT_SESSIONS_CLI_NETWORK_MODE` is read by BOTH the backend and the
host supervisor and takes exactly two values:

* `isolated_egress` (default): the CLI runs under `bwrap --unshare-net`; the supervisor reads
  bwrap's `--info-fd` child pid, starts `slirp4netns --configure --disable-host-loopback` in that
  network namespace, waits for its ready fd and only then releases bwrap's `--block-fd`. The
  backend refuses the probe (`agent_session_network_isolation_unavailable`) unless the host
  attested `network_mode=isolated_egress`, `netns=true` and `loopback_isolated=true`. The host
  repeats the check, verifies the request's mode equals its own, and returns
  `confinement_unavailable` if the loopback attestation did not succeed.
* `host_network_unsafe`: explicit development override; the CLI shares the host network. The
  host must attest that same mode for the backend to open a session, so a deployment that forgot
  the override fails closed instead of silently sharing the network.

The supervisor validates the mode and tool presence at startup, runs the real loopback
attestation once per profile (a host helper binds `127.0.0.1`; the sandbox attempts to reach it;
success requires a refusal, no accepted connection and a real `tap0`), and caches it. The CLI
environment is a closed literal and `spawn` additionally rejects any key in
`FORBIDDEN_CLI_ENV_KEYS`/`_TOKEN|_SECRET|_PASSWORD|_API_KEY`, so `OPERATOR_API_TOKEN`,
`M24_AGENT_SESSION_KEY` and provider/store credentials can never reach the CLI.

**External disclosure (B2).** Opening a session (prepare/open/start/resume) first runs the
canonical gate in `application/agent_session_disclosure.py`: materialized G2 envelope, not
revoked by an `AnalystDecision`, an active OSINT policy binding when inherited,
`external_llm_allowed` on BOTH the snapshot and the envelope, source/envelope TLP at or below
the approved bound and neither RED, and the configured advisor disclosure profile. It reuses
`InvestigationService.external_llm_allowed`, `g2_envelope_authorization_state`,
`osint_effective_policy_authorizes_binding` and `AdvisorDisclosurePolicy`; it creates no second
policy. A refusal is `403 agent_session_external_llm_not_allowed` with a stable reason, before
any host call, session row, secret or workspace bind. A service built without the gate refuses
(`gate_unavailable`), never opens. The same gate is wired on the transfer path.

**Instruction delivery (R03b M1).** The same gate is re-run when a queued human
instruction may be delivered live to an idle CLI (`enqueue_instruction_with_notes`
before `send_instruction`, including the note-continuation port): a TLP change,
an `external_llm_allowed=false` update or a revocation after the opening stops
the next delivery before any host effect. Transport-only host calls (interrupt,
reconcile, collect, release) are not gated because they carry no new evidence.

**Reliability (FIX-C2).**
* A crash/exception between the three `open_session` phases can never leave an
  unopenable task: `prepare`/`start_session` failures durably mark the session
  `failed` and interrupt the host object; the sweep additionally fails any
  `provisioning`/`ready` session without a lease older than
  `M24_ORPHAN_SESSION_GRACE_SECONDS` (default 900 s, DB clock, never a
  relaunch).
* A human can release an unleased session through
  `POST /api/investigations/{id}/agent-sessions/{sid}/close`; a session the
  current task lease still owns is refused there (the task command closes it).
* Every lease/pre-lease release requests host teardown **after** its commit.
  A confirmed teardown appends the durable `host-release:<session_id>`
  `session_state_changed` task event; the same sweep re-requests teardown for a
  terminal session with a host reference and no such event, and records
  `lost_unreconciled` when the host cannot confirm — never silently assumed
  stopped.
* `contribution.import` revalidates the session/lease/fence inside the
  persisting transaction under the advisory-then-task-row order, and writes
  `contribution_imported` in that same transaction; a cancel or sweep that
  committed between the workspace read and the write is a typed refusal with
  no canonical row.

**Known gap (not implemented — R5 M4).** The instruction queue is write-only.
No production path writes `awaiting_instruction`, `enqueue_instruction`
accepts a `running` session and returns `delivered_live=false`, there is no
signed pull/list route for `queued` instructions (only operator ack by ids),
and `request_input` does not change the session state. A human instruction on a
busy session is therefore persisted but never drained; it is only re-delivered
if the session later becomes `awaiting_instruction` (no such transition exists
today), and it otherwise remains undelivered until the lease expires and the
sweep blocks the task. Fixing this requires a producer for
`awaiting_instruction` and a signed pull (or an explicit re-delivery) path.

**NOT PROVEN (H0 spike).** The real CLI accepting our argv/MCP config, the provider account and
first model turn, live resume ids, the real stream-json behaviours, and the deployment's
`bwrap`/`slirp4netns` availability in the intended systemd user unit remain host-spike items.
The attestation proves *this* host's kernel behaviour (VERIFIED locally: host loopback and
`10.0.2.2` refused, `tap0` present); it does not prove provider connectivity or that a model
with a shell cannot abuse its own provider credential.

## 4. Technical execution: confined agent scripts

**What exists today.** The closed typed catalogue only: `LocalCommandRunner` runs `file`, `strings`,
`readelf`, `objdump`, `yara`, `tshark`, `capinfos` with fixed argv from typed options
(`local_command_runner.py:486-598`), and the worker's `ConfinedAnalysisTool` is a closed enum
(`application/confined_analysis.py:78-97`; `workers/confined_analysis.py:360-467`). The worker
accepts only allowlisted commands/options (`workers/confined_analysis.py:1546-1600,2082-2128`);
there is **no** generic script or argv passthrough today.

**Decision.** Add `analysis.run_script` as a broker tool backed by a new worker tool, executed by
the attested worker — not by host `bwrap` (production confinement is the container:
`docs/operations/analysis-capabilities-2026-10-17.md:13-18`; host bwrap is unreliable:
`00-inventory-host-frontend.md:64-70`).

**New capability H3, exact requirements.**
1. New `ConfinedAnalysisTool.SCRIPT` and `ConfinedAnalysisArtifactKind.SCRIPT`; the script blob is
   content-addressed (SHA-256) and archived before dispatch. Inputs: exactly one canonical sample
   or capture SHA already associated to the investigation (same resolution as
   `confined_local_commands.py:360-389`). Workspace text inputs are deferred.
2. Interpreter allowlist v1 = `python3` only (present in the worker image,
   `infra/analysis-worker.Dockerfile:24`). No shell, no argv from the model; argv is built by the
   worker (`python3 /analysis/script.py`).
3. Execution via `run_analysis_subprocess` with `deny_network=True` (seccomp socket denial:
   `analysis_subprocess.py:116-121`), env allowlist (`:91-114`), `RLIMIT_AS`, temp cwd, output
   cap, and `directory_limit_path` for the scratch (`:277-310`). **Defect to fix in H3:**
   `run_analysis_subprocess` calls `process.kill()`, not `killpg` (`:160-165`); a script can leak
   children. H3 must `start_new_session=True` and kill the group, as `LocalCommandRunner` already
   does (`local_command_runner.py:297-306,942-946`).
4. Limits: wall 60–300 s, CPU 30–120 s, AS 256–1024 MiB, FSIZE 16 MiB, ≤512 scratch files,
   ≤64 MiB scratch, stdout/stderr ≤1 MiB each. All are request fields validated against the
   worker's attested maxima (`CONFINED_ANALYSIS_WORKER_RESOURCE_LIMIT_MAXIMUMS`,
   `application/confined_analysis.py:24-55`).
5. Inputs are staged read-only (`/analysis/input/...`); scratch is writable and disposable.
   Declared outputs (≤32 files) are hashed, tarred deterministically, uploaded to MinIO, and
   returned as refs; only a bounded preview enters the tool result.
6. Env reachability: the worker holds PG/MinIO/Redis credentials (`compose.yaml:457-462`), so the
   child env allowlist and `PR_SET_DUMPABLE=0` are mandatory; the script is untrusted data, not
   instructions. The worker network is `analysis-internal` (`compose.yaml:536`) and the seccomp
   filter denies sockets, so neither malicious infrastructure nor AutoWork stores are reachable.
7. Budgeting: the tool declares capability `analysis.exec` and a cost; G2 must grant it. Without
   the grant the tool is absent from the manifest, not failing at call time.

**Verdict:** feasible and safe under these constraints; it is a NEW capability, not a flag on
`LOCAL_COMMAND`. If H3 is deferred, `analysis.run_script` is simply not registered; the typed
tools remain and no shell fallback is offered.

## 5. Host supervisor: lifecycle, processes, logs, reconciliation

**Package (ownership per §11).** `sessions/state.py` (persisted state), `process.py` (bwrap builder, setsid, killpg,
exit capture), `logs.py` (ring, sanitization, cursor), `supervisor.py` (handlers, reconcile,
per-session IPC and one-shot secret handoff), `adapters/{base,codex,claude}.py`, `shim.py` and
`catalog.py` (shim side), `fake_cli.py` (testing). `http_server.py` gains only the session
dispatch; the workspace routes are unchanged.

**State.** Host state mirrors ADR D2's enum (`provisioning`, `ready`, `running`,
`awaiting_instruction`, `interrupt_requested`, `stopped`, `lost`, `reconciling`, `closed`,
`failed`; `ADR-0011:64`) with `state_version` CAS exactly like the workspace projections. The
authority remains the backend row; the host state is the technical mirror and is reported, never
promoted.

**Persistence.** `state.json` per session: ids, adapter kind, state, version, generation, run
number, pid + pgid + `/proc/pid/stat` start ticks, log cursor, scratch path, timestamps. Atomic
tmp+rename+fsync, 0600. Secret is **not** in this file.

**Process control.** Each run: `start_new_session=True`, record pgid, `--die-with-parent`.
Interrupt = SIGTERM pgid → wait 10 s → SIGKILL pgid; state `interrupt_requested` then `stopped`
or `lost` after reconcile. Wall-clock budget, no-output watchdog, instruction count and output cap
are enforced by the supervisor thread. No `--max-turns` exists on Claude (verified absence,
`00-inventory-host-frontend.md:36`); Codex's JSONL events are counted by the supervisor instead.

**Logs.** Per run, stdout/stderr are read in 64 KiB chunks, ANSI/control-stripped, line-capped
(8 KiB), redacted with the same rule set as `_normalize_output`
(`local_command_runner.py:631-637`: URLs, credential assignments, bearer/JWT/long tokens, private
keys), and appended to a 2 MiB ring with `{seq, generation, stream, text, truncated}`. Event log
(10 000 entries) uses the workspace-agent cursor semantics: `after=<seq>`, `generation`, and
`resync_required` + `cursor_expired` when the cursor fell out
(`sidecars/workspace_agent/events.py:21,226-240`). No prompt, evidence bytes or secret is
logged by the supervisor itself.

**Restart reconciliation (exact).** On start, scan `sessions/*/state.json`. For each non-terminal
session: if the recorded pid/starttime is alive, SIGKILL the pgid (no adoption), set `lost`,
append `reconcile_completed`; if dead, use the recorded exit if the run's exit file exists, else
`lost`. Increment generation. Emit no `state_changed` for backend-owned transitions the backend
did not request. `POST reconcile` returns the typed outcome; the backend writes
`agent_sessions.reconciliation` and never auto-relaunches (ADR D7:187).

**Output capture.** At `collect` (and `note.capture`): list only declared scratch paths
(`notes/**`, `result.md`, `final.md`), verify they are regular files (no symlink), hash them, then
write through the in-process workspace file layer with `origin=agent` + task id for
`notes/agents/<task_id>/`, and `PUT generated` `generated_by=session:<id>` for `results/`. Byte
limits: 1 MiB/note, 4 MiB total. The captured files are observations of work, never canonical
contributions.

## 6. Wire contract `/v1/sessions/**`

Same socket, bearer token, HTTP/1.1 framing, 64 KiB JSON bodies (the tool-manifest route allows
512 KiB), 16 MiB responses, closed error
envelope and `Idempotency-Key` (`^[A-Za-z0-9._:-]{8,128}$`) as WORKSPACE-AGENT §4
(`sidecars/workspace_agent/protocol.py:12-23`; idempotency enforced at
`http_server.py:322-340`). New protocol string `workspace-agent.sessions.v1`; new error codes
`session_not_found`, `session_state_conflict`, `session_busy`, `confinement_unavailable`,
`capability_unavailable`, `resume_unavailable`, `idempotency_conflict`, `budget_exceeded`,
`manifest_missing`, `secret_unavailable`. POSTs replay their stored status/body for 24 h; a
different body on the same key is `409 idempotency_conflict`. No host absolute path in any
response or error (WORKSPACE-AGENT §4).

| Method / path | Request | Response |
|---|---|---|
| `GET /v1/sessions/capabilities?adapter_kind=` | — | `CapabilityProbeV1{adapter_kind,version,binary_sha256,help_sha256,flags{non_interactive,json_events,structured_output,resume,streaming_stdin,mcp_per_invocation,os_sandbox},confinement{profile,bwrap_ok,netns:false},probed_at,probe_sha256}` |
| `POST /v1/sessions` | `PrepareSessionV1{session_id,adapter_kind,workspace_id,investigation_id,task_id,handoff{handoff_id,version,sha256},lease_fence,allowed_workspace_paths,skill_names,budgets{wall_seconds,max_output_bytes,max_instructions},model?,auth_secret(base64, one-time)}` | `SessionV1{session_id,state:"ready",state_version,capability_sha256,confinement,prompt_sha256}` |
| `POST /v1/sessions/{id}/tool-manifest` | `ToolManifestV1{manifest_sha256,tools:[{mcp_name,broker_name,schema_version,input_schema,effect,cost,capability}]}` | `{accepted,manifest_sha256}` |
| `POST /v1/sessions/{id}/start` | `{run_prompt?,instruction_id?}` | `SessionV1{state:"running",state_version,run,log_cursor}` |
| `GET /v1/sessions/{id}` | — | `SessionStatusV1{state,state_version,adapter_kind,runs,budgets_used,last_activity_at,exit?,log_cursor,checkpoint_sha256}` |
| `GET /v1/sessions/{id}/events?after=&generation=&limit=` | — | `SessionEventsPageV1{generation,events[{seq,kind,at,payload}],next_cursor,oldest_retained,resync_required}` |
| `GET /v1/sessions/{id}/logs?after=&limit=` | — | `LogPageV1{lines[{seq,stream,text,truncated}],next_cursor}` |
| `POST /v1/sessions/{id}/instructions` | `{instruction_id,text,sha256}` | `{accepted,delivered:"live"|"queued",cursor}` |
| `POST /v1/sessions/{id}/interrupt` | `{reason}` | `{state:"interrupt_requested",signal_sent}` |
| `POST /v1/sessions/{id}/resume` | `{reason,instruction_id?}` | `SessionV1{state:"running",run}` or `409 resume_unavailable` |
| `POST /v1/sessions/{id}/reconcile` | `{observed_exit?}` | `ReconcileV1{outcome:session_alive|terminated_clean|lost_with_handoff|lost_unreconciled|resume_available|resume_unavailable,evidence{last_event_seq,exit,process_start_ticks}}` |
| `POST /v1/sessions/{id}/collect` | `{capture:true}` | `SessionResultV1{outcome,exit,final_message_sha256,files[{path,sha256}],log_range{cursor_from,cursor_to},captured_notes[],collected_at}` |
| `POST /v1/sessions/{id}/close` | `{reason}` | `{state:"closed"}`; deletes secret material, socket, scratch; keeps state/log until retention |

Event kinds (closed): `state_changed`, `instruction_accepted`, `instruction_delivered`,
`tool_call_requested`, `tool_call_completed`, `tool_call_denied`, `output_chunk`, `process_started`,
`process_exited`, `budget_limit_reached`, `capture_completed`, `reconcile_completed`.
`checkpoint_sha256` at collect hashes `{state, last_event_seq, exit, captured file SHAs}` so the
backend can cite a checkpoint in `agent_sessions.checkpoint_ref` without copying host state.

## 7. The tool shim (MCP stdio)

**Process and registration.** `shim.py` is a self-contained stdlib script copied 0700 into the
session config at `prepare`; the supervisor writes and registers it, and the CLI (as MCP stdio
requires) spawns it as an MCP server. Registration mechanisms:
- Claude (VERIFIED flags): `--mcp-config '<json>'` + `--strict-mcp-config`; the JSON names the
  command `/usr/bin/python3.12 <session>/shim.py`, env `AUTOWORK_SESSION_SOCKET`,
  `AUTOWORK_SESSION_ID`, `AUTOWORK_BACKEND_URL`, and no secret
  (`00-inventory-host-frontend.md:34,48`; re-verified `claude --help` 2026-11-22).
- Codex (partially VERIFIED): `-c key=value` exists on `codex exec` and `exec resume` and
  `~/.codex/config.toml` is the base config (`codex exec --help`, re-verified). A per-session
  `CODEX_HOME` with a generated `config.toml` declaring `mcp_servers.autowork` is the primary
  mechanism; `-c mcp_servers.*` is the fallback. The exact key schema and whether `exec` exposes
  MCP tools are **UNPROVEN** and are H0's first gate. No prompt is run in the probe.

**Secret flow.** The supervisor holds the secret in memory. At startup the shim connects to
`<runtime>/sessions/<id>/ipc/shim.sock` (0600, peer uid checked), receives the secret **once**, sets
`PR_SET_DUMPABLE=0`, and drops the socket. The model cannot read shim memory, but the CLI launches
the shim itself and can read `AUTOWORK_SESSION_SOCKET` from its own environment, so it can connect
before the shim and receive the frame: that is an **accepted and bounded residual risk, not a
narrow race** (`BROKER-RELAY-EGRESS.md` §8.6). The secret grants exactly the session's own closed
authority (relay catalogue; backend HMAC, lease and binding revalidation), is destroyed at
close/revocation and re-armed per run, and `collect` refuses and revokes any agent content that
carries it. The secret never appears in the MCP config, argv or env.

**Tool catalogue.** `tools/list` returns exactly the manifest, with MCP-safe names
(`autowork_bridge_consult` ↔ broker `advisor.consult`; `autowork_<sanitized broker name>` for the
rest). `tools/call` validates the JSON schema locally, maps to the fixed backend route, signs, and
returns a bounded JSON result (256 KiB; `truncated` + refs beyond). Unknown tool or unknown
argument → JSON-RPC error, no network call. Fixed tools: `task.heartbeat`, `task.submit_result`,
`handoff.submit`, `note.capture`. There is no URL, path, method or header parameter anywhere.

**Signing.** ADR D7:189 exact scheme: `v1=<hex>` = HMAC-SHA256(secret,
`method\0path+query\0session_id\0sha256(body)\0idempotency_key\0timestamp`), headers
`X-AutoWork-Agent-Session/Timestamp/Signature`, ±300 s. `client_key` is deterministic:
`sha256("mcp.v1\0"+session_id+"\0"+broker_name+"\0"+canonical_json(arguments))`; identical calls
replay (idempotent by design), a genuine retry varies an argument (e.g. `nonce`). Backend key is
`agent:<session_id>:<task_id>:<lease_fence>:<client_key>` (ADR D7:191).

**Backend routes called.** `POST /api/investigations/{id}/agent-sessions/{sid}/tool-calls` for
broker tools (D8:203), `/tasks/{tid}/commands` for heartbeat/submit (D8:200), `/handoffs` (D8:201).
Everything else is rejected locally. `bridge.consult` is just `advisor.consult`; an ambiguous
consult returns `consultation_pending_reconciliation` and must not be resubmitted (D9).

## 8. Provider profiles and differences

| Capability | Codex 0.163.0 (VERIFIED help) | Claude Code 2.1.284 (VERIFIED help) |
|---|---|---|
| Run | `codex exec` one-shot (argv/stdin prompt) | `claude -p` |
| Streamed events | `--json` JSONL; `-o` final message | `--output-format stream-json`; `--include-partial-messages` |
| Live instruction | none printed → queued as a `codex exec resume <id> <text>` run | `--input-format stream-json` (semantics H0) |
| Resume | `codex exec resume <id>`/`--last`; resume help lacks `-C/-s/--add-dir` | `--session-id <uuid>`, `-r <uuid>`, `--fork-session` |
| Structured output | `--output-schema <file>` | `--json-schema <json>` |
| Turn budget | no flag; external event/wall budget | no `--max-turns` (verified absence); `--max-budget-usd` + external budget |
| Sandbox | run inside our bwrap; use `--dangerously-bypass-approvals-and-sandbox` (its help says it is for externally sandboxed environments) so no nested sandbox | run inside bwrap; `--permission-mode bypassPermissions`; `--restricted` removes Bash/REPL/WebFetch and confines file tools to cwd (the restricted+bypass combination is proved in H5) |
| MCP | `CODEX_HOME/config.toml` or `-c`; per-exec flag not printed → H0 | `--mcp-config` + `--strict-mcp-config` |
| Skills/rules | `--ignore-rules`; no implicit AGENTS.md in scratch cwd | `--bare`, `--setting-sources`, `--strict-mcp-config` |

Session prompt = the supervisor-built brief: task, handoff JSON (bounded), allowed paths, skill
pack, tool rules, output conventions, stop conditions. Codex's final message and Claude's result
event are parsed into `SessionResultV1`; a parse failure leaves the session result usable from
captured files and log cursor (no fabrication).

## 9. Backend adapters are thin typed clients

`application/agentic_sessions.py` (05B1) owns business state, secret derivation, lease binding and
the `AgenticOperatorPort`. New shared port `application/agentic_host_sessions.py` (DTOs mirror §6)
and `infrastructure/agentic/host_sessions.py` (httpx UDS, token reload once on 401, bounded
bodies) reuse the existing client pattern (`application/workspace_agent.py:446-531`;
`infrastructure/workspace_agent.py:340,468-469`). `infrastructure/agentic/codex_cli.py` and
`claude_code.py` implement the port by delegating to `host_sessions` with `adapter_kind` and
validating the probe's capabilities; they contain no argv, no process control, no host path and no
CLI-specific retry. `probe_capabilities` = `GET capabilities`; `prepare/start/status/instruction/
interrupt/resume/collect/reconcile` map 1:1 to §6. `request_handoff` is backend-side (05A) and
becomes an instruction + new run; it is not a host operation.

## 10. Fake CLI, CI vs host-attested

`sidecars/workspace_agent/sessions/fake_cli.py` accepts the same argv builders (adapter code is
exercised), reads `AUTOWORK_FAKE_CLI_SCENARIO` (`success`, `consult`, `hang`, `flood`, `crash`,
`resume_ok`, `resume_unavailable`, `spawn_child`, `write_note`), emits JSONL, writes `-o`, writes
scratch notes, optionally ends by signal. A separate JSON-RPC client drives the shim over stdio.

| Proved in CI (offline) | Attested only on host |
|---|---|
| Supervisor lifecycle, idempotency, state CAS, cursor/generation, log ring, killpg, restart→lost, capture writes, no host path, runtime-dir placement | Real CLI accepts our argv/config; Codex MCP schema; provider auth and first model turn; real resume IDs; real JSON/stream-json shapes; bwrap userns on the host |
| Shim tool allowlist, schema validation, HMAC vector, secret absent from argv/env/log, deterministic client key, route mapping | Real backend D8 routes with a live signed session; systemd socket/token |
| Backend adapter DTO translation with a fake host server; `test_adapter_uses_probed_modes_only` | Real analysis-worker H3 execution under Docker (offline fixture may run in CI if Docker is available) |

No CI test calls a live provider, Bridge or VT. A live CLI prompt/model call is a separate,
human-authorized host qualification, never part of the test suite.

## 11. Implementation lots, DAG refinement, red tests

H0 gates H4/H5; H1 can proceed after 02 in parallel with 05B1's backend half.

| Lot | Content / refines | Allowed paths | Red tests (first) |
|---|---|---|---|
| **H0** | Host spike (no repo code): prove Codex MCP key schema + `exec` MCP tool exposure + resume config, Claude stream-json live instruction/resume, credential file set, bwrap CLI profile, provider-only egress reality. Output `docs/operations/host-cli-spike-2026-11.md`. | docs only | fact table signed by the human; every claim flagged VERIFIED/UNPROVEN |
| **H1** | Supervisor core + fake CLI; refines **05B1** host half (sessions reserved namespace, state, logs, reconcile, capture), not the backend `agent_sessions` aggregate. | `sidecars/workspace_agent/sessions/{__init__,state,process,logs,supervisor,fake_cli}.py`; `http_server.py` sessions dispatch only; `protocol.py` session codes; `backend/tests/workspace_agent/test_sessions_*.py` | `test_sessions_lifecycle.py::test_start_once_and_replay_is_idempotent`; `::test_prepare_rejects_unknown_adapter_and_arbitrary_binary`; `::test_interrupt_kills_process_group`; `::test_restart_marks_active_lost_and_never_relaunches`; `::test_log_ring_bounded_and_cursor_expiry_resync`; `::test_session_runtime_never_in_workspace` |
| **H2** | MCP shim + signing; refines **05B2**'s caller side (broker contract already fixed by the ADR). | `sessions/{shim,catalog}.py`; `backend/tests/workspace_agent/test_session_shim_*.py` | `::test_unknown_tool_rejected_without_network`; `::test_hmac_matches_adr_d7_vector`; `::test_secret_never_in_argv_env_or_log`; `::test_bridge_consult_maps_only_to_advisor_consult`; `::test_identical_arguments_replay_client_key` |
| **H3** | Confined script capability (or explicit deferral); refines **05B2/07** with a new worker tool. | `application/confined_analysis.py`; new `application/confined_scripts.py`; `workers/confined_analysis.py`; `infrastructure/analysis_subprocess.py` (killpg only); `infrastructure/confined_analysis_dispatch.py`; `infrastructure/investigation_runtime_ports.py` (registration); tests | `test_confined_scripts.py::test_interpreter_allowlist_only`; `::test_script_has_no_network`; `::test_input_read_only_and_scratch_quota`; `::test_timeout_kills_process_group`; `::test_env_never_contains_store_credentials`; `::test_outputs_archived_and_bounded` |
| **H4** | Codex profile (argv, CODEX_HOME config, resume, final-message parsing); refines **05B3**. | `sessions/adapters/codex.py`; `backend/tests/workspace_agent/test_codex_profile*.py`; fixtures | `::test_codex_argv_uses_probed_modes_only`; `::test_codex_resume_never_relaunches_without_probe`; `::test_resume_unprints_no_invented_flags` |
| **H5** | Claude profile (stream-json, resume, restricted tools, mcp-config); refines **06**. | `sessions/adapters/claude.py`; `backend/tests/workspace_agent/test_claude_profile*.py`; fixtures | `::test_claude_argv_uses_probed_modes_only`; `::test_claude_restricted_tools_only`; `::test_claude_resume_session_id_roundtrip` |
| **B** | Backend adapters/port; refines **05B3/06** (shared client), consumes 05B1. | `application/agentic_host_sessions.py`; `infrastructure/agentic/{host_sessions,codex_cli,claude_code}.py`; `backend/tests/test_agentic_host_sessions.py`; `tests/test_codex_adapter*.py`; `tests/test_claude_code_adapter*.py` | `test_agentic_host_sessions.py::test_port_maps_one_to_one_to_session_wire`; `test_codex_adapter.py::test_adapter_uses_probed_modes_only`; `test_claude_code_adapter.py::test_adapter_uses_probed_modes_only`; `::test_backend_never_mentions_host_paths` |

DAG delta: 02 → H1 (parallel with 05B1 backend); H0 → H4/H5; H1+H2 → H4/H5; H1+05B1 → B →
05B3/06 review; H3 after 05B2, merge after 07 for result import; 08A still needs 05B1+05B2;
09C needs 07 and can surface captured notes before H3.
**No parallel file overlap:** H1 owns `http_server.py`/`protocol.py`; H2 owns shim files only; H4
and H5 own separate adapter files; B owns `infrastructure/agentic/**`.

## 12. Verified code facts (read at this HEAD)

- Session namespace reserved and 404 today: `sidecars/workspace_agent/http_server.py:192-193,314-316`;
  contract `/v1/sessions/**` reserved (M24-05): `docs/design/investigation-workshop/WORKSPACE-AGENT.md:148`.
- Workspace agent: no outbound, no listener beyond UDS, stdlib only, bearer token:
  WORKSPACE-AGENT:11-13,28-31; auth `sidecars/workspace_agent/auth.py:87-97`; limits/idempotency
  `sidecars/workspace_agent/protocol.py:12-23`; enforcement `http_server.py:322-340`; cursor
  `sidecars/workspace_agent/events.py:21,151-240`.
- Backend client/port to extend: `backend/src/cti_app/application/workspace_agent.py:446-531`;
  UDS httpx `backend/src/cti_app/infrastructure/workspace_agent.py:340,468-469`.
- ADR constraints: session states/columns `ADR-0011:64`; typed port/reconcile/no auto-relaunch
  `:187`; secret/HMAC/signature `:189`; broker key `:191`; D8 routes `:195-209`; D5 handoff
  `:155-157`; D3 lease/heartbeat `:72-125`.
- Confinement stack: bwrap+seccomp fd and ro binds
  `backend/src/cti_app/infrastructure/local_command_runner.py:253-276,893-927`; env scrub
  `:279-289`; setsid/killpg `:297-306,942-946`; rlimits bootstrap `:949-989`; worker family
  attestation `backend/src/cti_app/infrastructure/confined_local_commands.py:154-179,236-255`.
- Closed worker catalogue and no script passthrough:
  `backend/src/cti_app/application/confined_analysis.py:78-97,126-190`;
  `backend/src/cti_app/workers/confined_analysis.py:360-467,1546-1600,2082-2128`.
- `run_analysis_subprocess` env allowlist/limits/temp cwd:
  `backend/src/cti_app/infrastructure/analysis_subprocess.py:75-133`; kill-only defect `:160-165`;
  directory quota `:277-310`.
- Worker isolation: Dockerfile `infra/analysis-worker.Dockerfile:24,80-89` (python3.12, UID 10001);
  compose `compose.yaml:423-460` (read_only, cap_drop, no-new-privileges, mem/pids/cpu) and `:536`
  (`analysis-internal`); PG/MinIO creds in worker env `:457-462`.
- ToolRegistry/ToolDispatcher: `backend/src/cti_app/application/investigation_tools.py:494,506,555,762`.
- CLI facts: `00-inventory-host-frontend.md:9-50` (versions, flags, resume, MCP differences);
  bwrap 0.9.0 and userns unreliability `:56-70`; backend reachability `:93,101`. Re-verified here:
  `codex-cli 0.163.0`, `claude 2.1.284`, `codex exec -c/--config`, `codex exec resume`,
  `claude --mcp-config/--strict-mcp-config/--session-id/--input-format/--restricted`.
- Skills: `.claude/skills/autowork-*` are symlinks to `.agents/skills/autowork-*` (listed).
- No agentic adapter or session code exists: `backend/src/cti_app/infrastructure/agentic/` absent;
  `m24_agent_session_key` absent from `config.py` (05B1 adds it).

## 13. Open questions for the human (max 5)

1. Provider egress cannot be restricted without root: do you accept G2/`external_llm_allowed` as
   the only control (plus fail-closed if `bwrap` is unavailable), or is a root-level egress policy
   a deployment prerequisite for operator sessions?
2. May the supervisor copy the user's Codex/Claude provider credential file(s) into the 0600
   per-session runtime config (deleted on close), or must it bind the real credential file
   read-write (weaker isolation, preserves token refresh)?
3. H3 v1 scope: `python3` only, one canonical sample/capture input, outputs archived as a
   tar — confirm, or require `bash` and workspace-file inputs in v1?
4. Supervisor restart kills active CLI runs (`--die-with-parent`) and reconciles them `lost` with a
   handoff. Confirm this over orphan adoption (which would need secret re-issue and pgid takeover).
5. Where does the per-session model/account/budget authorization come from: the existing G2
   envelope, or a separate human choice at session preparation (which model alias per adapter)?
