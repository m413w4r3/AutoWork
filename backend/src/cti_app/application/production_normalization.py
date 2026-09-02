"""Deterministic normalization and display rules for technical artifacts."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from cti_app.domain.publication import ArtifactType

_DOT = re.compile(r"\[\.\]|\(\.\)|\{\.\}", re.IGNORECASE)
_COLON = re.compile(r"\[:\]", re.IGNORECASE)
_AT = re.compile(r"\[(?:at|@)\]|\((?:at|@)\)", re.IGNORECASE)
# Soft hyphen and zero-width characters are line-wrapping artifacts of the
# publishing pipeline, never part of an indicator.  Removing them here keeps a
# copied value valid, comparable and deduplicable instead of silently invalid.
_INVISIBLE = str.maketrans(dict.fromkeys("\u00ad\u200b\u200c\u200d\u2060\ufeff"))


def refang(raw: str) -> str:
    """Undo supported CTI defanging without performing validation."""
    value = raw.strip().translate(_INVISIBLE).replace(r"\:", ":")
    value = _DOT.sub(".", value)
    value = _COLON.sub(":", value)
    value = _AT.sub("@", value)
    if value.lower().startswith("hxxps://"):
        value = "https://" + value[8:]
    elif value.lower().startswith("hxxp://"):
        value = "http://" + value[7:]
    return value


def normalize_indicator_value(raw: str, artifact_type: ArtifactType) -> str:
    """Return the fang-ed canonical representation while preserving no metadata."""
    value = refang(raw)
    if artifact_type is ArtifactType.DOMAIN:
        return value.rstrip(".").lower()
    if artifact_type is ArtifactType.IP:
        return str(ipaddress.ip_address(value))
    if artifact_type is ArtifactType.HASH:
        return value.lower()
    if artifact_type is ArtifactType.EMAIL:
        local, separator, domain = value.partition("@")
        return f"{local}{separator}{domain.lower()}" if separator else value
    if artifact_type is ArtifactType.URL:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return value
        hostname = parts.hostname.lower() if parts.hostname else ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        userinfo = ""
        if parts.username:
            userinfo = parts.username
            if parts.password:
                userinfo += f":{parts.password}"
            userinfo += "@"
        # Accessing ``port`` validates it and intentionally raises ValueError
        # for malformed URLs. Callers treat one invalid literal as non-fatal.
        port_number = parts.port
        port = f":{port_number}" if port_number else ""
        return urlunsplit(
            (
                parts.scheme.lower(),
                f"{userinfo}{hostname}{port}",
                parts.path,
                parts.query,
                parts.fragment,
            )
        )
    return value


def canonical_indicator_key(raw: str, artifact_type: ArtifactType) -> str:
    """Key used for exact, type-aware indicator comparison and deduplication."""
    return normalize_indicator_value(raw, artifact_type)


def display_indicator_value(value: str, artifact_type: ArtifactType, *, defanged: bool) -> str:
    """Render a canonical indicator for prose (defanged) or the IOC inventory."""
    canonical = normalize_indicator_value(value, artifact_type)
    if not defanged:
        return canonical
    if artifact_type is ArtifactType.URL:
        canonical = re.sub(r"^https://", "hxxps://", canonical, flags=re.IGNORECASE)
        canonical = re.sub(r"^http://", "hxxp://", canonical, flags=re.IGNORECASE)
        scheme, separator, remainder = canonical.partition("://")
        return f"{scheme}{separator}{remainder.replace('.', '[.]')}"
    if artifact_type in {ArtifactType.DOMAIN, ArtifactType.IP}:
        return canonical.replace(".", "[.]")
    if artifact_type is ArtifactType.EMAIL:
        local, separator, domain = canonical.partition("@")
        return f"{local}(at){domain.replace('.', '[.]')}" if separator else canonical
    return canonical
