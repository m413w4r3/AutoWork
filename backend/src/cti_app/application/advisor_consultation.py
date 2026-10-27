"""Pure advisor consultation preparation and collision-safe source references."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

from cti_app.application.advisor_dossier import (
    AdvisorDossier,
    build_advisor_dossier,
    evaluate_advisor_evidence_preflight,
)
from cti_app.application.advisor_prompt import render_advisor_prompt
from cti_app.application.publication_secret_guard import (
    contains_operational_secrets,
    redact_operational_secrets,
)
from cti_app.config import Settings

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_VERSIONED_OPINION_REF = re.compile(
    r"opinion:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r":v[1-9][0-9]*:[0-9a-f]{64}:[a-z][a-z0-9_]{0,63}\Z"
)


@dataclass(frozen=True, slots=True, order=True)
class SourceRef:
    """An exact source identity; opinion IDs must be versioned source refs."""

    kind: str
    id: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, str)
            or not self.kind
            or not isinstance(self.id, str)
            or not self.id
            or not isinstance(self.sha256, str)
            or _SHA256.fullmatch(self.sha256) is None
        ):
            raise ValueError("SourceRef requires a kind, id, and lowercase SHA-256")
        if self.kind == "opinion" and _VERSIONED_OPINION_REF.fullmatch(self.id) is None:
            raise ValueError("Opinion SourceRef IDs must identify an exact version and block")


@dataclass(frozen=True, slots=True)
class SourceRefConflict:
    """One `(kind, id)` identity observed with multiple valid digests."""

    kind: str
    id: str
    sha256s: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceRefCatalog:
    """Immutable observed refs and the exact aliases allowed to resolve."""

    refs: tuple[SourceRef, ...]
    transmitted_refs: tuple[SourceRef, ...]
    aliases: tuple[tuple[str, SourceRef], ...]
    review_targets: tuple[SourceRef, ...] = ()

    def __post_init__(self) -> None:
        refs = tuple(sorted(set(self.refs)))
        transmitted = tuple(sorted(set(self.transmitted_refs)))
        review_targets = tuple(sorted(set(self.review_targets)))
        if not set(transmitted).issubset(refs):
            raise ValueError("Transmitted refs must be present in the source catalog")
        if not set(review_targets).issubset(refs) or any(
            source_ref.kind != "opinion" for source_ref in review_targets
        ):
            raise ValueError("Review targets must be versioned opinion refs in the catalog")
        digest_by_identity: dict[tuple[str, str], str] = {}
        for source_ref in refs:
            identity = (source_ref.kind, source_ref.id)
            previous_digest = digest_by_identity.get(identity)
            if previous_digest is not None and previous_digest != source_ref.sha256:
                raise ValueError("Source catalog contains conflicting SHA-256 values")
            digest_by_identity[identity] = source_ref.sha256
        aliases: dict[str, SourceRef] = {}
        ambiguous: set[str] = set()
        for alias, source_ref in self.aliases:
            if (
                not isinstance(alias, str)
                or not alias
                or source_ref not in refs
                or (source_ref not in transmitted and source_ref not in review_targets)
            ):
                continue
            previous = aliases.get(alias)
            if previous is not None and previous != source_ref:
                aliases.pop(alias, None)
                ambiguous.add(alias)
            elif alias not in ambiguous:
                aliases[alias] = source_ref
        object.__setattr__(self, "refs", refs)
        object.__setattr__(self, "transmitted_refs", transmitted)
        object.__setattr__(self, "aliases", tuple(sorted(aliases.items())))
        object.__setattr__(self, "review_targets", review_targets)

    def resolve_alias(self, alias: str, *, expected_kind: str | None = None) -> SourceRef | None:
        """Resolve a transmitted alias, or an explicitly typed review target."""
        if not isinstance(alias, str) or not alias:
            return None
        source_ref = dict(self.aliases).get(alias)
        if source_ref is None or (expected_kind is not None and source_ref.kind != expected_kind):
            return None
        return source_ref


@dataclass(frozen=True, slots=True)
class PreparedConsultation:
    """Prepared prompt bytes, their digest, and immutable provenance metadata."""

    prompt_utf8: bytes
    prompt_sha256: str
    source_catalog: SourceRefCatalog
    dossier_text: str
    dossier_sha256: str
    dossier_bytes: int
    dossier_manifest: tuple[Mapping[str, Any], ...]
    completeness: Mapping[str, Any]
    redacted_secret_count: int


@dataclass(frozen=True, slots=True)
class PreparationRefusal:
    """Fail-closed preparation result with observable preflight details."""

    reason_code: str
    completeness: Mapping[str, Any] | None = None
    redacted_secret_count: int = 0
    conflicts: tuple[SourceRefConflict, ...] = ()


def prepare(projection: Mapping[str, Any]) -> PreparedConsultation | PreparationRefusal:
    """Render deterministic UTF-8 prompt bytes from a disclosure- and secret-filtered projection.

    This function performs no persistence, transport, authorization, or budget
    work. The caller supplies the refs that actually accompany this dispatch and
    must remove dotenv-only secrets before calling this pure preparation seam.
    """
    if not isinstance(projection, Mapping):
        return PreparationRefusal(reason_code="invalid_projection")
    try:
        return _prepare_projection(projection)
    except (AttributeError, KeyError, RecursionError, TypeError, UnicodeError, ValueError):
        return PreparationRefusal(reason_code="invalid_projection")


def _prepare_projection(projection: Mapping[str, Any]) -> PreparedConsultation | PreparationRefusal:
    # Disable dotenv explicitly: callers provide a disclosure- and secret-filtered
    # projection, and this pure preparation step must never read a config file.
    settings = Settings(_env_file=None)
    guard_settings = SimpleNamespace(
        **{
            name: str(value) if isinstance(value, Path) else value
            for name, value in vars(settings).items()
        }
    )
    cleaned, redacted_secret_count = redact_operational_secrets(
        projection, settings=guard_settings, include_host_paths=False
    )
    if not isinstance(cleaned, Mapping):
        return PreparationRefusal(
            reason_code="invalid_projection", redacted_secret_count=redacted_secret_count
        )
    prompt_data = dict(cleaned)
    raw_manifest = prompt_data.get("dossier_manifest")
    dossier = build_advisor_dossier(prompt_data)
    prompt_data["dossier_manifest"] = [dict(item) for item in dossier.manifest]
    prompt_data["dossier_text"] = dossier.text
    prompt_data["dossier_sha256"] = dossier.sha256
    prompt_data["dossier_bytes"] = dossier.byte_size

    if contains_operational_secrets(cleaned, settings=guard_settings, include_host_paths=False):
        completeness = evaluate_advisor_evidence_preflight(prompt_data, dossier, dossier.text)
        return PreparationRefusal(
            reason_code="secret_field_remaining",
            completeness=_freeze(completeness),
            redacted_secret_count=redacted_secret_count,
        )

    consultation_type = prompt_data.get("consultation_type")
    if not isinstance(consultation_type, str):
        consultation_type = ""
    if not isinstance(prompt_data.get("transmitted_refs"), (list, tuple)):
        prompt_data["transmitted_refs"] = []

    prompt = render_advisor_prompt(consultation_type, prompt_data)
    prompt_utf8 = prompt.encode("utf-8")
    completeness = evaluate_advisor_evidence_preflight(prompt_data, dossier, prompt)
    conflicts = _identity_conflicts(prompt_data, dossier, raw_manifest=raw_manifest)
    if conflicts:
        return PreparationRefusal(
            reason_code="ref_reference_conflict",
            completeness=_freeze(completeness),
            redacted_secret_count=redacted_secret_count,
            conflicts=conflicts,
        )
    if completeness.get("status") != "READY":
        status = str(completeness.get("status", "UNKNOWN")).casefold()
        return PreparationRefusal(
            reason_code=f"evidence_preflight_{status}",
            completeness=_freeze(completeness),
            redacted_secret_count=redacted_secret_count,
        )

    catalog = _source_catalog(prompt_data, dossier, raw_manifest=raw_manifest)
    return PreparedConsultation(
        prompt_utf8=prompt_utf8,
        prompt_sha256=hashlib.sha256(prompt_utf8).hexdigest(),
        source_catalog=catalog,
        dossier_text=dossier.text,
        dossier_sha256=dossier.sha256,
        dossier_bytes=dossier.byte_size,
        dossier_manifest=tuple(_freeze(dict(item)) for item in dossier.manifest),
        completeness=_freeze(completeness),
        redacted_secret_count=redacted_secret_count,
    )


def _identity_conflicts(
    prompt_data: Mapping[str, Any],
    dossier: AdvisorDossier,
    *,
    raw_manifest: object,
) -> tuple[SourceRefConflict, ...]:
    identities: dict[tuple[str, str], set[str]] = {}
    for candidate in _projection_refs(prompt_data):
        _record_identity(identities, candidate)
    for item in _mapping_rows(raw_manifest):
        for candidate in _manifest_refs(item):
            _record_identity(identities, candidate)
    for item in dossier.manifest:
        for candidate in _manifest_refs(item):
            _record_identity(identities, candidate)

    return tuple(
        SourceRefConflict(kind=kind, id=identifier, sha256s=tuple(sorted(digests)))
        for (kind, identifier), digests in sorted(identities.items())
        if len(digests) > 1
    )


def _source_catalog(
    prompt_data: Mapping[str, Any],
    dossier: AdvisorDossier,
    *,
    raw_manifest: object,
) -> SourceRefCatalog:
    transmitted = tuple(
        candidate
        for item in _mapping_rows(prompt_data.get("transmitted_refs"))
        if (candidate := _transmitted_ref(item)) is not None
    )
    transmitted_by_identity = {(item.kind, item.id): item for item in transmitted}
    restricted_identities = _restricted_identities(prompt_data)
    refs = set(_projection_refs(prompt_data))
    for item in _mapping_rows(raw_manifest):
        refs.update(_manifest_refs(item))
    for item in dossier.manifest:
        refs.update(_manifest_refs(item))
    review_targets: set[SourceRef] = set()
    alias_candidates: list[tuple[str, SourceRef]] = []
    for item in dossier.manifest:
        alias = item.get("alias")
        candidate = _manifest_ref(item)
        if not isinstance(alias, str) or candidate is None:
            continue
        identity = (candidate.kind, _unprefixed_identifier(candidate.kind, candidate.id))
        if identity in restricted_identities:
            continue
        explicitly_typed_review = _is_versioned_review_target(item, candidate)
        if transmitted_by_identity.get((candidate.kind, candidate.id)) == candidate:
            alias_candidates.append((alias, candidate))
        elif explicitly_typed_review:
            review_targets.add(candidate)
            alias_candidates.append((alias, candidate))
    return SourceRefCatalog(
        refs=tuple(refs | set(transmitted) | review_targets),
        transmitted_refs=transmitted,
        aliases=tuple(alias_candidates),
        review_targets=tuple(review_targets),
    )


def _restricted_identities(data: Mapping[str, Any]) -> set[tuple[str, str]]:
    restricted: set[tuple[str, str]] = set()
    for field_name, kind in (
        ("disclosure_restricted_refs", "observation"),
        ("disclosure_restricted_finding_refs", "finding"),
    ):
        value = data.get(field_name)
        if isinstance(value, str):
            rows: tuple[object, ...] = (value,)
        elif isinstance(value, (list, tuple, set, frozenset)):
            rows = tuple(value)
        else:
            continue
        for item in rows:
            if isinstance(item, str) and item:
                restricted.add((kind, _unprefixed_identifier(kind, item)))
            elif isinstance(item, Mapping):
                item_kind = item.get("kind", kind)
                identifier = item.get("id")
                if isinstance(item_kind, str) and isinstance(identifier, str) and identifier:
                    restricted.add((item_kind, _unprefixed_identifier(item_kind, identifier)))
    return restricted


def find_source_ref_conflicts(
    prompt_data: Mapping[str, Any],
    dossier: AdvisorDossier,
    *,
    raw_manifest: object,
) -> tuple[SourceRefConflict, ...]:
    """Return conflicts among the source identities available to a parser."""
    return _identity_conflicts(prompt_data, dossier, raw_manifest=raw_manifest)


def build_source_ref_catalog(
    prompt_data: Mapping[str, Any],
    dossier: AdvisorDossier,
    *,
    raw_manifest: object,
) -> SourceRefCatalog:
    """Build the public, immutable source catalog for a parser context."""
    return _source_catalog(prompt_data, dossier, raw_manifest=raw_manifest)


def restricted_source_identities(data: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """Return source identities excluded by disclosure restrictions."""
    return frozenset(_restricted_identities(data))


def _unprefixed_identifier(kind: str, identifier: str) -> str:
    prefix = f"{kind}:"
    return identifier.removeprefix(prefix)


def _projection_refs(data: Mapping[str, Any]) -> tuple[SourceRef, ...]:
    refs: list[SourceRef] = []

    def add(kind: object, identifier: object, digest: object) -> None:
        candidate = _make_ref(kind, identifier, digest)
        if candidate is not None:
            refs.append(candidate)

    for item in _mapping_rows(data.get("transmitted_refs")):
        add(item.get("kind"), item.get("id"), item.get("sha256"))
    for item in _mapping_rows(data.get("evidence")):
        add("observation", item.get("evidence_id"), item.get("sha256"))
    for item in _mapping_rows(data.get("findings")):
        add("finding", item.get("finding_id"), item.get("sha256"))
    for item in _mapping_rows(data.get("assertions")):
        for ref in _mapping_rows(item.get("refs")):
            add(ref.get("kind"), ref.get("id"), ref.get("sha256"))
    for item in _mapping_rows(data.get("measurements")):
        evidence_id = item.get("evidence_id")
        evidence_sha = item.get("evidence_sha256")
        add("observation", evidence_id, evidence_sha)
        measurement_id = item.get("measurement_id")
        if isinstance(measurement_id, str):
            add("measurement", measurement_id, hashlib.sha256(_canonical_bytes(item)).hexdigest())
    for item in _mapping_rows(data.get("network_facts")):
        add("fact", item.get("fact_id"), item.get("observation_sha256"))
        add("observation", item.get("evidence_id"), item.get("observation_sha256"))
    article_id = data.get("article_source_ref")
    if isinstance(article_id, str) and article_id.startswith("article:"):
        article_id = article_id.partition(":")[2]
    add("article", article_id, data.get("article_sha256"))
    if _mapping_rows(data.get("article_passages")):
        add("article", article_id, data.get("article_sha256"))
    for item in _mapping_rows(data.get("opinion_sections")):
        add("opinion", item.get("source_ref"), item.get("opinion_sha256"))
    for item in _mapping_rows(data.get("units")):
        if item.get("source_ref") is not None:
            add("opinion", item.get("source_ref"), item.get("opinion_sha256"))
    legacy_review = data.get("review_text")
    if isinstance(legacy_review, Mapping) and legacy_review.get("source_ref") is not None:
        add("opinion", legacy_review.get("source_ref"), legacy_review.get("opinion_sha256"))
    return tuple(refs)


def _manifest_refs(item: Mapping[str, Any]) -> tuple[SourceRef, ...]:
    kind = item.get("kind")
    if not isinstance(kind, str):
        return ()
    identifier = item.get("id")
    digest = item.get("sha256")
    if kind in {"measurement", "fact"}:
        own_ref = _make_ref(kind, identifier, digest)
        observation_ref = _make_ref(
            "observation", item.get("evidence_id"), item.get("evidence_sha256")
        )
        return tuple(ref for ref in (own_ref, observation_ref) if ref is not None)
    elif kind == "article" and isinstance(identifier, str) and identifier.startswith("article:"):
        identifier = identifier.partition(":")[2]
    elif kind == "opinion":
        identifier = item.get("source_ref")
    candidate = _make_ref(kind, identifier, digest)
    return (candidate,) if candidate is not None else ()


def _manifest_ref(item: Mapping[str, Any]) -> SourceRef | None:
    kind = item.get("kind")
    if kind in {"measurement", "fact"}:
        return _make_ref("observation", item.get("evidence_id"), item.get("evidence_sha256"))
    refs = _manifest_refs(item)
    return refs[0] if refs else None


def _transmitted_ref(item: Mapping[str, Any]) -> SourceRef | None:
    kind = item.get("kind")
    identifier = item.get("id")
    if kind == "article" and isinstance(identifier, str) and identifier.startswith("article:"):
        identifier = identifier.partition(":")[2]
    return _make_ref(kind, identifier, item.get("sha256"))


def _make_ref(kind: object, identifier: object, digest: object) -> SourceRef | None:
    if (
        not isinstance(kind, str)
        or not isinstance(identifier, str)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        return None
    if kind == "opinion" and _VERSIONED_OPINION_REF.fullmatch(identifier) is None:
        return None
    try:
        return SourceRef(kind=kind, id=identifier, sha256=digest)
    except ValueError:
        return None


def _is_versioned_review_target(item: Mapping[str, Any], source_ref: SourceRef) -> bool:
    if item.get("origin") != "versioned_review_target" or source_ref.kind != "opinion":
        return False
    identifier = item.get("id")
    digest = item.get("sha256")
    source_ref_text = item.get("source_ref")
    if not isinstance(identifier, str) or not isinstance(digest, str):
        return False
    if source_ref_text != source_ref.id or digest != source_ref.sha256:
        return False
    parts = source_ref.id.split(":")
    if len(parts) != 5 or parts[0] != "opinion" or parts[1] != identifier or parts[3] != digest:
        return False
    version = item.get("opinion_version")
    block_type = item.get("block_type")
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > 0
        and parts[2] == f"v{version}"
        and isinstance(block_type, str)
        and parts[4] == block_type
    )


def _record_identity(identities: dict[tuple[str, str], set[str]], source_ref: SourceRef) -> None:
    identities.setdefault((source_ref.kind, source_ref.id), set()).add(source_ref.sha256)


def _mapping_rows(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _canonical_bytes(value: object) -> bytes:
    import json

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, set):
        return frozenset(_freeze(child) for child in value)
    return value


__all__ = [
    "PreparationRefusal",
    "PreparedConsultation",
    "SourceRef",
    "SourceRefCatalog",
    "SourceRefConflict",
    "prepare",
]
