"""Claude Code 2.1.284 operator profile for the M24-05 host session runner (H5).

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md`` §3
(OS profile, env, skills), §5 (processes, budgets), §7 (MCP shim registration)
and §8 (provider differences). Every flag used here is printed by the real
``claude --help`` 2.1.284 (recorded verbatim in
``backend/tests/workspace_agent/fixtures/claude_help_2_1_284.txt``); the help
text is parsed at probe time and a missing flag fails the probe closed.

Capabilities the help text cannot prove are explicit ``proven=False`` entries
in the typed probe (``ClaudeCapabilityProbe.capabilities``) with the
conservative branch this adapter takes, plus the ``CLAUDE_HOST_SPIKE_CHECKS``
H0 list. The adapter never assumes them:

* live ``--input-format stream-json`` instructions -> not used; the prompt is
  the verified positional argument and instructions stay queued;
* resume via ``--session-id``/``--resume`` -> only with the explicit
  ``resume_proven=True`` host-spike gate, otherwise ``resume_unavailable``;
* ``CLAUDE_CONFIG_DIR`` -> not set; the private root is ``HOME`` under the
  session config directory, and ``--restricted`` ignores user/project/local
  settings (both printed by ``--help``);
* MCP config schema / shim tool exposure -> generated and strict, but a
  failure leaves no tools (fail closed, no fallback MCP server);
* ``--bare`` auth and even ``--restricted`` file-tool behaviour -> host-spike
  items; they cannot be exercised offline.

Registration hook (one line, for the orchestrator; H4 owns ``adapters/__init__``
on its branch). Inside ``sessions/supervisor.default_profiles()``, add a
*local* import to avoid an import cycle, then the profile::

    from .adapters.claude import ClaudeProfile  # local import, no cycle
    return ProfileRegistry((FakeProfile(), ClaudeProfile()))

The provider-neutral authority-limits preamble comes from
``adapters/preamble.py`` shared with the Codex profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .. import logs, process
from ..state import (
    CapabilityUnavailableError,
    ConfinementUnavailableError,
    ResumeUnavailableError,
    SessionError,
    atomic_write_json,
)
from ..supervisor import CapabilityProbe, LaunchContext, ProbeFlags
from .preamble import SYSTEM_PREAMBLE

ADAPTER_KIND = "claude"
DEFAULT_MAX_BUDGET_USD = "1.00"
MAX_PROMPT_ARGV_BYTES = 96 * 1024
MAX_PROBE_OUTPUT_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 5.0
MAX_STREAM_LINE_BYTES = 1024 * 1024
MAX_FINAL_MESSAGE_BYTES = 256 * 1024
MAX_TOOL_TRACKING = 1024
MAX_TOOL_NAME_BYTES = 128

MCP_SERVER_NAME = "autowork"
MCP_CONFIG_FILE_NAME = "claude-mcp.json"
MCP_SHIM_FILE_NAME = "shim.py"
MCP_SHIM_SOURCE = Path(__file__).resolve().parent.parent / "shim.py"
MCP_PYTHON_CANDIDATES: tuple[str, ...] = (
    "/usr/bin/python3.12",
    "/usr/bin/python3",
    "/usr/local/bin/python3",
)
BACKEND_URL = "http://127.0.0.1:8000"

DISALLOWED_TOOLS: tuple[str, ...] = ("Bash", "PowerShell", "REPL", "WebFetch")
PERMISSION_MODE = "dontAsk"
PERMISSION_PROMPTS = "none"

REQUIRED_HELP_FLAGS: tuple[str, ...] = (
    "--print",
    "--output-format",
    "--verbose",
    "--model",
    "--permission-mode",
    "--permission-prompts",
    "--disallowedTools",
    "--restricted",
    "--bare",
    "--strict-mcp-config",
    "--mcp-config",
    "--max-budget-usd",
    "--no-session-persistence",
    "--append-system-prompt",
)
RESUME_HELP_FLAGS: tuple[str, ...] = ("--session-id", "--resume")

# Read-only system paths the CLI needs to reach its provider: DNS/NSS config
# and TLS trust anchors. No host credential lives here.
TRUST_ANCHORS: tuple[str, ...] = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
    "/etc/localtime",
)
SYSTEM_BIND_PREFIXES: tuple[str, ...] = ("/usr", "/lib", "/lib64", "/bin", "/sbin")

MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
BUDGET_PATTERN = re.compile(r"^(?:[1-9][0-9]{0,5})(?:\.[0-9]{1,6})?$")
_OPTION_LINE = re.compile(r"^ {2}(-.*?)(?: {2,}|$)")
_FLAG_TOKEN = re.compile(r"--[A-Za-z][A-Za-z0-9-]*")
_PROBE_HOME_PREFIX = "autowork-claude-probe-"

CLAUDE_HOST_SPIKE_CHECKS: tuple[str, ...] = (
    "Run `claude -p --output-format stream-json --verbose` against a local prompt "
    "with the chosen provider auth; record the exact event frames and replay them "
    "through ClaudeStreamParser.",
    "Prove `--input-format stream-json` live instruction semantics (envelope, "
    "mid-run acceptance, EOF); only then advertise streaming_stdin and let the "
    "supervisor report `delivered=live`.",
    "Prove `--session-id <uuid>` then `claude -r <uuid>` resumes the same "
    "conversation with persistence enabled; only then set resume_proven=True.",
    "Prove the generated `--mcp-config` JSON schema registers the shim and that "
    "`--strict-mcp-config` exposes exactly the signed tool manifest.",
    "Prove `--restricted` + `--permission-mode dontAsk` + `--permission-prompts "
    "none` still lets Read/Write/Edit produce notes/ and result.md inside the "
    "scratch cwd, and that no subagent or hook escapes the profile.",
    "Prove `--bare` auth: ANTHROPIC_API_KEY in the seeded environment or "
    "apiKeyHelper via --settings; keychain/OAuth files are never read. Never "
    "log the credential.",
    "Prove the §3 bwrap profile creates namespaces on the host (no netns) with "
    "the resolved CLI runtime prefix and TLS/DNS anchors bound read-only.",
    "Prove `--max-budget-usd` is enforced for the chosen auth mode.",
    "Prove the resolved CLI install prefix (and any node runtime for script "
    "installs) is complete inside bwrap.",
    "Prove prompt delivery above MAX_PROMPT_ARGV_BYTES (the single-argument "
    "limit) through the stream-json stdin envelope or another printed file "
    "mechanism before raising the cap.",
)


# ---------------------------------------------------------------------------
# Typed probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvenCapability:
    """One adapter capability with its evidence and conservative branch."""

    name: str
    proven: bool
    basis: str
    conservative_behaviour: str

    def to_wire(self) -> dict[str, object]:
        return {
            "name": self.name,
            "proven": self.proven,
            "basis": self.basis,
            "conservative_behaviour": self.conservative_behaviour,
        }


@dataclass(frozen=True)
class ConfinementProbe:
    """Whether the reasoning-session bwrap profile can be launched."""

    ok: bool
    reason: str
    mocked: bool = False

    def to_wire(self) -> dict[str, object]:
        return {
            "profile": "bwrap-claude",
            "bwrap_ok": self.ok,
            "netns": False,
            "reason": self.reason,
            "mocked": self.mocked,
        }


@dataclass(frozen=True)
class ClaudeCapabilityProbe(CapabilityProbe):
    """The H1 typed probe plus the explicit proven/unproven capability table."""

    capabilities: tuple[ProvenCapability, ...] = ()
    host_spike: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, object]:
        wire = super().to_wire()
        wire["capabilities"] = [entry.to_wire() for entry in self.capabilities]
        wire["host_spike"] = list(self.host_spike)
        return wire


def parse_help_flags(help_text: str) -> frozenset[str]:
    """The long options a real ``--help`` text defines (prose mentions excluded)."""
    flags: set[str] = set()
    for line in help_text.splitlines():
        match = _OPTION_LINE.match(line)
        if match is None:
            continue
        flags.update(_FLAG_TOKEN.findall(match.group(1)))
    return frozenset(flags)


def _probe_digest(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _capabilities(*, resume_proven: bool) -> tuple[ProvenCapability, ...]:
    return (
        ProvenCapability(
            name="print_mode",
            proven=True,
            basis="claude --help 2.1.284: -p, --print",
            conservative_behaviour="non-interactive runs use --print only",
        ),
        ProvenCapability(
            name="stream_json_output",
            proven=True,
            basis="claude --help 2.1.284: --output-format text|json|stream-json",
            conservative_behaviour=(
                "frames are parsed by ClaudeStreamParser; a malformed or oversized "
                "line is dropped and never fabricated"
            ),
        ),
        ProvenCapability(
            name="max_budget_flag",
            proven=True,
            basis="claude --help 2.1.284: --max-budget-usd <amount> (--print only)",
            conservative_behaviour=(
                "a positive decimal default plus the supervisor wall/output/"
                "instruction budgets; the effect per auth mode is host-spike"
            ),
        ),
        ProvenCapability(
            name="restricted_refuses_bypass",
            proven=True,
            basis=(
                "claude --help: --restricted refuses bypassPermissions and removes "
                "code-running tools unless --tools names them"
            ),
            conservative_behaviour=(
                "bypassPermissions is never passed; --permission-mode dontAsk with "
                "--permission-prompts none denies anything that would prompt"
            ),
        ),
        ProvenCapability(
            name="stream_json_live_instructions",
            proven=False,
            basis="stdin stream-json envelopes are not printed by --help",
            conservative_behaviour=(
                "no --input-format; the prompt is the positional argument and "
                "instructions stay queued; the supervisor must not report live "
                "delivery while the probe flag streaming_stdin is false"
            ),
        ),
        ProvenCapability(
            name="resume_session_id_roundtrip",
            proven=resume_proven,
            basis=(
                "claude --help prints --session-id and --resume but not that a "
                "chosen id is resumable"
            ),
            conservative_behaviour=(
                "resume_proven=False by default: the probe reports resume=false, "
                "the supervisor returns 409 resume_unavailable and a new session "
                "plus handoff is started; never auto-relaunch"
            ),
        ),
        ProvenCapability(
            name="mcp_strict_shim_registration",
            proven=False,
            basis=(
                "claude --help prints --mcp-config/--strict-mcp-config only; the "
                "JSON schema and its tool exposure are host-spike facts"
            ),
            conservative_behaviour=(
                "one generated stdio server pointing only at the H2 shim with "
                "--strict-mcp-config; an unregistered shim leaves no tools, and "
                "no other MCP server is ever loaded"
            ),
        ),
        ProvenCapability(
            name="restricted_keeps_file_tools",
            proven=False,
            basis=(
                "claude --help says --restricted removes code-running tools and "
                "WebFetch and confines file tools to cwd, but not that notes/ and "
                "result.md remain writable under --permission-prompts none"
            ),
            conservative_behaviour=(
                "--permission-prompts none denies anything that would prompt; the "
                "run's final result event still parses and a missing capture is "
                "reported, never fabricated"
            ),
        ),
        ProvenCapability(
            name="private_config_dir_env",
            proven=False,
            basis="CLAUDE_CONFIG_DIR is not printed by claude --help",
            conservative_behaviour=(
                "no CLAUDE_CONFIG_DIR and no --setting-sources/--settings (their "
                "empty-list and schema semantics are unproven); the private root "
                "is HOME=<session>/config/home and --restricted ignores "
                "user/project/local settings"
            ),
        ),
        ProvenCapability(
            name="bare_provider_auth",
            proven=False,
            basis=(
                "claude --help: --bare accepts only ANTHROPIC_API_KEY or "
                "apiKeyHelper via --settings; the host credential set is unknown"
            ),
            conservative_behaviour=(
                "the adapter fabricates no credential env or config; an auth "
                "failure is a failed run (fail closed) that the host spike resolves"
            ),
        ),
        ProvenCapability(
            name="bwrap_namespaces",
            proven=False,
            basis="only the bwrap binary presence is checked here; namespace creation is host-only",
            conservative_behaviour=(
                "a missing binary raises confinement_unavailable before spawn and "
                "a bwrap namespace failure kills the run; there is no unconfined "
                "fallback"
            ),
        ),
        ProvenCapability(
            name="stream_json_verbose_requirement",
            proven=False,
            basis="claude --help does not say --output-format stream-json requires --verbose",
            conservative_behaviour=(
                "--verbose is passed anyway (it is a printed flag); the supervisor "
                "keeps the output ring bounded"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Stream-json parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventDraft:
    """One closed supervisor event derived from a CLI output frame."""

    kind: logs.EventKind
    payload: dict[str, object]


class ClaudeStreamParser:
    """Bounded, line-oriented ``--output-format stream-json`` frame parser.

    Only the closed supervisor event kinds are produced; tool prose and model
    text never enter an event payload (hashes and byte counts only). One event
    is produced per frame (at most one tool call per assistant or user frame);
    the raw frame stays in the supervisor's bounded log, so nothing is hidden.
    The final result text is bounded and exposed for the collect wiring.
    Unknown frames and malformed lines are counted and dropped, never
    fabricated.
    """

    def __init__(self) -> None:
        self.final_message: str | None = None
        self.outcome: str | None = None
        self.final_message_truncated = False
        self.dropped_lines = 0
        self.truncated_lines = 0
        self.tool_calls = 0
        self._tool_order: dict[str, None] = {}

    def feed(self, line: bytes | str) -> EventDraft | None:
        if isinstance(line, str):
            try:
                raw = line.encode("utf-8")
            except UnicodeEncodeError:
                self.dropped_lines += 1
                return None
        else:
            raw = line
        raw = raw.strip()
        if not raw:
            return None
        if len(raw) > MAX_STREAM_LINE_BYTES:
            self.truncated_lines += 1
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.dropped_lines += 1
            return None
        if not isinstance(value, dict):
            self.dropped_lines += 1
            return None
        kind = value.get("type")
        if kind == "assistant":
            return self._assistant(value)
        if kind == "user":
            return self._tool_result(value)
        if kind == "result":
            self._result(value)
        return None

    def _assistant(self, value: Mapping[str, object]) -> EventDraft | None:
        message = value.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if not isinstance(name, str) or not name:
                continue
            tool_id = block.get("id")
            tool_id_text = tool_id if isinstance(tool_id, str) else ""
            tool_input = block.get("input")
            encoded = (
                b""
                if tool_input is None
                else json.dumps(
                    tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
            )
            self.tool_calls += 1
            self._remember_tool(tool_id_text)
            return EventDraft(
                kind=logs.EventKind.TOOL_CALL_REQUESTED,
                payload={
                    "tool": name[:MAX_TOOL_NAME_BYTES],
                    "tool_use_id_sha256": hashlib.sha256(tool_id_text.encode("utf-8")).hexdigest(),
                    "input_sha256": hashlib.sha256(encoded).hexdigest(),
                    "input_bytes": len(encoded),
                },
            )
        return None

    def _tool_result(self, value: Mapping[str, object]) -> EventDraft | None:
        message = value.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return None
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            tool_id_text = tool_id if isinstance(tool_id, str) else ""
            raw_content = block.get("content")
            encoded = (
                b""
                if raw_content is None
                else json.dumps(raw_content, separators=(",", ":"), ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            return EventDraft(
                kind=logs.EventKind.TOOL_CALL_COMPLETED,
                payload={
                    "tool_use_id_sha256": hashlib.sha256(tool_id_text.encode("utf-8")).hexdigest(),
                    "is_error": block.get("is_error") is True,
                    "content_bytes": len(encoded),
                },
            )
        return None

    def _result(self, value: Mapping[str, object]) -> None:
        result = value.get("result")
        if isinstance(result, str):
            encoded = result.encode("utf-8")
            if len(encoded) > MAX_FINAL_MESSAGE_BYTES:
                self.final_message = encoded[:MAX_FINAL_MESSAGE_BYTES].decode(
                    "utf-8", errors="ignore"
                )
                self.final_message_truncated = True
            else:
                self.final_message = result
                self.final_message_truncated = False
        is_error = value.get("is_error") is True
        subtype = value.get("subtype")
        self.outcome = "failed" if is_error or subtype not in {None, "success"} else "success"

    def _remember_tool(self, tool_id: str) -> None:
        self._tool_order.pop(tool_id, None)
        self._tool_order[tool_id] = None
        while len(self._tool_order) > MAX_TOOL_TRACKING:
            oldest = next(iter(self._tool_order))
            del self._tool_order[oldest]


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class ClaudePromptUnavailableError(CapabilityUnavailableError):
    """The prepared brief cannot be delivered through the verified argv contract."""

    def __init__(self, prompt_bytes: int, limit: int) -> None:
        super().__init__(ADAPTER_KIND)
        self.details["reason"] = "prompt_too_large"
        self.details["prompt_bytes"] = prompt_bytes
        self.details["limit_bytes"] = limit


class _ProbeTooLarge(RuntimeError):
    """A probe command produced more output than the bound allows."""


class ClaudeProfile:
    """The closed ``claude`` launch profile: argv, env, MCP config, bwrap plan."""

    adapter_kind = ADAPTER_KIND
    requires_sandbox = True
    requires_manifest = True

    def __init__(
        self,
        binary: Path | str | None = None,
        *,
        bwrap_binary: Path | str | None = None,
        resume_proven: bool = False,
        max_budget_usd: str = DEFAULT_MAX_BUDGET_USD,
        mcp_python: Path | str | None = None,
        probe_timeout_seconds: float = PROBE_TIMEOUT_SECONDS,
        confinement_probe: Callable[[], ConfinementProbe] | None = None,
    ) -> None:
        self._binary = None if binary is None else Path(binary)
        self._bwrap_binary = None if bwrap_binary is None else Path(bwrap_binary)
        self._resume_proven = bool(resume_proven)
        self._max_budget_usd = _validate_budget(max_budget_usd)
        self._mcp_python = None if mcp_python is None else Path(mcp_python)
        self._probe_timeout_seconds = float(probe_timeout_seconds)
        self._confinement_probe = confinement_probe
        self._last_probe: ClaudeCapabilityProbe | None = None
        self._binary_digest_cache: dict[tuple[str, int, int, int], str] = {}

    # -- probe -------------------------------------------------------------

    def probe(self, *, now: str) -> ClaudeCapabilityProbe:
        binary = self._resolve_binary()
        version_bytes = self._run_probe_command(binary, "--version")
        help_bytes = self._run_probe_command(binary, "--help")
        version = _first_line(version_bytes)
        if not version:
            raise CapabilityUnavailableError(ADAPTER_KIND)
        help_text = help_bytes.decode("utf-8", errors="replace")
        flags = parse_help_flags(help_text)
        if not set(REQUIRED_HELP_FLAGS) <= flags:
            raise CapabilityUnavailableError(ADAPTER_KIND)
        resume_supported = set(RESUME_HELP_FLAGS) <= flags
        if self._resume_proven and not resume_supported:
            raise CapabilityUnavailableError(ADAPTER_KIND)
        confinement = self._probe_confinement()
        probe_flags = ProbeFlags(
            non_interactive=True,
            json_events="--output-format" in flags and "stream-json" in help_text,
            structured_output=False,
            resume=self._resume_proven and resume_supported,
            streaming_stdin=False,
            mcp_per_invocation={"--mcp-config", "--strict-mcp-config"} <= flags,
            os_sandbox=False,
        )
        capabilities = _capabilities(resume_proven=self._resume_proven)
        binary_sha256 = self._binary_digest(binary)
        help_sha256 = hashlib.sha256(help_bytes).hexdigest()
        payload: dict[str, object] = {
            "adapter_kind": ADAPTER_KIND,
            "version": version,
            "binary_sha256": binary_sha256,
            "help_sha256": help_sha256,
            "flags": probe_flags.to_wire(),
            "confinement": confinement.to_wire(),
            "probed_at": now,
            "capabilities": [entry.to_wire() for entry in capabilities],
            "host_spike": list(CLAUDE_HOST_SPIKE_CHECKS),
        }
        probe = ClaudeCapabilityProbe(
            adapter_kind=ADAPTER_KIND,
            version=version,
            binary_sha256=binary_sha256,
            help_sha256=help_sha256,
            flags=probe_flags,
            confinement=confinement.to_wire(),
            probed_at=now,
            probe_sha256=_probe_digest(payload),
            capabilities=capabilities,
            host_spike=CLAUDE_HOST_SPIKE_CHECKS,
        )
        self._last_probe = probe
        return probe

    def _resolve_binary(self) -> Path:
        candidate = self._binary
        if candidate is None:
            located = shutil.which("claude")
            if located is None:
                raise CapabilityUnavailableError(ADAPTER_KIND)
            candidate = Path(located)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CapabilityUnavailableError(ADAPTER_KIND) from exc
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise CapabilityUnavailableError(ADAPTER_KIND)
        return resolved

    def _probe_confinement(self) -> ConfinementProbe:
        if self._confinement_probe is not None:
            return self._confinement_probe()
        candidate = self._bwrap_path()
        if candidate.is_file():
            return ConfinementProbe(
                ok=True,
                reason=("bwrap binary present; namespace creation remains a host-spike (H0) check"),
            )
        return ConfinementProbe(ok=False, reason="bwrap binary missing")

    def _bwrap_path(self) -> Path:
        if self._bwrap_binary is not None:
            return self._bwrap_binary
        return Path(process.default_bwrap_binary())

    def _binary_digest(self, binary: Path) -> str:
        try:
            info = binary.stat()
        except OSError as exc:
            raise CapabilityUnavailableError(ADAPTER_KIND) from exc
        key = (str(binary), info.st_mode, info.st_size, info.st_mtime_ns)
        cached = self._binary_digest_cache.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        try:
            with binary.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise CapabilityUnavailableError(ADAPTER_KIND) from exc
        value = digest.hexdigest()
        self._binary_digest_cache.clear()
        self._binary_digest_cache[key] = value
        return value

    def _run_probe_command(self, binary: Path, argument: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix=_PROBE_HOME_PREFIX) as home:
            env = {
                "PATH": "/usr/bin:/bin",
                "HOME": home,
                "TMPDIR": home,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
            try:
                child = subprocess.Popen(
                    [str(binary), argument],
                    cwd=home,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                raise CapabilityUnavailableError(ADAPTER_KIND) from exc
            try:
                data = _read_bounded(child, timeout=self._probe_timeout_seconds)
                code = child.wait(timeout=self._probe_timeout_seconds)
            except (_ProbeTooLarge, subprocess.TimeoutExpired, OSError) as exc:
                raise CapabilityUnavailableError(ADAPTER_KIND) from exc
            finally:
                if child.poll() is None:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except OSError:
                        pass
                if child.stdout is not None:
                    child.stdout.close()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if code != 0:
                raise CapabilityUnavailableError(ADAPTER_KIND)
            return data

    # -- launch plan -------------------------------------------------------

    def build_launch_plan(self, context: LaunchContext) -> process.LaunchPlan:
        probe = self._last_probe if self._last_probe is not None else self.probe(now=_utc_now())
        if context.resume and not probe.flags.resume:
            raise ResumeUnavailableError(ADAPTER_KIND)
        if not probe.flags.mcp_per_invocation or not probe.flags.json_events:
            raise CapabilityUnavailableError(ADAPTER_KIND)
        confinement = self._probe_confinement()
        if not confinement.ok:
            raise ConfinementUnavailableError(ADAPTER_KIND)
        binary = self._resolve_binary()
        prompt = self._read_prompt(context)
        if len(prompt) > MAX_PROMPT_ARGV_BYTES:
            raise ClaudePromptUnavailableError(len(prompt), MAX_PROMPT_ARGV_BYTES)
        try:
            prompt_text = prompt.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CapabilityUnavailableError(ADAPTER_KIND) from exc
        mcp_config_path = self._prepare_session_config(context)
        argv = self._claude_argv(context, binary, prompt_text, mcp_config_path)
        bwrap_argv = process.build_bwrap_argv(
            tuple(argv),
            workspace_binds=self._readonly_binds(binary),
            ipc_directory=context.ipc_socket_path.parent,
            scratch_directory=context.scratch_directory,
            config_directory=context.config_directory,
            work_directory=context.work_directory,
            bwrap=self._bwrap_path(),
        )
        return process.LaunchPlan(
            argv=tuple(bwrap_argv),
            env=self._environment(context),
            cwd=context.work_directory,
            uses_bwrap=True,
        )

    def _read_prompt(self, context: LaunchContext) -> bytes:
        try:
            fd = os.open(context.prompt_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise CapabilityUnavailableError(ADAPTER_KIND) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise CapabilityUnavailableError(ADAPTER_KIND)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PROMPT_ARGV_BYTES:
                    raise ClaudePromptUnavailableError(total, MAX_PROMPT_ARGV_BYTES)
                chunks.append(chunk)
        finally:
            os.close(fd)
        data = b"".join(chunks)
        if not data:
            raise CapabilityUnavailableError(ADAPTER_KIND)
        return data

    def _claude_argv(
        self,
        context: LaunchContext,
        binary: Path,
        prompt_text: str,
        mcp_config_path: Path,
    ) -> list[str]:
        argv = [
            str(binary),
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            PERMISSION_MODE,
            "--permission-prompts",
            PERMISSION_PROMPTS,
            "--disallowedTools",
            ",".join(DISALLOWED_TOOLS),
            "--restricted",
            "--bare",
            "--mcp-config",
            str(mcp_config_path),
            "--strict-mcp-config",
            "--add-dir",
            str(context.scratch_directory),
            "--max-budget-usd",
            self._max_budget_usd,
            "--append-system-prompt",
            self._system_prompt(context),
        ]
        model = context.record.model
        if model is not None:
            if MODEL_PATTERN.fullmatch(model) is None:
                raise CapabilityUnavailableError(ADAPTER_KIND)
            argv.extend(("--model", model))
        if context.resume:
            argv.extend(("--resume", context.session_id))
        elif self._resume_proven:
            argv.extend(("--session-id", context.session_id))
        else:
            argv.append("--no-session-persistence")
        argv.append(prompt_text)
        return argv

    def _system_prompt(self, context: LaunchContext) -> str:
        # §5 captures notes/** and result.md/final.md from the scratch root,
        # which is one level above the CLI cwd; --add-dir makes it writable and
        # --restricted keeps file tools confined to cwd + --add-dir.
        scratch = context.scratch_directory
        return (
            SYSTEM_PREAMBLE
            + "\nOutput paths: write the final result to "
            + f"{scratch}/result.md (or {scratch}/final.md) and working notes "
            + f"under {scratch}/notes/. Those are the only task output "
            + "directories; do not write anywhere else.\n"
        )

    def _environment(self, context: LaunchContext) -> dict[str, str]:
        home = context.config_directory / "home"
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(context.config_directory / "cache"),
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "AUTOWORK_SESSION_ID": context.session_id,
            "AUTOWORK_SESSION_SOCKET": str(context.ipc_socket_path),
            "AUTOWORK_BACKEND_URL": BACKEND_URL,
        }

    def _readonly_binds(self, binary: Path) -> tuple[Path, ...]:
        candidates: list[Path] = []
        for path in (binary.parent, *(Path(item) for item in TRUST_ANCHORS)):
            if not path.exists():
                continue
            text = str(path)
            if any(
                text == prefix or text.startswith(prefix + "/") for prefix in SYSTEM_BIND_PREFIXES
            ):
                continue
            if any(text == str(seen) for seen in candidates):
                continue
            candidates.append(path)
        return tuple(candidates)

    # -- per-session config (MCP shim only) --------------------------------

    def _prepare_session_config(self, context: LaunchContext) -> Path:
        config = context.config_directory
        _ensure_private_directory(config)
        home = config / "home"
        _ensure_private_directory(home)
        _ensure_private_directory(home / ".config")
        _ensure_private_directory(config / "cache")
        shim_path = self._copy_shim(config)
        python = self._resolve_mcp_python()
        mcp_config_path = config / MCP_CONFIG_FILE_NAME
        payload = {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "type": "stdio",
                    "command": python,
                    "args": [str(shim_path)],
                    "env": {
                        "AUTOWORK_SESSION_ID": context.session_id,
                        "AUTOWORK_SESSION_SOCKET": str(context.ipc_socket_path),
                        "AUTOWORK_BACKEND_URL": BACKEND_URL,
                    },
                }
            }
        }
        try:
            atomic_write_json(mcp_config_path, payload)
        except OSError as exc:
            raise SessionError("the Claude MCP config cannot be written") from exc
        return mcp_config_path

    def _copy_shim(self, config: Path) -> Path:
        try:
            source = MCP_SHIM_SOURCE.read_bytes()
        except OSError as exc:
            raise CapabilityUnavailableError(ADAPTER_KIND) from exc
        target = config / MCP_SHIM_FILE_NAME
        try:
            fd = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o700,
            )
            try:
                os.fchmod(fd, 0o700)
                view = memoryview(source)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("the session shim write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise SessionError("the session shim cannot be installed") from exc
        return target

    def _resolve_mcp_python(self) -> str:
        if self._mcp_python is not None:
            if not self._mcp_python.is_file():
                raise CapabilityUnavailableError(ADAPTER_KIND)
            return str(self._mcp_python)
        for candidate in MCP_PYTHON_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        raise CapabilityUnavailableError(ADAPTER_KIND)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_budget(value: str) -> str:
    if BUDGET_PATTERN.fullmatch(value) is None or float(value) <= 0.0:
        raise ValueError("max_budget_usd must be a positive decimal string")
    return value


def _first_line(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    return text.splitlines()[0][:128]


def _read_bounded(child: subprocess.Popen[bytes], *, timeout: float) -> bytes:
    if child.stdout is None:
        raise _ProbeTooLarge("probe stdout is not captured")
    fd = child.stdout.fileno()
    deadline = time.monotonic() + max(0.01, timeout)
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _ProbeTooLarge("probe timed out")
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.2))
        if not ready:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_PROBE_OUTPUT_BYTES:
            raise _ProbeTooLarge("probe output exceeded its bound")
        chunks.append(chunk)
    return b"".join(chunks)


def _ensure_private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise SessionError("a session config directory cannot be created") from exc
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SessionError("a session config path is not a real directory")
    os.chmod(path, 0o700)
