# M24.1 H0 — Host CLI spike: Codex 0.163.0 and Claude Code 2.1.284

**Status:** host spike executed 2026-11-23 on the AutoWork analysis host.
**Branch:** `m24-1/h0-real-cli-spike`, base `8da9c3c0` (integration HEAD of
`m24-1/integration`). Local commits only; nothing pushed, merged or deployed.
**Normative neighbours:** [SESSION-RUNNER](../design/investigation-workshop/SESSION-RUNNER.md)
§3/§5/§7/§8/§11 (H0), [BROKER-RELAY-EGRESS](../design/investigation-workshop/BROKER-RELAY-EGRESS.md)
§4-§6, [ADR-0011](../adr/ADR-0011-investigation-workshop-coordination.md) D7.
Legend: **ATTESTÉ** = observed on this host with the real binary; **BLOQUÉ** =
proven impossible or refused; **NON TESTÉ** = deliberately out of the bounded
budget of this spike; **INFÉRÉ** = follows from code/help but was not executed.

## 0. Authorization and bounds actually applied

Authorized (user, 2026-11-23): real Codex and Claude Code sessions, synthetic
inoffensive data, cheap model, strict cost bound, the operator's own CLI auth
scoped to a disposable profile and destroyed at close, `M24_AGENT_SESSION_KEY`
only in the test process environment (it was **not needed and never set**;
the harness generated a synthetic D7 secret in-process).

Forbidden and never done: VT/Shodan/CTI data, live CTI acquisition, Docker
stack contact (`cti-bulletin-*`, `atlas-*`, no compose/exec/stop/rm), system
permission changes, `host_network_unsafe`, `--privileged`/`--cap-add`/seccomp
unconfined, reading `.env`/`.secrets`/`var`/auth-file contents, push/merge.

Harness: disposable `/tmp/h0-*` base — synthetic workspace, synthetic
`autowork-h0-probe` skill text, synthetic D7 secret, a minimal **signed backend
double** on `127.0.0.1:<ephemeral>`, the real `autowork-workspace-agent`
supervisor on a short AF_UNIX socket dir, the real bwrap netns profile, the
real egress CONNECT proxy, the real relay, and the real CLI. Provider
credentials were copied by the adapters into the per-session config (0600)
and never displayed; the whole disposable base was deleted at close for every
run except three diagnostic runs, whose bases were destroyed immediately after
inspection. **No PostgreSQL was used in H0**: the relay target was the signed
backend double (it verifies the D7 HMAC independently); the real FastAPI +
PostgreSQL broker path is covered by the offline/PG suites, not re-proven here.

## 1. Exact environment

| Component | Version |
|---|---|
| `codex` | `codex-cli 0.163.0` (`/home/nill/.nvm/versions/node/v24.18.0/bin/codex`, Node v24.18.0) |
| `claude` | `2.1.284 (Claude Code)` (`/home/nill/.local/bin/claude` → `~/.local/share/claude/versions/2.1.284`) |
| `bwrap` | `bubblewrap 0.9.0` (`/usr/bin/bwrap`) |
| sandbox Python | `/usr/bin/python3.12` (3.12.3) |
| host | Linux 6.8.0-149-generic; user namespaces usable |

Sidecar launch (real code, unchanged):

```sh
PYTHONPATH=<repo> python3.12 -m sidecars.workspace_agent \
  --root <base>/ws --socket-dir <base>/run --log-level INFO
# env: AGENT_SESSIONS_EGRESS_PROFILE_FILE=<base>/egress.json
#      AGENT_SESSIONS_BROKER_BACKEND_URL=http://127.0.0.1:<double port>
#      AUTOWORK_SESSIONS_SKILLS_ROOT=<base>/skills
#      AUTOWORK_CODEX_BINARY=/home/nill/.nvm/versions/node/v24.18.0/bin/codex
#      AUTOWORK_CODEX_AUTH_FILE=/home/nill/.codex/auth.json
#      AUTOWORK_CLAUDE_BINARY=/home/nill/.local/bin/claude
#      AUTOWORK_CLAUDE_CREDENTIALS_FILE=/home/nill/.claude/.credentials.json
```

Egress profile used (no other host was ever allowlisted):

```json
{"schema":"autowork-session-egress.v1","profiles":{
  "codex":{"hosts":["api.openai.com","auth.openai.com","chatgpt.com"]},
  "claude":{"hosts":["api.anthropic.com"]}}}
```

Wire sequence per session: `POST /v1/workspaces/ensure` → `GET
/v1/sessions/capabilities?adapter_kind=…` → `POST /v1/sessions` →
`POST /v1/sessions/{id}/tool-manifest` → `POST …/start` → poll
`GET …/{id}`/`events`/`logs` → `POST …/interrupt` (interrupt runs) →
`POST …/collect` → `POST …/close`.

## 2. Codex 0.163.0 — ATTESTÉ

**Preflight (non-destructive, no prompt):**
- `codex exec --help`: `-c/--config`, `-m/--model`, `-C/--cd`, `--json`,
  `-o/--output-last-message`, `--skip-git-repo-check`, `--ignore-rules`,
  `--ephemeral`, `--dangerously-bypass-approvals-and-sandbox` all printed.
- `codex exec resume --help`: `--last`, `-c`, `-m`, `--json`, `-o`,
  `--skip-git-repo-check`, `--ignore-rules` printed; no stop/abort flag.
- `codex mcp add --help` + `codex mcp list` with a synthetic `CODEX_HOME`:
  the generated `[mcp_servers.autowork]` table (`command`, `args`, `env`)
  parses as a stdio server (VERIFIED). `codex debug models` lists the
  account catalogue locally (no provider call).
- `codex login status`: `Logged in using ChatGPT`. `~/.codex/auth.json` is a
  0600 regular file, copied by the adapter into the session `CODEX_HOME`.

**Session (model `gpt-6-luna`, "fast and affordable"; one trivial prompt):**
- Supervisor probe: `version 0.163.0`, `bwrap_ok=true`, `netns=true`,
  `loopback_isolated=true` (real netns attestation), `mcp_per_invocation=false`,
  `resume=false` (unproven), `next_run_instruction=true`.
- Start → `running`; the CLI ran inside `bwrap --unshare-net` behind the
  trusted entrypoint; `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY(+lowercase)=
  http://127.0.0.1:3128`, `NO_PROXY=` empty, no other proxy variable.
- **CLI proxy compliance: ATTESTÉ.** Every provider connection was a CONNECT
  seen by the real egress proxy; 63/63 CONNECTs to `chatgpt.com:443` were
  allowed in a diagnostic run. Exact hosts observed joined: **`chatgpt.com`
  only**. `api.openai.com`/`auth.openai.com` were not contacted in this
  ChatGPT-auth flow. Default-deny was exercised: `ab.chatgpt.com` (telemetry)
  and `sdmntpr{southcentralus,centralus,westus}.oaiusercontent.com` were
  **denied `host_not_allowed`** with no observed effect on the turn.
- **MCP round trip through the UDS relay: ATTESTÉ.** `codex exec` loaded the
  generated MCP server: JSONL `item.started/completed` frames of type
  `mcp_tool_call`, `server:"autowork"`, tools
  `autowork_instruction_pending` then `autowork_instruction_ack`. Both hit the
  relay and the backend double: `{"action":"pull"}` and
  `{"action":"ack","instruction_ids":["7c9e6679-…"]}`, each with a **valid D7
  HMAC signature** (verified independently by the double). A third call,
  `autowork_note_capture`, was refused by the relay because it was not in the
  session manifest (manifest enforcement observed at runtime).
- **Bounded result:** JSONL events and logs are bounded by the supervisor ring
  and output budget; the run ended `exit 0` → `awaiting_instruction`, `collect`
  outcome `success`; the model finished with `turn.completed` and usage
  `input_tokens=153943`, `cached_input_tokens=125184`, `output_tokens=1457`,
  `reasoning_output_tokens=1017` (Codex system/tool context dominates).
- **Interruption: ATTESTÉ.** A second session was interrupted 6 s into the
  model turn: SIGTERM to the process group (exit `signal 15`), state
  `stopped`, outcome `interrupted`, `collect` `interrupted`, no lingering
  process group, close clean.
- **Close/cleanup: ATTESTÉ.** relay + egress sockets unlinked, in-memory
  secret and one-shot handoff wiped, scratch removed, session `closed`,
  disposable runtime deleted (including the seeded `auth.json` copy).
- **NON TESTÉ/UNPROVEN in this spike:** resume (`exec resume --last`) with
  re-armed secret/MCP — the adapter correctly keeps `resume=false` and
  `AUTOWORK_CODEX_RESUME_PROVEN` unset; live instructions (no streaming-stdin
  flag exists; a delivered instruction rides the next prompt); final-message
  file content (`-o` file) is not wired into `collect` (see §5).

## 3. Claude Code 2.1.284 — ATTESTÉ

**Preflight:**
- The recorded real `claude --help` fixture matches 2.1.284 (sha256-checked by
  the tests). `--print`, `--output-format stream-json`, `--verbose`,
  `--model`, `--permission-mode`, `--permission-prompts`, `--disallowedTools`,
  `--allowedTools`, `--restricted`, `--bare`, `--mcp-config`,
  `--strict-mcp-config`, `--max-budget-usd`, `--no-session-persistence`,
  `--append-system-prompt`, `--session-id`, `--resume` are all printed; no
  `--max-turns`.
- Credential set proven empirically: `~/.claude/.credentials.json` (0600) is
  sufficient when seeded into `$HOME/.claude/`; `~/.claude.json` is **not**
  required for a print-mode run in a private HOME.
- `--bare` is incompatible with that login (help text: "Anthropic auth is
  strictly ANTHROPIC_API_KEY or apiKeyHelper … OAuth and keychain are never
  read"). The adapter now omits `--bare` only when
  `AUTOWORK_CLAUDE_CREDENTIALS_FILE` is configured; `--restricted` and
  `--strict-mcp-config` remain.

**Session (model `claude-haiku-4-5-20251001`, one trivial prompt):**
- Supervisor probe: `version 2.1.284`, `bwrap_ok=true`, `netns=true`,
  `loopback_isolated=true`, `mcp_per_invocation=true`, `resume=false`,
  `next_run_instruction=true`.
- **OAuth + proxy compliance: ATTESTÉ.** Only `api.anthropic.com:443` was
  ever seen by the egress proxy; no denied host was observed with
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_AUTOUPDATER=1`.
  Provider calls completed (`rate_limit_event status=allowed`; the account's
  overage is disabled/out of credits, the 5-hour window allowed the calls).
- **MCP round trip: ATTESTÉ.** The model discovered
  `mcp__autowork__autowork_instruction_pending/ack`, called pull then ack; both
  arrived at the relay and the signed backend double with valid D7 HMACs.
- **Restricted + dontAsk surface:** `--restricted` removed the code-running
  tools (Bash/PowerShell/REPL/WebFetch). `--permission-mode dontAsk
  --permission-prompts none` denied **all** tools that would prompt, including
  the MCP tools and `Write`; the profile now passes
  `--allowedTools mcp__autowork,Write,Edit`. With that, `Write` produced
  `scratch/result.md` (`File created successfully`) and `collect` captured
  `results/sessions/<session>/result.md` (sha256
  `180bb625a73bdca98cc7444b8e0da2f4bb3b827313e22ad65671b142a563175b`); no
  built-in code-running tool was re-enabled.
- Live `stream-json` frames observed and parsed: `system/init`,
  `system/thinking_tokens`, `assistant` (text / `tool_use`), `user`
  (`tool_result`), `rate_limit_event`, final `result` frame with
  `total_cost_usd`. Run exit 0 → `awaiting_instruction`, `collect` `success`.
- **Interruption: ATTESTÉ.** SIGTERM to the group 6 s into the turn, exit
  `signal 15`, `stopped`/`interrupted`, no lingering group, clean close.
- **Costs observed:** `total_cost_usd` 0.0410 / 0.0331 / 0.0280 for the three
  completed-or-partial diagnostic sessions (≈ **USD 0.10** total), under the
  `--max-budget-usd 1.00` bound; the interrupt run printed no final frame.
- **NON TESTÉ/UNPROVEN:** `--input-format stream-json` live instruction
  semantics (`streaming_stdin=false`); resume via `--session-id`/`--resume`
  (`resume=false`, `AUTOWORK_CLAUDE_RESUME_PROVEN` unset); whether
  `--restricted` truly disables subagent/hook surfaces beyond the init tool
  list (the init frame still lists `Task`, `ListAgents`, …); final-message
  richness beyond the captured `result.md`.

## 4. Negative-space matrix (offline, no provider traffic)

Run inside the production bwrap netns with the real entrypoint forwarder and
the real `EgressProxy` (injected resolver/connector; only an allowed synthetic
verdict is dialed). Raw result:

| Probe | Result |
|---|---|
| direct `10.0.0.1:443` / `192.168.0.1:443` / `192.0.2.1:443` | `ENETUNREACH` (101) |
| direct `172.17.0.1:443` (Docker bridge) | `ENETUNREACH` (101) |
| direct host loopback service `127.0.0.1:<host port>` | refused (111); host listener accepted **0** connections |
| CONNECT `127.0.0.1:443`, `172.17.0.1:443` | `403 ip_literal` |
| CONNECT `api.openai.com:444` | `400 port_not_allowed` |
| CONNECT `example.com:443` (not allowlisted) | `403 host_not_allowed` |
| CONNECT allowed name resolving to loopback / Docker / mixed public+loopback | `403 address_forbidden` (the whole CONNECT is refused) |
| CONNECT allowed name resolving only to a public address | `200 Connection Established` + tunnel echo; only the public IP dialed |

The production netns attestation additionally proves only `lo`, no IPv4/IPv6
route, usable loopback, and refused non-loopback connects before any CLI is
released.

## 5. Defects found by the spike and fixed (local commits)

| Commit | Fix | Evidence |
|---|---|---|
| `1732db26` | Spawn the sandbox from a long-lived anchor thread. bwrap `--die-with-parent` (PR_SET_PDEATHSIG) follows the **spawning thread**; the supervisor spawned from the short-lived HTTP handler thread, so every long run was SIGKILLed (18 ms–4 s) with no caller-side signal. | Offline proof: child alive while the spawning thread lives, SIGKILLed when it exits; regression oracle `test_real_spawn_survives_a_short_lived_calling_thread` |
| `90ac09d9` | Codex installed no shim at all; both adapters copied `shim.py` without `catalog.py` (the shim imports it as a sibling). Both now install the exact two modules, 0700. | Real CLI startup would otherwise have a dead MCP server; profile tests |
| `921781fb` | Codex bound no `/etc/ssl`: bwrap's tmpfs root hid the trust store; 63 CONNECTs were allowed but every TLS request failed ("workspace routing discovery failed"). The adapter now binds the same read-only trust anchors as Claude. | Before: 63 allowed CONNECTs / 0 requests; after: first request reached the provider (HTTP 400 model-not-supported, then a successful turn) |
| `8c6b83b4` | Claude `--bare` never reads OAuth. `AUTOWORK_CLAUDE_CREDENTIALS_FILE` seeds `$HOME/.claude/.credentials.json` 0600 and the adapter omits `--bare`. | Pre-fix: no auth; post-fix: OAuth turn and OAuth rate-limit frames |
| `ae6aaf94` | `dontAsk` denied the MCP tools. `--allowedTools mcp__autowork` only (no built-in re-enabled). | Pre-fix `permission_denied`; post-fix pull+ack with valid HMACs |
| `8e7408c7` | `dontAsk` denied `Write`. Allow `Write,Edit` next to the MCP server; `--restricted` keeps them confined to cwd/`--add-dir`. | Pre-fix `permission_denied` on Write; post-fix `result.md` captured |

## 6. ATTESTÉ vs BLOQUÉ vs NON TESTÉ (per CLI)

| Capability | Codex 0.163.0 | Claude 2.1.284 |
|---|---|---|
| Non-interactive real session | **ATTESTÉ** | **ATTESTÉ** |
| Provider auth from the operator's own login | **ATTESTÉ** (ChatGPT `auth.json` copy) | **ATTESTÉ** (`~/.claude/.credentials.json` copy, no `--bare`) |
| `HTTPS_PROXY` honoured (CONNECT via the real proxy) | **ATTESTÉ** | **ATTESTÉ** |
| Default-deny egress; telemetry/updates excluded | **ATTESTÉ** (`ab.chatgpt.com`, `sdmntpr*` denied) | **ATTESTÉ** (only `api.anthropic.com`, no denial needed) |
| Host loopback / LAN / Docker unreachable | **ATTESTÉ** (netns + matrix) | **ATTESTÉ** (netns + matrix) |
| MCP config schema + `exec`/`-p` tool exposure | **ATTESTÉ** (`mcp_tool_call` events) | **ATTESTÉ** (`mcp__autowork__*` tool results) |
| Broker call round trip through the UDS relay with D7 HMAC | **ATTESTÉ** (`instruction.pending`, `instruction.ack`) | **ATTESTÉ** (same) |
| Manifest enforcement at the relay | **ATTESTÉ** (`note.capture` refused) | INFÉRÉ (same relay path; not attempted) |
| Interruption (SIGTERM group, no leftovers) | **ATTESTÉ** | **ATTESTÉ** |
| Bounded output / clean exit | **ATTESTÉ** | **ATTESTÉ** |
| File result captured by `collect` | BLOQUÉ by the manifest used (the model chose `note.capture`, absent from the manifest; `-o` not wired) | **ATTESTÉ** (`result.md`, sha256 above) |
| Resume / re-armed secret for a second run | NON TESTÉ (adapter refuses by default) | NON TESTÉ (adapter refuses by default) |
| Live instruction while running | NON TESTÉ (no CLI streaming flag) | NON TESTÉ (`--input-format stream-json` untested) |
| Final message wired into `collect` | BLOQUÉ (code gap, §7) | BLOQUÉ (code gap, §7) |

## 7. Remaining risks and follow-ups (not fixed here)

1. **Credential lifetime on close (production).** `SessionSupervisor.close()`
   unlinks sockets/scratch and wipes the in-memory secret, but keeps
   `config/` and `home/`, which would retain the seeded provider credential
   copy. The design says the credential is deleted at close. The spike
   destroyed the disposable runtime, so nothing persisted here; the
   supervisor fix is required before real sessions are enabled in a
   deployment (H5/LIFE-1).
2. **`collect` never reads the real final message.** Codex `-o
   final-message.txt` and Claude's `result` frame are parsed by helpers that
   `collect` does not call (`final_message_sha256` is always `null` for real
   adapters). The captured-file path works.
3. **Codex final-message / `note.capture` capture path.** The backend manifest
   did not include `note.capture`; the CLI then had no way to produce a
   captured result. The production manifest/row set is the backend's own
   (`host_operator.py`), which does include it; this harness manifest simply
   omitted it — verify end-to-end with the real backend in the F4/F7 gate.
4. **Adapter capability tables still declare some now-attested facts
   `proven=false`** (`mcp_config_toml_schema`, `mcp_exec_tool_exposure`,
   `jsonl_event_schema`, `provider_credentials`, `cli_interrupt` for Codex).
   The H4/H5 owners should flip them from this evidence; H0 did not change the
   declared capability surface.
5. **Claude live-instruction and resume remain unproven**, as do Codex
   resume and any "disable the CLI shell" control. The adapters keep their
   conservative branches; nothing was widened.
6. **Codex telemetry**: `ab.chatgpt.com` is denied by default and the turn
   succeeded. If a future CLI build requires it, add it explicitly (never a
   wildcard).
7. **Real backend/PostgreSQL path not exercised in H0** (by design/budget):
   the signed target was a loopback double that independently verifies the D7
   HMAC; the FastAPI broker route and PG are covered by the offline and PG
   suites and by the future F7 browser gate.
8. **`Task`/subagent surfaces in Claude's init tool list** under
   `--restricted` were not probed; if a model can spawn a subagent that gains
   tools, the profile needs an explicit denial. Report as UNKNOWN.

## 8. Real provider calls actually made (cost accounting)

Diagnostic sessions, in order (each = one `codex exec`/`claude -p` process; a
session may have produced several HTTP requests):

- Codex: 10 sessions — 5 killed by the spawn-thread defect before or during
  the first request (runs 1–5), 1 manual replay of a captured plan, 1 that
  failed at TLS before the trust-anchor fix, 1 that reached the provider and
  failed with `model_not_found` (`gpt-5.1-codex-mini` unsupported on a
  ChatGPT account), 1 successful turn (`gpt-6-luna`), 1 interruption run.
  Token usage of the successful turn: 153,943 input (125,184 cached) / 1,457
  output. Codex on a ChatGPT plan exposes no dollar figure → **USD UNKNOWN**;
  the model was the account's documented "fast and affordable" catalogue
  entry, one trivial prompt per session, no additional prompt was sent.
- Claude: 4 sessions — one denied at the MCP permission layer, one with MCP
  working but Write denied, one complete turn with `result.md`, one
  interruption. API-reported `total_cost_usd`: **0.0410 + 0.0331 + 0.0280 =
  USD 0.1021** for the three reporting runs; the interrupt run reported none.
  All under `--max-budget-usd 1.00`.
- Additional local, non-provider commands: `codex --version`, `exec --help`,
  `exec resume --help`, `mcp --help/list/add --help`, `debug models`,
  `login status`, `claude --version/--help`, bwrap probes and the offline
  negative-space matrix.
- CONNECT-level counts (the only provider-request telemetry AutoWork has; the
  exact HTTP request count behind them is not observable): Codex **220 allowed
  CONNECTs over the 9 proxied sessions** (0 in the 18 ms run); Claude **12
  allowed CONNECTs over 4 sessions**. Denied: 7 `host_not_allowed`
  (Codex telemetry/upload hosts) and 3 late `session_inactive` teardown
  refusals.

No credential value, token, signed URL or provider secret appears in this
document, the commits, the logs kept for this spike, or any harness report.
