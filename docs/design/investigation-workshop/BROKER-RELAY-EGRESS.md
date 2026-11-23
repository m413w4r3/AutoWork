# Broker relay and provider-only egress (M24.1-F1-D)

**Status:** design, normative for implementers. Base `1d52106f`, branch
`m24-1/f1d-relay-egress-design`. **Scope:** the host supervisor's per-session broker
relay, the in-sandbox provider forwarder, the host egress proxy and the network
attestation that gates `running`. No production code here. PostgreSQL/MinIO/artifacts
remain canonical; host processes, logs and model output are never evidence.
Normative neighbours: [SESSION-RUNNER.md](SESSION-RUNNER.md) §3/§3bis/§7,
[ADR-0011](../../adr/ADR-0011-investigation-workshop-coordination.md) D7 and
[WORKSPACE-AGENT.md](WORKSPACE-AGENT.md) §1/§3.4.
Legend: **VERIFIED** = read or executed at this HEAD; **INFERRED** = follows from code or
tool semantics but was not executed; **UNPROVEN** = host/CLI/provider fact for the H0 spike.

## 1. Verified current state (and corrections to the M24 report)

| Fact | Evidence | Verdict |
|---|---|---|
| The shim calls the backend directly at a loopback HTTP URL. | `sidecars/workspace_agent/sessions/shim.py:114-133` accepts only `http://127.0.0.1|::1:<port>`; `:173-214` opens `http.client.HTTPConnection`; the URL comes from `AUTOWORK_BACKEND_URL` (`:542-553`). | VERIFIED |
| Both real adapters inject `AUTOWORK_BACKEND_URL=http://127.0.0.1:8000` into the sandbox and into the MCP config. | `adapters/codex.py:66,523-535,614-631`; `adapters/claude.py:86,911-924,953-965`. | VERIFIED |
| `isolated_egress` runs the CLI under `bwrap --unshare-net` and attaches `slirp4netns --disable-host-loopback`. | `sessions/process.py:514-578` (`--unshare-net` at `:548-549`), `:182-250`, `:311-344`. | VERIFIED |
| That profile cannot reach host loopback, so BL1 is real: a real CLI shim cannot reach the broker. | Current attestation requires a refused loopback connection (`process.py:660-746`); shim reuse of `127.0.0.1:8000` inside that netns is impossible. | VERIFIED (by construction) |
| slirp NAT reaches the LAN and Internet without an allowlist; Docker bridge addresses are reachable through host routing. | `process.py:311-344`; slirp provides user-mode NAT. Docker reachability is not exercised. | LAN/Internet VERIFIED by semantics; Docker INFERRED |
| `host_network_unsafe` is a selectable production path, not just a dead flag. | Closed mode set `process.py:37-39`; startup accepts and warns `supervisor.py:875-883,981-990`; both sides must agree `:1182-1218`. | VERIFIED |
| The broker catalogue is closed and fixed (POST routes, strict schemas, no URL/header/proxy knob). | `sessions/catalog.py:39-41,101-125,685-691,705-736,846-876`; oracles `test_session_shim_contract.py:89-99,515-539`. | VERIFIED; reusable for the relay allowlist |
| The backend revalidates every signed call (HMAC, active session, task binding, current lease) and never trusts the shim. | `application/agent_session_verifier.py:164-198,351-445`; broker gate `application/agentic_tool_broker.py:586-617`; route `api/agentic_tools.py:229`. | VERIFIED; the relay must stay a transport |
| The D7 secret handoff is one-shot and is **never re-armed**. | `SecretBroker` `supervisor.py:667-796`; created only at prepare `:1266-1277`; `_launch` (`:1481-1586`) never recreates it. | VERIFIED — **new blocker BL3** |
| BL3 consequence: a second run of the same session (Codex `exec resume`) spawns a new shim that finds no secret socket. | Each CLI process spawns its own MCP servers; only one delivery exists. | INFERRED from code and CLI model |
| SESSION-RUNNER is internally inconsistent: §3 says “(no `--unshare-net`)” while §3bis and the code use it. | `SESSION-RUNNER.md:61` vs `:112-123`; `process.py:548-549`. | VERIFIED doc defect |
| Adapters declare no provider host list today; the only network facts are CLI proxy env strings. | `codex.py:66`, `claude.py:86`; no egress allowlist anywhere in `sessions/`. | VERIFIED |
| bwrap 0.9.0 gives a clean `lo`-only netns on this host. | Executed locally: `if_nameindex()==['lo']`, `lo` flags `0x9` (UP|LOOPBACK), empty `/proc/net/route`, only `::1` in `/proc/net/ipv6_route`; 127.0.0.1 bind/connect works; `192.0.2.1:443` and `1.1.1.1:443` fail with `ENETUNREACH` (no packet left the namespace). | VERIFIED locally |
| Codex 0.163.0 and Claude 2.1.284 contain proxy env names but print no proxy flag. | Codex binary strings: `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY` + lowercase + `NO_PROXY`; Claude binary strings: `HTTPS_PROXY/HTTP_PROXY/NO_PROXY/ALL_PROXY` + lowercase and explicit proxy diagnostics; neither `--help` prints a proxy flag. | Strings VERIFIED; behaviour UNPROVEN |
| M24 report BL1 (“relay via supervisor UDS required”) is accurate. | Same evidence as above. | VERIFIED |
| M24 report BL2 is directionally accurate but unproven for Docker container IPs; “never `host_network_unsafe`” is a decision, not an implemented control. | See rows 5–6. | Mixed |

Reusable pieces: the closed catalogue (`catalog.py`), the D7 signing (`shim.py:136-170`),
the backend verifier, the one-shot secret socket pattern (`SecretBroker`), the 0600
runtime dir and atomic state (`state.py`), the ro-bind UDS connect precedent
(`WORKSPACE-AGENT.md:84-99`), and the existing fake CLI/back-end harnesses.

## 2. Threat model (short)

Adversary: model output and untrusted source text steering the CLI; a compromised or
misconfigured CLI process; a same-uid process inside the sandbox (Codex has a shell); a
malformed shim request. Assets: AutoWork operational credentials, PostgreSQL/MinIO/Redis,
host LAN services, the broker's authority, evidence provenance, provider account.
In scope: broker access from the sandbox, provider-only egress, secret disclosure,
replay/ambiguity, revocation. **Not** in scope: a malicious host user, a malicious
supervisor binary, a malicious CLI binary, kernel/bwrap exploits, and the accepted
same-uid startup race on the one-time secret (`SESSION-RUNNER.md:334-336`).
No model instruction is a boundary; only kernel namespaces, UDS permissions, the fixed
catalogue, the HMAC gate and the egress proxy are.

## 3. Target architecture (and corrections to the proposal)

```
browser ─HTTP─> backend (host loopback:8000) <──HTTP── supervisor relay ── UDS broker.sock ── shim
                    ▲                                                   (host, outside sandbox)  │ MCP stdio
                    │                                                                           ▼
                    │                                                                     CLI process group
                    │                                                                     inside bwrap netns
host egress proxy ── UDS egress.sock ── netns forwarder 127.0.0.1:3128 ── HTTPS_PROXY ── CLI
   (CONNECT only)                         (byte pipe)                                      │
   resolves + validates + pins                                                              └─ TLS to provider
```

Decisions, with the proposal's gaps fixed:

1. **Broker relay (accepted, refined).** Per-session AF_UNIX listener in
   `<runtime>/sessions/<id>/ipc/broker.sock`, 0600, supervisor-owned, exposed only by the
   existing `--ro-bind ipc/` (connect works on a ro bind, `WORKSPACE-AGENT.md:84-99`).
   Sockets are **pathname** AF_UNIX, never abstract: an abstract socket lives in the
   network namespace and would not cross the sandbox boundary. The shim sends its signed
   HTTP request over UDS; the relay forwards byte-exact to a
   backend URL fixed in supervisor config. It never re-signs, never chooses a URL, never
   follows redirects and never becomes an authentication authority. **Refinement:** for
   the shared `tool-calls` path, method+path alone does not identify the operation; the
   relay also parses the bounded body and requires `tool_name` to be in the manifest the
   backend accepted for this session, and `command`/`task_id` to match the session record.
2. **Provider egress (accepted, refined).** slirp4netns is removed from the production
   path. The netns has only `lo` (verified); a trusted entrypoint starts a byte-pipe
   forwarder on `127.0.0.1:3128` that tunnels to a host AF_UNIX CONNECT proxy. The proxy
   resolves DNS, validates every address, pins the IP and tunnels TLS untouched.
   **Refinement:** the proxy refuses a CONNECT if *any* resolved address is forbidden
   (no “pick the good one”), refuses IP-literal CONNECT, and re-evaluates every new
   CONNECT (application redirects inside TLS re-enter the same policy).
3. **Secret re-arm (new).** The per-run one-shot secret socket is re-armed before every
   run from the supervisor's in-memory secret; it remains single-use *per run* and is
   never persisted. This closes BL3 without weakening the one-time property.
4. **Unsafe fallback removed.** `host_network_unsafe` is deleted from the shipped
   supervisor and from the backend literal; tests use fake profiles and injected probes.
   A real session with no attested isolation is `confinement_unavailable`, never degraded.
5. **Attestation strengthened.** The current probe only proves host-loopback refusal and
   a `tap0` interface (`process.py:660-746`). The new attestation proves, inside the
   netns: only `lo`, no IPv4/IPv6 default or non-loopback route, loopback usable, and
   non-loopback connects fail; it runs before the bwrap block is released.
6. **No DNS inside the sandbox.** Any client that resolves the provider before consulting
   `HTTPS_PROXY` cannot work; this is an H0 gate per adapter, never a reason to open a
   resolver or a route.

## 4. Broker relay protocol

Transport: HTTP/1.1 over `AF_UNIX`/`SOCK_STREAM`. One request per connection
(`Connection: close`), `Content-Length` required, no chunked bodies, no
`Expect: 100-continue`, no upgrade, no pipelining, no query string, max 8 concurrent
connections per session. Request-line limit 2 KiB; header block 16 KiB; body 1 MiB
(the catalogue's largest argument bound, `catalog.py:34-37,506-534`). Response body is
streamed to the shim with a 16 MiB ceiling; the shim applies the final tool-result cut
(`shim.py:189-212,458-497`).

Allowed operations (all POST; `sid` = the session id this listener belongs to):

| Path | Body check before forwarding | Timeout |
|---|---|---|
| `/api/investigations/{iid}/agent-sessions/{sid}/tool-calls` | `tool_name` ∈ accepted manifest; `schema_version` string; `arguments` object | 120 s command tools, 600 s registry, 1800 s `advisor.consult` (`catalog.py:77-79`) |
| `/api/investigations/{iid}/tasks/{tid}/commands` | `command` ∈ {`heartbeat`,`submit`}; `tid` = session task; `iid` = session investigation | 120 s |
| `/api/investigations/{iid}/handoffs` | `task_id` = session task; `iid` = session investigation | 120 s |

Header whitelist forwarded verbatim: `Content-Type`, `Accept`, `Accept-Encoding`,
`User-Agent`, `Content-Length`, `Idempotency-Key`, `X-AutoWork-Agent-Session`,
`X-AutoWork-Agent-Timestamp`, `X-AutoWork-Agent-Signature`. Any other header is refused
(400), not silently dropped. `X-AutoWork-Agent-Session` must equal `sid`; an
`Idempotency-Key` is mandatory (8–255 safe chars). The relay never logs headers, bodies,
paths or the backend URL beyond `method + route template + session id`.

Backend target: `AGENT_SESSIONS_BROKER_BACKEND_URL`, default `http://127.0.0.1:8000`,
validated at supervisor start as loopback-only HTTP with no credentials, path or query
(reuse `shim.parse_backend_url`); the CLI environment never contains it.

Relay error codes (typed JSON, `Content-Type: application/json`; no host path, no URL):

| Code | Status | Meaning |
|---|---|---|
| `relay_session_inactive` | 409 | local state is not `running`/`awaiting_instruction`, or revoked |
| `relay_operation_not_allowed` | 403 | method/path/header/body outside the closed catalogue |
| `relay_manifest_missing` | 409 | broker call before an accepted tool manifest |
| `relay_binding_mismatch` | 403 | path ids, session header or task do not match the session record |
| `relay_request_too_large` | 413 | request line/headers/body over the bound |
| `relay_backend_unreachable` | 503 | connect failed or request not fully written — **not sent** |
| `relay_backend_outcome_unknown` | 504 | full request written, no complete response — **ambiguous** |
| `relay_timeout` | 504 | per-operation deadline; `outcome_unknown` is true once the request was written |
| `relay_response_too_large` | 502 | response exceeded the 16 MiB ceiling |

Ambiguity and idempotence: the shim never retries automatically. `not_sent` is returned
only when no request byte reached the backend; `outcome_unknown` otherwise. The shim's
tool result carries `outcome_unknown`, `retryable: false` and
`idempotent_replay_allowed` (true only for tools whose backend idempotency key covers the
effect). A deliberate replay must reuse **identical arguments** (hence the same
deterministic `mcp.v1` key, `shim.py:160-170`); changing a `nonce` after ambiguity is
forbidden because it changes the key. `advisor.consult` ambiguity is surfaced as
`consultation_pending_reconciliation` and never replayed blindly (D9; existing oracle
`test_agentic_tool_broker.py:550`).

Revocation: the relay re-checks local session state on every connection and again before
forwarding; `interrupt_requested`, `stopped`, `lost`, `closed`, `failed` refuse. On those
transitions the listener aborts in-flight connections and is unlinked at close. The
backend remains the authority: it revalidates signature, active session, binding and
current lease on every call (`agent_session_verifier.py:351-445`;
`agentic_tool_broker.py:586-617`), so a relay bug cannot authorize an effect.

## 5. Provider-only egress policy

**Sandbox network profile.** `bwrap --unshare-net` only; no slirp; no interface except
`lo`; no route. The entrypoint (`sessions/entrypoint.py`, trusted 0700 code started by
bwrap, not by the model) raises `lo` if needed (locally it is already UP, flags `0x9`),
starts the forwarder with `PR_SET_PDEATHSIG`, then `exec`s the CLI. The forwarder listens
on `127.0.0.1:3128` (fixed per netns), parses nothing, resolves nothing, does TLS
nothing, and byte-pipes each TCP connection to `ipc/egress.sock`; max 8 concurrent
tunnels, 64 KiB buffers, close on either EOF.

**CLI environment (closed literal).** `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and their
lowercase forms = `http://127.0.0.1:3128`; `NO_PROXY=` empty; no other proxy variable.
Codex strings contain `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY`; Claude strings contain
all four plus explicit proxy diagnostics. Neither `--help` prints a proxy flag. Actual
honouring of the variables is **UNPROVEN** and is the first H0 gate; an adapter that
ignores them is refused, never run unconfined. Claude non-essential traffic must be
disabled (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_AUTOUPDATER=1`; strings
verified, effect UNPROVEN). No Codex equivalent is proven yet (open question 4).
If H0 finds an adapter that ignores the proxy variables and resolves/connects directly,
the safe options are (a) refuse that adapter, or (b) a separate SNI-redirect lot: a local
DNS stub answers only allowlisted names with `127.0.0.1`, and the forwarder parses the
TLS ClientHello SNI and issues a CONNECT under the same policy. TLS interception is never
an option, and option (b) does not relax default-deny.

**Host egress proxy.** Per-session AF_UNIX listener `ipc/egress.sock` (0600), no TCP
listener. CONNECT only; request line and headers bounded (8 KiB), 10 s to receive the
CONNECT, 600 s tunnel idle timeout, wall-clock capped by the run budget. It authenticates
nobody: the UDS permissions are the boundary, and the allowlist is per session, chosen by
the supervisor from `record.adapter_kind` — never from anything the sandbox sends. After a
validated destination is connected it answers `200 Connection Established` and pipes
bytes; there is no TLS interception and no certificate substitution.

Validation order, fail-closed at every step:
1. Parse authority as exactly `host:port`; reject userinfo, path, query, percent escapes,
   whitespace/control characters; port must be 443.
2. Normalize the name to lowercase ASCII/IDNA, valid labels, ≤253 chars; reject a trailing
   dot, wildcards and every IP-literal form (IPv4, bracketed IPv6, integer/octal/hex).
3. Require an exact match in the effective allowlist for this session's profile (no
   wildcard).
4. Resolve once with `getaddrinfo(host, 443, AF_UNSPEC)` under a 5 s timeout; require at
   least one address.
5. Validate **every** returned address: reject loopback, RFC1918/ULA, link-local
   (including `169.254.169.254`), CGNAT `100.64/10`, multicast, unspecified, reserved,
   documentation, benchmark, broadcast, 6to4/Teredo, IPv4-mapped IPv6, and every address
   assigned to a local host interface (read from `/proc/net/fib_trie` +
   `/proc/net/ipv6_route`; no new dependency).
6. Refuse the whole CONNECT if any address is forbidden; never fall back to a “good” one.
7. Connect to a validated address with a 10 s timeout and pin that IP; never re-resolve.
8. Pipe until EOF; application-level redirects happen inside TLS and therefore produce a
   new CONNECT, which re-enters step 1.
9. Log the decision (below).

Allowlist configuration: `AGENT_SESSIONS_EGRESS_PROFILE_FILE` (0600, no symlink, ≤64 KiB)
with schema `autowork-session-egress.v1`:
`{"schema":"autowork-session-egress.v1","profiles":{"codex":{"hosts":["api.openai.com"]},"claude":{"hosts":["api.anthropic.com"]}}}`.
Hosts are exact DNS names, port implicit 443. Each adapter declares a compiled maximum;
the effective set is `config ∩ adapter maximum` (configuration can narrow, never widen).
An empty/missing profile refuses real sessions (`confinement_unavailable`). Candidates
observed in binary strings (INFERRED, H0 confirms): Codex `api.openai.com`,
`auth.openai.com`, `chatgpt.com`; Claude `api.anthropic.com`. Telemetry/update hosts
(Claude `releases.claude.com`, `downloads.claude.ai`, Sentry ingest, `statsig`) are
excluded by default; add explicitly only if H0 proves the adapter cannot work without
them.

Logging: one JSONL line per CONNECT in `<session>/egress.log` (0600, 2 MiB ring):
`{at, session_id, adapter_kind, host, port, decision, reason, family}`. Never a URL, path,
query, header, token, body or resolved IP list; malformed authorities log
`invalid_authority` with a control-stripped 64-char prefix, never raw input. Provider
hostnames stay on the host; no backend route receives them.

No fallback: proxy unavailable, profile missing, attestation failed or bwrap/userns absent
⇒ `confinement_unavailable`; `host_network_unsafe` is deleted, not defaulted.

## 6. Lifecycle

| Event | Relay | Egress proxy | Secret |
|---|---|---|---|
| `prepare` | bind `broker.sock` (state READY, refuses calls) | bind `egress.sock`, load profile | arm one-shot `shim.sock` for run 1 |
| `tool-manifest` | store per-session manifest; broker calls require it | — | — |
| `start` | allow `running` only | allow CONNECT once running | re-arm if consumed; then spawn |
| run ends | close in-flight connections | close tunnels | consumed socket already gone |
| new run (Codex) | persists | persists | **re-arm** from in-memory secret |
| `interrupt_requested` | refuse + abort | refuse + close tunnels | close/wipe socket |
| `close`/revocation | unlink socket | unlink socket, close tunnels | wipe secret |
| supervisor stop/crash | unlink on next start | unlink on next start | wiped on stop; never persisted |
| backend restart | `relay_backend_unreachable`, no queue | unchanged | unchanged |

`start` order: re-arm secret socket → run the attestation inside a fresh netns with the
same `bwrap` profile → only on success release the bwrap block and spawn. Attestation
failure is `confinement_unavailable`, no spawn. The supervisor caches the attestation per
profile for its lifetime; a changed bwrap binary hash invalidates it. Restart
reconciliation is unchanged: every non-terminal session is killed and marked `lost`
(`supervisor.py:1102-1158`; oracle
`test_sessions_lifecycle.py:216`), never adopted. Close keeps `state.json`/logs until
retention but removes scratch, prompt, secret and both sockets
(`supervisor.py:2260-2326`). The in-memory secret is wiped on stop/close
(`supervisor.py:996-1024,2294-2299`); it is never written to state
(`state.py:388-466`). Lease expiry is backend-driven; the supervisor learns it through
the existing release path. Already-open provider tunnels are closed on that path; the
window until the host is told remains a residual risk (§8).

Attestation evidence (inside the netns, before `running`): `if_nameindex() == ["lo"]`;
`lo` flags UP|LOOPBACK; empty IPv4 route table and only `::1` IPv6 route; loopback
bind/connect succeeds; a host-loopback helper sees no accepted connection; connects to
RFC1918/TEST-NET addresses fail with `ENETUNREACH`. No packet to any non-loopback
destination is generated by the attestation. CI classification: unit tests use injected
attestations/resolvers and must never claim real isolation; host tests run only on hosts
with bwrap and are skipped/refused otherwise (`test_sessions_network.py:106-115` shows the
honest “refusal is acceptable” shape).

## 7. Implementation lots (exclusive files, order, oracles)

| Lot | Content | Exclusive files | Depends |
|---|---|---|---|
| **D0/H0** | Human-authorized host spike: CLI proxy honouring, exact provider hosts, MCP-over-relay, bwrap netns, credential set. Docs only. | `docs/operations/host-cli-spike-2026-11.md` | — |
| **RLY-1** | Relay core: listener, catalogue/manifest/body validation, fixed backend client, error codes, bounded I/O. No supervisor change. | `sessions/relay.py` (new); `tests/workspace_agent/test_sessions_relay.py` (new) | — |
| **RLY-2** | Shim UDS transport, relay error mapping, ambiguity envelope; keep the D7 signing and catalogue untouched. | `sessions/shim.py`; `tests/workspace_agent/test_session_shim_relay.py` (new) | RLY-1 interface |
| **EGR-1** | Host CONNECT proxy: parse, allowlist, resolver injection, address validation, pinning, tunnel lifecycle, bounded log. | `sessions/egress.py` (new); `tests/workspace_agent/test_sessions_egress.py` (new) | — |
| **NET-1** | Netns profile: remove slirp, entrypoint + forwarder, verified attestation; wrap the CLI argv with the entrypoint (adapters keep building argv); keep killpg/fail-closed semantics. | `sessions/process.py`; `sessions/entrypoint.py` (new); `tests/workspace_agent/test_sessions_network.py`; `test_sessions_forwarder.py` (new) | EGR-1, RLY-1 |
| **ADP-1** | Adapter env/profile: broker socket + proxy env, provider-host maximum, non-essential traffic off; no `AUTOWORK_BACKEND_URL`. | `adapters/codex.py`; `adapters/claude.py`; `tests/workspace_agent/test_codex_profile.py`; `test_claude_profile.py` | NET-1, RLY-2 |
| **LIFE-1** | Supervisor wiring: create/close relay+proxy, per-run secret re-arm, state gating, revocation, restart cleanup. | `sessions/supervisor.py`; `sessions/state.py` if fields are needed; `tests/workspace_agent/test_sessions_lifecycle.py`; `test_sessions_secret.py`; `test_sessions_relay_lifecycle.py` (new) | RLY-1, EGR-1, NET-1, ADP-1 |
| **CFG-1** | Backend: delete `host_network_unsafe`, require `isolated_egress`, document new host knobs. | `backend/src/cti_app/config.py`; `.env.example`; `tests/test_agent_sessions.py` (mode cases only) | — |
| **E2E-1** | Simulated end-to-end: fake CLI → shim → relay → fake backend; fake resolver → egress. | `tests/workspace_agent/test_sessions_relay_e2e.py` (new) | LIFE-1 |
| **REAL-1** | Host-attested bwrap tests, no providers, marked host-only. | `tests/workspace_agent/test_sessions_network_host.py` (new) | NET-1, LIFE-1 |
| **DOC-1** | Normative doc updates: SESSION-RUNNER §3/§3bis/§7, WORKSPACE-AGENT invariant exception, ADR D7 addendum. | `docs/design/investigation-workshop/SESSION-RUNNER.md`; `WORKSPACE-AGENT.md`; `docs/adr/ADR-0011-*` | after review |

No two lots share a file; `supervisor.py`, `process.py`, `shim.py`, each adapter, config
and docs have one owner. Order: RLY-1/EGR-1 in parallel; RLY-2 then NET-1; ADP-1;
LIFE-1; E2E-1/REAL-1; DOC-1 after the code review. Existing oracles that must stay green:
`test_agentic_tool_broker.py:329,455,487,550` (lease, replay, fence, ambiguity),
`test_sessions_lifecycle.py:84,216,513` (idempotent start, restart→lost, close cleanup),
`test_sessions_secret.py:126` (single-use handoff; now per run), and
`test_session_shim_contract.py:89,515` (closed catalogue).

Oracle map (S = simulated with injected transport/resolver/connector; R = real bwrap
attestation; P = live provider, requires separate authorization — not run here):

| ID | Oracle | Level |
|---|---|---|
| NET-01 | `test_session_shim_relay::test_uds_relay_forwards_a_signed_broker_call`; `test_sessions_relay_e2e::test_fake_cli_reaches_fake_backend` | S |
| NET-01 | `test_sessions_network_host::test_real_netns_shim_reaches_relay_without_host_loopback` | R |
| NET-02 | `test_sessions_relay::test_relay_refuses_method_path_header_and_unlisted_tool` | S |
| NET-03 | `test_session_shim_contract::test_catalogue_has_no_url_shell_path_or_proxy_knob`; `test_sessions_relay::test_relay_ignores_agent_headers_and_uses_fixed_backend` | S |
| NET-04 | `test_sessions_relay::test_relay_refuses_inactive_or_revoked_session`; existing `test_agentic_tool_broker::test_broker_refuses_missing_or_stale_lease` | S |
| NET-05 | `test_sessions_relay::test_relay_does_not_re_sign_and_backend_rejects_bad_hmac`; existing verifier tests | S |
| NET-06 | existing `test_broker_replays_same_client_key_without_second_dispatch`; `test_sessions_relay::test_replay_forwards_identical_bytes_once` | S |
| NET-07 | `test_sessions_relay::test_backend_drop_after_request_returns_outcome_unknown`; `test_session_shim_bounds::test_transport_failure_is_a_tool_error_without_details` (updated to distinguish not-sent/unknown) | S |
| NET-08 | existing `test_sessions_secret::test_secret_never_in_state_logs_argv_env_or_responses`; `test_sessions_relay::test_relay_log_has_no_header_body_or_url` | S |
| NET-09 | existing `test_sessions_lifecycle::test_restart_marks_active_lost_and_never_relaunches`, `::test_close_deletes_secret_socket_scratch_and_keeps_state`; add socket unlink assertions | S |
| NET-10 | `test_codex_profile::test_codex_uses_relay_socket_without_backend_url`; `test_claude_profile::test_claude_uses_relay_socket_without_backend_url`; `test_agentic_host_sessions::test_port_maps_one_to_one_to_session_wire` | S |
| EG-01 | `test_sessions_egress::test_allowlisted_connect_tunnels_to_fake_upstream` | S |
| EG-01 | `test_sessions_network_host::test_real_netns_provider_path_attested` (no provider; a live provider needs separate authorization) | R |
| EG-02 | updated `test_sessions_network.py::test_real_host_attestation_blocks_host_loopback_or_refuses` | R |
| EG-03 | `test_sessions_egress::test_private_and_lan_resolutions_are_refused` | S |
| EG-04 | `test_sessions_egress::test_docker_ranges_and_local_addresses_are_refused`; netns route assertions | S/R |
| EG-05 | `test_sessions_egress::test_non_allowlisted_host_and_ip_literal_are_refused` | S |
| EG-06 | `test_sessions_egress::test_ipv6_ula_linklocal_and_v4_mapped_are_refused`; `::test_every_resolved_address_must_validate` | S |
| EG-07 | `test_sessions_forwarder::test_agent_env_cannot_bypass_netns_route`; `test_sessions_network_host::test_direct_connect_fails_without_route` | S/R |
| EG-08 | `test_sessions_network::test_spawn_fails_closed_when_egress_is_unavailable`; config test refusing `host_network_unsafe` | S |
| EG-09 | `test_sessions_relay_e2e::test_offline_capture_still_completes_without_egress` | S |
| EG-10 | `test_sessions_egress::test_revocation_refuses_new_connects_and_closes_tunnels`; `test_sessions_relay_lifecycle::test_revocation_unlinks_both_sockets` | S/R |

## 8. Residual risks and open questions

1. **Exact provider hosts** per adapter and auth mode are unknown; the candidates above
   are inferred from binary strings, not docs. H0 must produce the exact minimal list.
2. **Live CLI/provider testing is not authorized here.** Any H0 model turn, OAuth refresh
   or provider call needs a separate explicit human authorization; no test may call a
   live provider.
3. **CLI proxy compliance is UNPROVEN.** If Codex or Claude resolves the host itself or
   ignores `HTTPS_PROXY`, that adapter must be refused until fixed. There is no
   `host_network_unsafe` fallback.
4. **Codex telemetry/update controls** are not proven; Claude's non-essential switches are
   present in strings but unproven in behaviour. Required hosts must be explicit or the
   adapter refused.
5. **Claude's own proxy/agent-proxy handling** may rewrite `HTTPS_PROXY` for subprocesses
   (strings mention an agent-proxy relay and `HTTPS_PROXY` diagnostics); H0 must prove the
   outer proxy is the effective one.
6. **D7 secret readable by the sandboxed CLI — accepted and bounded, never resolved.**
   The CLI launches the shim itself and can read `AUTOWORK_SESSION_SOCKET` from its own
   environment (`adapters/codex.py:529-541`, `adapters/claude.py:916-929`), so the order
   is under its control: it can connect to the per-run one-shot socket before the shim
   and receive the complete `autowork-session-secret.v1` frame (R2-F1, VERIFIED). This
   is not a narrow startup race and `PR_SET_DUMPABLE=0` / the ro `ipc/` bind only reduce
   the blast radius. What the secret grants is exactly the session's own authority: the
   relay exposes a closed catalogue of three fixed routes with closed headers, and the
   backend revalidates HMAC D7, the active session, the task binding and the current
   lease; the secret is destroyed at close and revocation and re-armed per run. The
   compensating control is that the secret must never be persisted: `collect` refuses
   agent content carrying it (raw, hex, base64, or a ≥16-byte aligned fragment) and
   revokes the session with `secret_exposure_detected`.
7. **Revocation latency for open tunnels**: the backend is authoritative, but a lease
   expiry the host has not yet been told about does not instantly close an active tunnel.
   A local lease deadline (passed in `PrepareSessionV1`) would tighten this; today the
   state carries only `lease_fence` (`state.py:389-421`).
8. **bwrap/user-namespace availability and `lo` behaviour** vary by kernel/host; the
   design is verified on bwrap 0.9.0 here, but every deployment must pass the real
   attestation or refuse sessions. CI proves refusal, never isolation.
9. **Removing `host_network_unsafe`** changes tests and deployment knobs
   (`backend/tests/test_investigation_transfers.py:1535`,
   `test_codex_profile.py:308,510,585,617`); they must move to injected probes.
10. **No cgroup quota** is added: memory/CPU remain `RLIMIT_AS`/wall-clock budgets
    (`SESSION-RUNNER.md:95-106`), unchanged by this design.
11. **Log retention**: `egress.log` contains provider hostnames; confirm host-log
    retention and whether that metadata is acceptable outside PostgreSQL.
12. **Redirects and CDN answers**: a provider redirect to a new hostname requires that
    hostname in the allowlist, otherwise the model call fails; H0 must record the full
    redirect set actually used.
13. **The D7 secret is readable by the sandboxed CLI (item 6); this residual risk is
    accepted and bounded, not fixed.** The design does not hide the secret from a CLI
    that spawns the shim; it bounds what the secret can do, destroys it at
    close/revocation, re-arms it per run, and refuses to persist agent content that
    carries it. Any reviewer must keep treating this residue as open unless the shim is
    moved host-side.
