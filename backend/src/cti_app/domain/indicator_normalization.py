"""Pure normalization rules for technical indicator values."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from cti_app.domain.publication import ArtifactType

_DOT = re.compile(r"\[\.\]|\(\.\)|\{\.\}", re.IGNORECASE)
_COLON = re.compile(r"\[:\]", re.IGNORECASE)
_AT = re.compile(r"\[(?:at|@)\]|\((?:at|@)\)", re.IGNORECASE)
_SSDEEP_HASH = re.compile(r"[1-9][0-9]*:[A-Za-z0-9+/]{1,64}:[A-Za-z0-9+/]{1,64}")
_TLSH_HASH = re.compile(r"T1[0-9a-f]{70}", re.IGNORECASE)
_INVISIBLE = str.maketrans(dict.fromkeys("\u00ad\u200b\u200c\u200d\u2060\ufeff"))


def refang(raw: str) -> str:
    """Undo supported CTI defanging without performing validation."""
    value = raw.strip().translate(_INVISIBLE).replace(r"\:", ":")
    value = _DOT.sub(".", value)
    value = _COLON.sub(":", value)
    value = _AT.sub("@", value)
    if value.lower().startswith("hxxps://"):
        return "https://" + value[8:]
    if value.lower().startswith("hxxp://"):
        return "http://" + value[7:]
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


def normalize_publication_indicator_value(raw: str, artifact_type: ArtifactType) -> str:
    """Normalize a publication IOC without changing extraction identities.

    Extraction keeps the source-verified original value and uses the stricter
    ``normalize_indicator_value`` for its deduplication key. This publication
    projection accepts endpoint-shaped values without changing that
    extraction identity or discarding the original value from IOC detail rows.
    Case-sensitive fuzzy hashes keep their digest alphabet, while TLSH uses its
    canonical uppercase spelling. Other hash values keep the existing lowercase
    normalization. URL whitespace is retained so publication rendering can omit
    and count malformed values instead of silently trimming them into a URL.
    """
    if artifact_type is ArtifactType.URL and any(character.isspace() for character in raw):
        return raw
    if artifact_type is ArtifactType.HASH:
        value = refang(raw)
        if _SSDEEP_HASH.fullmatch(value):
            return value
        if _TLSH_HASH.fullmatch(value):
            return value.upper()
        return normalize_indicator_value(value, artifact_type)
    try:
        return normalize_indicator_value(raw, artifact_type)
    except ValueError:
        if artifact_type is not ArtifactType.IP:
            raise
    return _normalize_ip_endpoint(raw)


def publication_indicator_identity_key(normalized_value: str, artifact_type: ArtifactType) -> str:
    """Return a publication dedup key, retaining ssdeep's case-sensitive alphabet."""
    if artifact_type is ArtifactType.HASH and _SSDEEP_HASH.fullmatch(normalized_value):
        return normalized_value
    return normalized_value.casefold()


def _normalize_ip_endpoint(raw: str) -> str:
    value = refang(raw)
    if value.lower().startswith(("http://", "https://")):
        try:
            parts = urlsplit(value)
            hostname = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise ValueError("Invalid IP endpoint") from exc
        if (
            parts.scheme.lower() not in {"http", "https"}
            or hostname is None
            or port is None
            or parts.username is not None
            or parts.password is not None
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Invalid IP endpoint")
        address = ipaddress.ip_address(hostname)
    elif value.startswith("["):
        closing = value.find("]")
        if closing < 0 or value[closing + 1 : closing + 2] != ":":
            raise ValueError("Invalid IP endpoint")
        address = ipaddress.ip_address(value[1:closing])
        if address.version != 6:
            raise ValueError("Invalid IP endpoint")
        port_text = value[closing + 2 :]
        _validate_endpoint_port(port_text)
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator or ":" in host:
            raise ValueError("Invalid IP endpoint")
        address = ipaddress.ip_address(host)
        if address.version != 4:
            raise ValueError("Invalid IP endpoint")
        _validate_endpoint_port(port_text)
    return str(address)


def _validate_endpoint_port(value: str) -> None:
    if not value.isascii() or not value.isdecimal():
        raise ValueError("Invalid IP endpoint port")
    if not 1 <= int(value) <= 65535:
        raise ValueError("Invalid IP endpoint port")


def canonical_indicator_key(raw: str, artifact_type: ArtifactType) -> str:
    """Key used for exact, type-aware indicator comparison and deduplication."""
    return normalize_indicator_value(raw, artifact_type)


def display_indicator_value(value: str, artifact_type: ArtifactType, *, defanged: bool) -> str:
    """Render a canonical indicator for prose (defanged) or the IOC inventory."""
    canonical = normalize_indicator_value(value, artifact_type)
    return _display_normalized_indicator_value(canonical, artifact_type, defanged=defanged)


def display_publication_indicator_value(
    value: str, artifact_type: ArtifactType, *, defanged: bool
) -> str:
    """Render a publication IOC using its publication-specific canonical form."""
    canonical = normalize_publication_indicator_value(value, artifact_type)
    return _display_normalized_indicator_value(canonical, artifact_type, defanged=defanged)


def _display_normalized_indicator_value(
    canonical: str, artifact_type: ArtifactType, *, defanged: bool
) -> str:
    if not defanged:
        return canonical
    if artifact_type is ArtifactType.URL:
        canonical = re.sub(r"^https://", "hxxps://", canonical, flags=re.IGNORECASE)
        canonical = re.sub(r"^http://", "hxxp://", canonical, flags=re.IGNORECASE)
        scheme, separator, remainder = canonical.partition("://")
        if not separator:
            return canonical.replace(".", "[.]")
        return f"{scheme}{separator}{remainder.replace('.', '[.]')}"
    if artifact_type in {ArtifactType.DOMAIN, ArtifactType.IP}:
        return canonical.replace(".", "[.]")
    if artifact_type is ArtifactType.EMAIL:
        local, separator, domain = canonical.partition("@")
        return f"{local}(at){domain.replace('.', '[.]')}" if separator else canonical
    return canonical
