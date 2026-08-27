#!/usr/bin/env python3
"""Fail-closed validation of the isolated M2 analysis-worker Compose configuration.

Expected input is JSON produced by:
    docker compose config --format json

The script is standalone and performs no network access.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_SERVICE = "analysis-worker"
DEFAULT_NETWORK = "analysis-internal"
FORBIDDEN_ENV_PREFIXES = ("OPENAI_", "QWEN_", "VIRUSTOTAL_", "BRIDGE_")
ALLOWED_ENV_EXACT = {
    "APP_ENV",
    "LOG_LEVEL",
    "POSTGRES_DSN",
    "REDIS_URL",
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
    "S3_SECURE",
    "JOB_RETRY_BASE_SECONDS",
    "JOB_RETRY_MAX_SECONDS",
    "JOB_HEARTBEAT_TIMEOUT_SECONDS",
    "JOB_RECOVERY_INTERVAL_SECONDS",
    "JOB_ACTOR_TIME_LIMIT_SECONDS",
}
ALLOWED_ENV_PREFIXES = ("ANALYSIS_", "CAPA_", "SMDA_", "CODE_NGRAM_")


class ComposeGuardError(ValueError):
    pass


def _environment_keys(service: dict[str, Any]) -> set[str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {str(key) for key in environment}
    if isinstance(environment, list):
        keys: set[str] = set()
        for item in environment:
            if not isinstance(item, str):
                raise ComposeGuardError("service environment list contains a non-string entry")
            keys.add(item.split("=", 1)[0])
        return keys
    raise ComposeGuardError("service environment must be an object or list")


def _service_networks(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", {})
    if isinstance(networks, dict):
        return {str(name) for name in networks}
    if isinstance(networks, list):
        return {str(name) for name in networks}
    raise ComposeGuardError("service networks must be an object or list")


def _extra_hosts(service: dict[str, Any]) -> list[str]:
    extra_hosts = service.get("extra_hosts", [])
    if isinstance(extra_hosts, dict):
        return [f"{key}:{value}" for key, value in extra_hosts.items()]
    if isinstance(extra_hosts, list):
        return [str(value) for value in extra_hosts]
    raise ComposeGuardError("extra_hosts must be an object or list")


def _volume_tokens(service: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        raise ComposeGuardError("service volumes must be a list")
    for item in volumes:
        if isinstance(item, str):
            tokens.append(item)
        elif isinstance(item, dict):
            for key in ("source", "target"):
                value = item.get(key)
                if value is not None:
                    tokens.append(str(value))
        else:
            raise ComposeGuardError("service volumes contains an unsupported entry")
    return tokens


def validate_compose(
    payload: dict[str, Any],
    *,
    service_name: str = DEFAULT_SERVICE,
    network_name: str = DEFAULT_NETWORK,
) -> list[str]:
    services = payload.get("services")
    if not isinstance(services, dict):
        raise ComposeGuardError("compose config has no services object")
    service = services.get(service_name)
    if not isinstance(service, dict):
        raise ComposeGuardError(f"missing service {service_name!r}")

    errors: list[str] = []
    env_keys = _environment_keys(service)
    forbidden = sorted(key for key in env_keys if key.startswith(FORBIDDEN_ENV_PREFIXES))
    if forbidden:
        errors.append("forbidden environment keys: " + ", ".join(forbidden))
    unexpected = sorted(
        key
        for key in env_keys
        if key not in ALLOWED_ENV_EXACT and not key.startswith(ALLOWED_ENV_PREFIXES)
    )
    if unexpected:
        errors.append("unexpected environment keys: " + ", ".join(unexpected))

    service_networks = _service_networks(service)
    if service_networks != {network_name}:
        errors.append(
            f"{service_name} must join only {network_name!r}; got {sorted(service_networks)!r}"
        )
    networks = payload.get("networks")
    network = networks.get(network_name) if isinstance(networks, dict) else None
    if not isinstance(network, dict) or network.get("internal") is not True:
        errors.append(f"network {network_name!r} must exist with internal=true")

    if service.get("ports"):
        errors.append(f"{service_name} must not publish ports")

    hosts = _extra_hosts(service)
    if any("host.docker.internal" in item for item in hosts):
        errors.append("host.docker.internal is forbidden")

    volume_tokens = _volume_tokens(service)
    forbidden_volume_tokens = (
        "subject_workspaces",
        "/work/subjects",
        "var/diagnostics",
        "/var/diagnostics",
    )
    bad_volumes = sorted(
        token
        for token in volume_tokens
        if any(forbidden in token for forbidden in forbidden_volume_tokens)
    )
    if bad_volumes:
        errors.append("forbidden volumes: " + ", ".join(bad_volumes))
    return errors


def _load(path: Path | None) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8") if path is not None else sys.stdin.read()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ComposeGuardError("input is not valid Compose JSON") from exc
    if not isinstance(payload, dict):
        raise ComposeGuardError("Compose JSON root must be an object")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config_json",
        nargs="?",
        type=Path,
        help="docker compose config --format json output; stdin when omitted",
    )
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--network", default=DEFAULT_NETWORK)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        errors = validate_compose(
            _load(args.config_json),
            service_name=args.service,
            network_name=args.network,
        )
    except ComposeGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"{args.service}: isolated configuration OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
