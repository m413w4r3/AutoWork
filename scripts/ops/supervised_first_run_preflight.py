"""Read-only preflight for a supervised CTI investigation pilot.

The command never calls a provider, starts a service, migrates PostgreSQL, or
reads sample bytes. Local service checks are TCP connects to loopback only.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from cti_app.application.identity import PROTECTED_APP_ENVIRONMENTS

GATE_STATUSES = {
    "PASS",
    "FAIL",
    "BLOCKED_APPROVAL",
    "NOT_VALIDATED",
    "NOT_APPLICABLE",
}
TLP_LABELS = {
    "TLP:CLEAR",
    "TLP:WHITE",
    "TLP:GREEN",
    "TLP:AMBER",
    "TLP:AMBER+STRICT",
    "TLP:RED",
}

_BINARY_PROBES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("uv", ("uv",), ("--version",)),
    ("psql", ("psql",), ("--version",)),
    ("pg_isready", ("pg_isready",), ("--version",)),
    ("typst", ("typst",), ("--version",)),
    ("pdftotext", ("pdftotext",), ("-v",)),
    ("pdftoppm", ("pdftoppm",), ("-v",)),
    ("d2", ("d2",), ("--version",)),
    ("file", ("file",), ("--version",)),
    ("strings", ("strings",), ("--version",)),
    ("readelf", ("readelf",), ("--version",)),
    ("objdump", ("objdump",), ("--version",)),
    ("yara", ("yara",), ("--version",)),
    ("tshark", ("tshark",), ("--version",)),
    ("capinfos", ("capinfos",), ("--version",)),
    ("floss", ("floss",), ("--version",)),
    ("mwcp", ("mwcp",), ("--version",)),
    ("bwrap", ("bwrap",), ("--version",)),
)


def normalize_gate_status(status: str, *, evidence_mode: str = "live") -> str:
    """Apply hard readiness semantics before a gate status can be serialized."""
    normalized = status.upper()
    if normalized not in GATE_STATUSES:
        return "NOT_VALIDATED"
    if normalized == "PASS" and evidence_mode in {"offline_simulated", "ci"}:
        return "NOT_VALIDATED"
    return normalized


def calculate_conclusion(
    signals: Mapping[str, bool | None],
    *,
    reduced_scope: str | None = None,
    missing_dependencies: Sequence[str] = (),
    ci_green: bool = False,
) -> str:
    """Calculate GO conservatively; CI and simulated evidence never confer readiness."""
    del ci_green  # A green CI run is implementation evidence, never a live GO signal.
    required = {
        "python_312": True,
        "conda_active": False,
        "critical_path_only_mocked": False,
        "api_worker_match": True,
        "bridge_accessible": True,
        "virustotal_proxy_present": True,
        "quarantine_ok": True,
        "tlp_known": True,
        "operator_identity_verified": True,
        "database_safe": True,
        "publication_threatens_old_release": False,
    }
    if any(signals.get(name) is not expected for name, expected in required.items()):
        return "NO_GO"
    missing = tuple(item.strip() for item in missing_dependencies if item.strip())
    if missing:
        if reduced_scope and reduced_scope.strip():
            return "GO_PARTIEL"
        return "NO_GO"
    return "GO"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_snapshot() -> dict[str, Any]:
    def run(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip()

    sha = run(["rev-parse", "HEAD"])
    status = run(["status", "--porcelain=v1"])
    lines = [line for line in (status or "").splitlines() if line]
    return {
        "sha": sha,
        "branch": run(["branch", "--show-current"]),
        "tree_dirty": bool(lines) if status is not None else None,
        "changed_path_count": len(lines) if status is not None else None,
        "status_available": status is not None,
    }


def _version_line(text: str) -> str | None:
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            candidate = re.sub(
                r"(?i)(api[_ -]?key|token|secret|password)=\S+",
                r"\1=[REDACTED]",
                candidate,
            )
            return candidate[:160]
    return None


def _binary_profile() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {
        "python": {
            "present": True,
            "version": platform.python_version(),
            "source": "uv runtime",
        }
    }
    minimal_env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C",
        "LC_ALL": "C",
    }
    for name, candidates, version_args in _BINARY_PROBES:
        executable = next(
            (
                resolved
                for candidate in candidates
                if (resolved := shutil.which(candidate))
            ),
            None,
        )
        if executable is None:
            result[name] = {"present": False, "version": None}
            continue
        try:
            completed = subprocess.run(
                [executable, *version_args],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
                env=minimal_env,
            )
        except (OSError, subprocess.SubprocessError):
            result[name] = {"present": True, "version": "unavailable"}
            continue
        version = (
            _version_line(completed.stdout + "\n" + completed.stderr)
            if completed.returncode == 0
            else None
        )
        result[name] = {
            "present": True,
            "version": version or f"unavailable (exit {completed.returncode})",
        }
    return result


def _local_host(host: str | None) -> bool:
    if host is None:
        return True  # PostgreSQL's default Unix socket is local to this process.
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _safe_url_target(
    value: str | None, *, default_scheme: str | None = None
) -> tuple[str | None, int | None]:
    if not value:
        return None, None
    raw = value.strip()
    if default_scheme and "://" not in raw:
        raw = f"{default_scheme}://{raw}"
    try:
        parsed = urlsplit(raw)
        return parsed.hostname, parsed.port
    except ValueError:
        return None, None


def _probe_local_tcp(
    name: str, value: str | None, *, default_scheme: str | None = None
) -> dict[str, Any]:
    if not value:
        return {"state": "ABSENT", "target": None, "reason": "not_configured"}
    host, port = _safe_url_target(value, default_scheme=default_scheme)
    if host is None or port is None:
        return {"state": "UNKNOWN", "target": None, "reason": "invalid_endpoint"}
    if not _local_host(host):
        return {
            "state": "NOT_PROBED",
            "target": "non_local_target_omitted",
            "reason": "local_only_policy",
        }
    target_host = "localhost" if host.casefold() == "localhost" else host
    target = f"{target_host}:{port}"
    try:
        with socket.create_connection((target_host, port), timeout=0.4):
            reachable = True
    except OSError:
        reachable = False
    return {
        "state": "REACHABLE" if reachable else "UNREACHABLE",
        "target": target,
        "reason": "tcp_connect_only",
    }


def _database_identity(dsn: str | None) -> dict[str, Any]:
    if not dsn:
        return {
            "state": "REFUSED",
            "safe": False,
            "host": None,
            "database": None,
            "schema": "unknown",
            "reason": "postgres_dsn_missing",
            "read_only": True,
            "migrated": False,
        }
    raw = dsn.strip()
    if raw.startswith("postgresql+"):
        raw = "postgresql:" + raw.split(":", 1)[1]
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"postgresql", "postgres"}:
            raise ValueError("unsupported database scheme")
        host = parsed.hostname
        database = unquote(parsed.path.lstrip("/")) or None
        port = parsed.port or 5432
    except ValueError:
        return {
            "state": "REFUSED",
            "safe": False,
            "host": None,
            "database": None,
            "schema": "unknown",
            "reason": "postgres_dsn_invalid",
            "read_only": True,
            "migrated": False,
        }
    host_display = (
        host
        if _local_host(host) and host
        else "local_socket"
        if host is None
        else "non_local_omitted"
    )
    database_display = (
        database
        if database and not re.search(r"(?i)(prod|live)", database)
        else "sensitive_or_live_name_omitted"
    )
    app_env = os.environ.get("APP_ENV", "development").casefold()
    disposable_marker = os.environ.get(
        "SUPERVISED_FIRST_RUN_DB_DISPOSABLE", ""
    ).casefold() in {
        "1",
        "true",
        "yes",
    }
    pilot_name = bool(
        database and re.search(r"(?i)(pilot|first[-_]run|disposable)", database)
    )
    live_name = bool(
        database and re.search(r"(?i)(^|[_-])(prod|production|live)([_-]|$)", database)
    )
    environment_is_live = app_env in PROTECTED_APP_ENVIRONMENTS
    if not _local_host(host):
        reason = "database_host_not_loopback"
    elif live_name or environment_is_live:
        reason = "live_database_refused"
    elif not disposable_marker or not pilot_name:
        reason = "explicit_disposable_pilot_identity_required"
    else:
        return _inspect_safe_local_database(
            raw, host, port, database, host_display, database_display
        )
    return {
        "state": "REFUSED",
        "safe": False,
        "host": host_display,
        "port": port if _local_host(host) else None,
        "database": database_display,
        "schema": "not_queried",
        "reason": reason,
        "read_only": True,
        "migrated": False,
    }


def _inspect_safe_local_database(
    dsn: str,
    host: str | None,
    port: int,
    database: str | None,
    host_display: str,
    database_display: str | None,
) -> dict[str, Any]:
    try:
        import asyncpg
    except ImportError:
        return {
            "state": "REFUSED",
            "safe": False,
            "host": host_display,
            "port": port,
            "database": database_display,
            "schema": "unknown",
            "reason": "asyncpg_unavailable",
            "read_only": True,
            "migrated": False,
        }

    async def inspect() -> dict[str, Any]:
        parsed = urlsplit(dsn)
        connection = await asyncpg.connect(
            host=host,
            port=port,
            user=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
            database=database,
            timeout=2,
            server_settings={"default_transaction_read_only": "on"},
        )
        try:
            async with connection.transaction(readonly=True):
                current_database = await connection.fetchval(
                    "SELECT current_database()"
                )
                table_count = await connection.fetchval(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
                alembic_exists = await connection.fetchval(
                    "SELECT to_regclass('public.alembic_version') IS NOT NULL"
                )
                revisions: list[str] = []
                if alembic_exists:
                    revisions = [
                        str(row["version_num"])
                        for row in await connection.fetch(
                            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
                        )
                    ]
        finally:
            await connection.close()
        empty = int(table_count or 0) == 0
        identified_schema = bool(revisions)
        safe = bool(current_database == database and (empty or identified_schema))
        if current_database != database:
            reason = "connected_database_mismatch"
            schema = "mismatch"
        elif not empty and not identified_schema:
            reason = "non_empty_database_without_alembic_identity"
            schema = "unidentified_non_empty"
        else:
            reason = "explicit_disposable_pilot_read_only"
            schema = "empty" if empty else "alembic_version_present"
        return {
            "state": "SAFE_READ_ONLY" if safe else "REFUSED",
            "safe": safe,
            "host": host_display,
            "port": port,
            "database": database_display,
            "schema": schema,
            "alembic_revisions": revisions,
            "reason": reason,
            "read_only": True,
            "migrated": False,
        }

    try:
        return asyncio.run(inspect())
    except (
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
        ImportError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as exc:  # Never expose connection strings or driver diagnostics.
        return {
            "state": "REFUSED",
            "safe": False,
            "host": host_display,
            "port": port,
            "database": database_display,
            "schema": "unknown",
            "reason": f"read_only_connection_failed:{type(exc).__name__}",
            "read_only": True,
            "migrated": False,
        }


def _validated_reference(args: argparse.Namespace) -> dict[str, Any]:
    fields = {
        "subject_id": args.subject_id,
        "production_run_id": args.production_run_id,
        "synthesis_artifact_id": args.synthesis_artifact_id,
        "version": args.version,
        "sha256": args.sha256,
        "tlp": args.tlp,
    }
    present = [value is not None for value in fields.values()]
    if not any(present):
        return {
            "provided": False,
            "valid": False,
            "missing_fields": list(fields),
            "reason": "exact_reference_arguments_missing",
            "selection_by_title_or_index": False,
            "validation_scope": "syntactic_only; canonical_trace_match_required",
        }
    missing = [name for name, value in fields.items() if value is None]
    if missing:
        return {
            "provided": True,
            "valid": False,
            "missing_fields": missing,
            "reason": "exact_reference_incomplete",
            "selection_by_title_or_index": False,
            "validation_scope": "syntactic_only; canonical_trace_match_required",
        }
    errors: list[str] = []
    for name in ("subject_id", "production_run_id", "synthesis_artifact_id"):
        value = str(fields[name])
        try:
            if str(UUID(value)) != value.casefold():
                errors.append(f"{name}_must_be_canonical_uuid")
        except ValueError:
            errors.append(f"{name}_must_be_canonical_uuid")
    sha = str(fields["sha256"])
    if re.fullmatch(r"[0-9a-fA-F]{64}", sha) is None:
        errors.append("sha256_must_be_64_hex_characters")
    if isinstance(fields["version"], bool) or int(fields["version"]) < 1:
        errors.append("version_must_be_positive")
    tlp = str(fields["tlp"]).upper()
    if tlp not in TLP_LABELS:
        errors.append("tlp_label_unknown")
    return {
        "provided": True,
        "valid": not errors,
        "missing_fields": [],
        "subject_id": fields["subject_id"],
        "production_run_id": fields["production_run_id"],
        "synthesis_artifact_id": fields["synthesis_artifact_id"],
        "version": fields["version"],
        "sha256": sha,
        "tlp": tlp if tlp in TLP_LABELS else None,
        "sha256_format_valid": re.fullmatch(r"[0-9a-fA-F]{64}", sha) is not None,
        "reason": "exact_reference_validated_syntactically"
        if not errors
        else "invalid_exact_reference",
        "errors": errors,
        "selection_by_title_or_index": False,
        "validation_scope": "syntactic_only; canonical_trace_match_required",
    }


def _source_calls_shared_assembly(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "cti_app.infrastructure.investigation_runtime_ports"
        and any(
            alias.name == "build_investigation_runtime_composition_factory"
            for alias in node.names
        )
        for node in ast.walk(tree)
    )
    called = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_investigation_runtime_composition_factory"
        for node in ast.walk(tree)
    )
    return imported and called


def _safe_runtime_inventories() -> dict[str, Any]:
    """Build the shared composition with provider adapters omitted and no I/O ports."""
    sys.path.insert(0, str(BACKEND_SRC))
    settings: Any | None = None
    factory_error: str | None = None
    try:
        from cti_app.application.blobs import BlobCatalogService
        from cti_app.application.virustotal import VirusTotalCapabilities
        from cti_app.config import Settings
        from cti_app.infrastructure import local_command_runner
        from cti_app.infrastructure.investigation_runtime_ports import (
            build_investigation_runtime_composition_factory,
            describe_tool_inventory,
        )

        settings = Settings(_env_file=None)
        original_get_settings = local_command_runner.get_settings
        local_command_runner.get_settings = lambda: settings

        def uow_factory() -> None:
            return None

        blob_store = object()
        blob_catalog = BlobCatalogService(blob_store, uow_factory)
        inputs: dict[str, Any] = {
            "settings": settings,
            "uow_factory": uow_factory,
            "blob_store": blob_store,
            "blob_catalog": blob_catalog,
            "artifact_store": object(),
            "model_conversations": object(),
            "vt_adapter": None,
            "vt_capabilities": VirusTotalCapabilities(),
            "vt_observations": None,
            "shodan_adapter": None,
            "ghidra_adapter": None,
        }
        try:
            api_runtime = build_investigation_runtime_composition_factory(**inputs)
            worker_runtime = build_investigation_runtime_composition_factory(**inputs)
        finally:
            local_command_runner.get_settings = original_get_settings
        api_inventory = describe_tool_inventory(api_runtime)
        worker_inventory = describe_tool_inventory(worker_runtime)
        return {
            "mode": "offline_simulated",
            "provider_calls": 0,
            "api": api_inventory,
            "worker": worker_inventory,
            "constructed_inventory_matches": api_inventory == worker_inventory,
            "live_process_inventory_observed": False,
            "reason": "shared_composition_built_with_provider_adapters_omitted",
        }
    except Exception as exc:  # noqa: BLE001 - runtime version mismatch uses safe inventory fallback.
        factory_error = factory_error or type(exc).__name__
        try:
            from types import SimpleNamespace

            from cti_app.config import Settings
            from cti_app.infrastructure.investigation_runtime_ports import (
                RuntimePortState,
                RuntimePortStatus,
                describe_tool_inventory,
            )

            settings = settings or Settings(_env_file=None)
            local_enabled = bool(settings.local_analysis_enabled)
            statuses = (
                RuntimePortStatus(
                    "virustotal",
                    RuntimePortState.UNAVAILABLE,
                    "provider_adapter_omitted_by_preflight",
                ),
                RuntimePortStatus(
                    "shodan",
                    RuntimePortState.UNAVAILABLE,
                    "provider_adapter_omitted_by_preflight",
                ),
                RuntimePortStatus(
                    "ghidra",
                    RuntimePortState.UNAVAILABLE,
                    "ghidra_disabled"
                    if not settings.ghidra_enabled
                    else "adapter_not_probed",
                ),
                RuntimePortStatus(
                    "local_analysis",
                    RuntimePortState.UNAVAILABLE,
                    "local_analysis_disabled"
                    if not local_enabled
                    else "runtime_not_probed",
                ),
                RuntimePortStatus(
                    "local_command_execution",
                    RuntimePortState.UNAVAILABLE,
                    "local_analysis_disabled"
                    if not local_enabled
                    else "confinement_not_probed",
                ),
                RuntimePortStatus(
                    "rule_workbench",
                    RuntimePortState.UNAVAILABLE,
                    "rule_workbench_disabled"
                    if not settings.rule_workbench_enabled
                    else "yara_runtime_not_probed",
                ),
                RuntimePortStatus("sample_similarity", RuntimePortState.AVAILABLE),
                RuntimePortStatus(
                    "floss_extraction",
                    RuntimePortState.UNAVAILABLE,
                    "local_analysis_disabled"
                    if not local_enabled
                    else "binary_or_confinement_not_probed",
                ),
                RuntimePortStatus(
                    "configuration_extraction",
                    RuntimePortState.UNAVAILABLE,
                    "local_analysis_disabled"
                    if not local_enabled
                    else "parser_or_confinement_not_probed",
                ),
                RuntimePortStatus("behavior_report_import", RuntimePortState.AVAILABLE),
                RuntimePortStatus(
                    "retrohunt",
                    RuntimePortState.UNAVAILABLE,
                    "retrohunt_provider_not_configured",
                ),
                RuntimePortStatus(
                    "network_capture",
                    RuntimePortState.UNAVAILABLE,
                    "no_operator_capture_source",
                ),
                RuntimePortStatus(
                    "native_analysis",
                    RuntimePortState.UNAVAILABLE,
                    "native_analysis_disabled"
                    if not settings.native_analysis_enabled
                    else "confined_runner_not_probed",
                ),
            )
            ports = SimpleNamespace(
                statuses=statuses,
                optional_tool_ports=SimpleNamespace(
                    local_command_catalog=None, tshark=None
                ),
            )
            api_inventory = describe_tool_inventory(ports)
            worker_inventory = describe_tool_inventory(ports)
            return {
                "mode": "offline_simulated",
                "provider_calls": 0,
                "api": api_inventory,
                "worker": worker_inventory,
                "constructed_inventory_matches": api_inventory == worker_inventory,
                "live_process_inventory_observed": False,
                "reason": f"shared_status_contract_fallback:{factory_error}",
            }
        except (
            ImportError,
            AttributeError,
            NameError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as fallback_exc:
            return {
                "mode": "offline_simulated",
                "provider_calls": 0,
                "api": None,
                "worker": None,
                "constructed_inventory_matches": None,
                "live_process_inventory_observed": False,
                "reason": f"safe_inventory_build_failed:{type(fallback_exc).__name__}",
            }


def _budget_profile(env: Mapping[str, str]) -> dict[str, Any]:
    defaults = {
        "INVESTIGATION_MAX_CYCLES": 12,
        "INVESTIGATION_MAX_PIVOT_RUNS": 0,
        "INVESTIGATION_MAX_HITS_ACQUIRED": 0,
        "INVESTIGATION_MAX_NEW_SAMPLES": 0,
        "INVESTIGATION_MAX_VT_READ_UNITS": 0,
        "INVESTIGATION_MAX_SHODAN_CREDITS": 0,
        "INVESTIGATION_MAX_TOOL_SECONDS": 3600,
        "INVESTIGATION_MAX_CONTEXT_BYTES": 4_000_000,
        "INVESTIGATION_NO_PROGRESS_LIMIT": 3,
    }
    limits: dict[str, int | None] = {}
    for name, default in defaults.items():
        raw = env.get(name)
        try:
            limits[name] = int(raw) if raw is not None else default
        except ValueError:
            limits[name] = None
    return {
        "limits": limits,
        "source": "process_environment_or_code_defaults",
        "g2_approved": False,
        "external_disclosure_approved": False,
        "next_action": "operator_reviews_and_approves_a_case_specific_G2_envelope",
    }


def _environment_profile(env: Mapping[str, str]) -> dict[str, Any]:
    app_env_raw = env.get("APP_ENV", "development").casefold()
    app_env = (
        "production"
        if app_env_raw in PROTECTED_APP_ENVIRONMENTS
        else "test"
        if app_env_raw in {"test", "testing"}
        else "development"
        if app_env_raw in {"dev", "development", "local"}
        else "custom_or_unset"
    )
    return {
        "app_env": app_env,
        "platform": platform.system().lower(),
        "python_version": platform.python_version(),
        "conda_active": bool(env.get("CONDA_PREFIX")),
        "confinement_required": env.get(
            "LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT", "false"
        ).casefold()
        in {"1", "true", "yes"},
        "local_analysis_enabled": env.get("LOCAL_ANALYSIS_ENABLED", "false").casefold()
        in {"1", "true", "yes"},
        "operator_identity": "LocalIdentityProvider/dev-analyst",
        "operator_identity_verified": False,
        "operator_identity_scope": "local-isolated",
        "operator_identity_warning": (
            "accès local isolé uniquement; ce n'est pas une identité externe vérifiée"
        ),
    }


def _gate(
    gate_id: str,
    status: str,
    *,
    checked_at: str,
    git_sha: str | None,
    environment: Mapping[str, Any],
    evidence_refs: Sequence[str],
    next_action: str,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "status": normalize_gate_status(status),
        "checked_at": checked_at,
        "git_sha": git_sha,
        "environment": dict(environment),
        "evidence_refs": list(evidence_refs),
        "next_action": next_action,
    }


def _build_gates(
    *,
    checked_at: str,
    git_sha: str | None,
    environment: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> list[dict[str, Any]]:
    identity_status = "NOT_VALIDATED"
    api_worker_evidence = [
        "backend/src/cti_app/infrastructure/investigation_runtime_ports.py::describe_tool_inventory",
        "backend/src/cti_app/api/main.py",
        "backend/src/cti_app/workers/tasks.py",
    ]
    if inventory.get("constructed_inventory_matches") is False:
        api_worker_evidence.append("API/worker construction inventories differ")
    return [
        _gate(
            "L0",
            identity_status,
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=["CLI exact subject/run/artifact/version/SHA-256/TLP shape"],
            next_action=(
                "fournir puis confronter la référence exacte à la trace canonique; "
                "aucune sélection par titre/index"
            ),
        ),
        _gate(
            "L1",
            "NOT_VALIDATED",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=["git rev-parse/status", "local binary profile"],
            next_action="revoir l'environnement pilote isolé et valider les versions utiles",
        ),
        _gate(
            "L2",
            "NOT_VALIDATED",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=["loopback TCP probes only; no provider request"],
            next_action=(
                "faire confirmer Bridge et proxy VirusTotal par l'opérateur dans leur "
                "interface locale"
            ),
        ),
        _gate(
            "L3",
            "BLOCKED_APPROVAL",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=["G2 envelope is an explicit audited operator decision"],
            next_action="l'opérateur autorise une enveloppe G2 bornée et la divulgation",
        ),
        _gate(
            "L4",
            "NOT_VALIDATED",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=api_worker_evidence,
            next_action=(
                "obtenir une trace fournisseur réelle; l'inventaire hors ligne ne "
                "valide pas le chemin critique"
            ),
        ),
        _gate(
            "L5",
            "NOT_VALIDATED",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=[
                "LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT",
                "binary profile: bwrap",
            ],
            next_action=(
                "confirmer la quarantaine et le confinement sans lancer d'échantillon "
                "avant autorisation dédiée"
            ),
        ),
        _gate(
            "L6",
            "BLOCKED_APPROVAL",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=[
                "POST /api/investigations/{id}/opinion/compose",
                "POST /api/opinions/{id}/approve",
                "draft opinion PDF preview route not found",
            ],
            next_action=(
                "réviser l'avis et faire approuver G3; établir séparément le chemin "
                "d'aperçu PDF sans publication"
            ),
        ),
        _gate(
            "L7",
            "BLOCKED_APPROVAL",
            checked_at=checked_at,
            git_sha=git_sha,
            environment=environment,
            evidence_refs=[
                "publication explicitly excluded from the supervised first run"
            ],
            next_action=(
                "ne publier aucune release pendant ce pilote; vérifier l'impact sur "
                "l'ancienne release avant tout autre lot"
            ),
        ),
    ]


def build_report(
    args: argparse.Namespace, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    environment_values = dict(os.environ if env is None else env)
    checked_at = _utc_now()
    git = _git_snapshot()
    environment = _environment_profile(environment_values)
    reference = _validated_reference(args)
    database = _database_identity(environment_values.get("POSTGRES_DSN"))
    redis_url = environment_values.get("REDIS_URL", "redis://redis:6379/0")
    minio_endpoint = environment_values.get("S3_ENDPOINT", "minio:9000")
    bridge_url = environment_values.get(
        "OPENAI_BRIDGE_BASE_URL", "http://127.0.0.1:8001/v1"
    )
    proxy_url = environment_values.get("VIRUSTOTAL_PROXY_URL")
    services = {
        "redis": _probe_local_tcp("redis", redis_url),
        "minio": _probe_local_tcp("minio", minio_endpoint, default_scheme="http"),
        "bridge": _probe_local_tcp("bridge", bridge_url),
        "virustotal_proxy": _probe_local_tcp("virustotal_proxy", proxy_url),
    }
    binaries = _binary_profile()
    inventory = _safe_runtime_inventories()
    wiring = {
        "api_calls_shared_assembly": _source_calls_shared_assembly(
            ROOT / "backend" / "src" / "cti_app" / "api" / "main.py"
        ),
        "worker_calls_shared_assembly": _source_calls_shared_assembly(
            ROOT / "backend" / "src" / "cti_app" / "workers" / "tasks.py"
        ),
        "live_processes_queried": False,
    }
    reference_valid = bool(reference.get("valid"))
    bridge_accessible = services["bridge"]["state"] == "REACHABLE"
    virustotal_proxy_present = services["virustotal_proxy"]["state"] == "REACHABLE"
    signals: dict[str, bool | None] = {
        "python_312": environment["python_version"].startswith("3.12."),
        "conda_active": environment["conda_active"],
        "critical_path_only_mocked": None,
        "api_worker_match": None,
        "api_worker_structural_match": bool(
            wiring["api_calls_shared_assembly"]
            and wiring["worker_calls_shared_assembly"]
        ),
        "bridge_accessible": bridge_accessible,
        "virustotal_proxy_present": virustotal_proxy_present,
        "quarantine_ok": None,
        "tlp_known": reference_valid,
        "operator_identity_verified": False,
        "database_safe": bool(database.get("safe")),
        "publication_threatens_old_release": None,
    }
    conclusion = calculate_conclusion(signals)
    report = {
        "report_type": "supervised_first_run_readiness",
        "report_version": 1,
        "checked_at": checked_at,
        "git": git,
        "environment": environment,
        "reference": reference,
        "database": database,
        "binaries": binaries,
        "services": services,
        "tool_inventory": inventory,
        "api_worker_wiring": wiring,
        "g2_and_budgets": _budget_profile(environment_values),
        "signals": signals,
        "gates": _build_gates(
            checked_at=checked_at,
            git_sha=git.get("sha"),
            environment=environment,
            inventory=inventory,
        ),
        "conclusion": conclusion,
        "hard_rules": {
            "not_validated_is_pass": False,
            "offline_simulated_validates_live_gate": False,
            "ci_green_is_live_go": False,
            "provider_calls": 0,
            "sample_bodies_read": False,
            "database_writes": 0,
            "database_migrations": 0,
            "publication_requested": False,
        },
        "refusals": [database["reason"] for _ in [0] if not database.get("safe")],
        "next_action": "traiter les blocages NO_GO; aucun GO live ne découle de ce prévol",
    }
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    git = report.get("git", {})
    environment = report.get("environment", {})
    reference = report.get("reference", {})
    database = report.get("database", {})
    lines = [
        "# Readiness — premier run CTI supervisé",
        "",
        f"- Contrôlé à : `{report.get('checked_at')}`",
        (
            f"- Git : `{git.get('sha') or 'inconnu'}` — arbre "
            f"{'modifié' if git.get('tree_dirty') else 'propre/inconnu'} "
            f"({git.get('changed_path_count')} chemins)"
        ),
        (
            f"- Environnement : `{environment.get('app_env')}` / "
            f"Python `{environment.get('python_version')}` / "
            f"Conda actif `{environment.get('conda_active')}`"
        ),
        (
            f"- Identité opérateur : `{environment.get('operator_identity')}` — "
            f"{environment.get('operator_identity_warning')}"
        ),
        (
            f"- Confinement requis : `{environment.get('confinement_required')}`; "
            f"analyse locale activée : `{environment.get('local_analysis_enabled')}`"
        ),
        (
            f"- Base : `{database.get('state')}` — hôte `{database.get('host')}` / "
            f"base `{database.get('database')}` / schéma `{database.get('schema')}`; "
            f"migration lancée `{database.get('migrated')}`"
        ),
        f"- Conclusion : **{report.get('conclusion')}**",
        "",
        "## Référence exacte",
        "",
        "```json",
        json.dumps(reference, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Binaires",
        "",
        "| Binaire | Présence | Version |",
        "|---|---:|---|",
    ]
    for name, profile in report.get("binaries", {}).items():
        lines.append(
            f"| `{name}` | "
            f"{'PRÉSENT' if profile.get('present') else 'ABSENT'} | "
            f"{profile.get('version') or '—'} |"
        )
    lines.extend(
        [
            "",
            "## Services locaux",
            "",
            "| Service | État | Cible | Motif |",
            "|---|---|---|---|",
        ]
    )
    for name, service in report.get("services", {}).items():
        lines.append(
            f"| `{name}` | `{service.get('state')}` | "
            f"`{service.get('target') or '—'}` | `{service.get('reason')}` |"
        )
    lines.extend(["", "## Inventaire des tools", "", "```json"])
    lines.append(
        json.dumps(
            report.get("tool_inventory"), ensure_ascii=False, indent=2, sort_keys=True
        )
    )
    lines.extend(
        [
            "```",
            "",
            (
                "L'inventaire est construit hors ligne avec les adapters provider absents. "
                "Il n'atteste aucun appel réel."
            ),
            "",
        ]
    )
    lines.extend(["## G2 et budgets", "", "```json"])
    lines.append(
        json.dumps(
            report.get("g2_and_budgets"), ensure_ascii=False, indent=2, sort_keys=True
        )
    )
    lines.extend(
        [
            "```",
            "",
            "## Gates L0-L7",
            "",
            "| Gate | Statut | Prochaine action |",
            "|---|---|---|",
        ]
    )
    for gate in report.get("gates", []):
        lines.append(f"| `{gate['id']}` | `{gate['status']}` | {gate['next_action']} |")
    for gate in report.get("gates", []):
        lines.extend(
            [
                "",
                f"### {gate['id']} — {gate['status']}",
                "",
                f"- checked_at : `{gate['checked_at']}`",
                f"- git_sha : `{gate['git_sha'] or 'inconnu'}`",
                "- environment : `"
                + json.dumps(gate["environment"], ensure_ascii=False, sort_keys=True)
                + "`",
                "- evidence_refs :",
            ]
        )
        lines.extend(f"  - `{evidence}`" for evidence in gate["evidence_refs"])
        lines.append(f"- next_action : {gate['next_action']}")
    lines.extend(
        [
            "",
            "## Blocages et limites",
            "",
            f"- Refus/base : `{', '.join(report.get('refusals', [])) or 'aucun'}`.",
            "- Les probes sont limitées à TCP loopback. Aucun endpoint provider n'a été appelé.",
            (
                "- L'identité `LocalIdentityProvider/dev-analyst` autorise uniquement "
                "un accès local isolé; elle ne vérifie pas un opérateur externe."
            ),
            (
                "- Un résultat `offline_simulated`, une CI verte ou un gate "
                "`NOT_VALIDATED` ne valent jamais PASS/GO live."
            ),
            "- Aucune migration, écriture PostgreSQL, lecture de corps d'échantillon ou publication.",
            "",
        ]
    )
    return "\n".join(lines)


def render_text(report: Mapping[str, Any]) -> str:
    lines = [
        "PRÉVOL — PREMIER RUN CTI SUPERVISÉ (lecture seule)",
        f"Git SHA: {report.get('git', {}).get('sha') or 'inconnu'}",
        (
            f"Environnement: Python {report.get('environment', {}).get('python_version')} / "
            f"Conda actif={report.get('environment', {}).get('conda_active')}"
        ),
        (
            f"Arbre modifié: {report.get('git', {}).get('tree_dirty')} "
            f"({report.get('git', {}).get('changed_path_count')} chemins)"
        ),
        (
            f"Identité opérateur: {report.get('environment', {}).get('operator_identity')} — "
            f"{report.get('environment', {}).get('operator_identity_warning')}"
        ),
        (
            f"Confinement requis: {report.get('environment', {}).get('confinement_required')}; "
            f"analyse locale activée: {report.get('environment', {}).get('local_analysis_enabled')}"
        ),
        (
            f"Base: {report.get('database', {}).get('state')} / "
            f"{report.get('database', {}).get('host')} / "
            f"{report.get('database', {}).get('database')} / "
            f"schéma={report.get('database', {}).get('schema')} / "
            f"migration={report.get('database', {}).get('migrated')}"
        ),
        f"Conclusion: {report.get('conclusion')}",
        "Référence exacte: "
        + json.dumps(report.get("reference", {}), ensure_ascii=False, sort_keys=True),
        "",
        "Binaires:",
    ]
    for name, profile in report.get("binaries", {}).items():
        lines.append(
            f"  {name}: {'PRÉSENT' if profile.get('present') else 'ABSENT'} — "
            f"{profile.get('version') or '—'}"
        )
    lines.extend(["", "Services (TCP loopback uniquement):"])
    for name, service in report.get("services", {}).items():
        lines.append(
            f"  {name}: {service.get('state')} — "
            f"{service.get('target') or '—'} ({service.get('reason')})"
        )
    lines.extend(["", "Gates:"])
    lines.extend(
        f"  {gate['id']}: {gate['status']} — {gate['next_action']}"
        for gate in report.get("gates", [])
    )
    lines.extend(
        ["", "Refusals: " + (", ".join(report.get("refusals", [])) or "aucun")]
    )
    lines.append(
        "Aucun appel provider, corps d'échantillon, migration ou écriture PostgreSQL."
    )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prévol local en lecture seule; aucune migration ni requête provider."
    )
    parser.add_argument("--subject-id")
    parser.add_argument("--production-run-id")
    parser.add_argument("--synthesis-artifact-id")
    parser.add_argument("--version", type=int)
    parser.add_argument("--sha256")
    parser.add_argument("--tlp")
    parser.add_argument(
        "--format", choices=("text", "json", "markdown"), default="markdown"
    )
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--report-markdown", type=Path)
    parser.add_argument("--reduced-scope")
    parser.add_argument("--missing-dependency", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        reference = _validated_reference(args)
        if (
            reference.get("provided")
            and not reference.get("valid")
            and reference.get("missing_fields")
        ):
            parser.error(
                "incomplete exact reference: provide all six identity arguments"
            )
        if reference.get("provided") and not reference.get("valid"):
            parser.error(
                "invalid exact reference: check UUID, version, SHA-256 and TLP formats"
            )
    except (TypeError, ValueError):
        parser.error("invalid exact reference arguments")
    report = build_report(args)
    if args.reduced_scope or args.missing_dependency:
        report["conclusion"] = calculate_conclusion(
            report["signals"],
            reduced_scope=args.reduced_scope,
            missing_dependencies=args.missing_dependency,
        )
    json_output = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    markdown_output = render_markdown(report)
    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json_output, encoding="utf-8")
    if args.report_markdown:
        args.report_markdown.parent.mkdir(parents=True, exist_ok=True)
        args.report_markdown.write_text(markdown_output, encoding="utf-8")
    if args.format == "json":
        sys.stdout.write(json_output)
    elif args.format == "markdown":
        sys.stdout.write(markdown_output)
    else:
        sys.stdout.write(render_text(report) + "\n")
    return 2 if report["conclusion"] == "NO_GO" else 0


if __name__ == "__main__":
    raise SystemExit(main())
