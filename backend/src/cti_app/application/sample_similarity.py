"""Application orchestration for persisted, exact-sample similarity evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any, BinaryIO, Protocol
from uuid import UUID

from cti_app.application.research_context_builder import SampleFeaturePackPort
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.goodware_index import SUPPORTED_FEATURE_KINDS
from cti_app.domain.sample_similarity import (
    CorpusSnapshot,
    ExtractorVersion,
    FamilyFeatureComparisonV1,
    FeatureLoss,
    FeatureSource,
    MeasuredFeature,
    SimilarityFeatureKind,
    SimilarityLimits,
    SimilarityPolicy,
    SimilaritySampleInput,
    build_family_feature_comparison,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SIMILARITY_BUCKET = "investigation-sample-similarity"
_JSON_MIME_TYPE = "application/vnd.autowork.family-feature-comparison+json"


class CorpusSnapshotPort(Protocol):
    """Read an immutable view of exact sample associations for one investigation."""

    async def get_snapshot(self, investigation_id: UUID) -> CorpusSnapshot: ...


class PreparedGoodwarePrevalencePort(Protocol):
    baseline_fingerprint_sha256: str
    index_descriptor: BlobDescriptor

    async def lookup_batch(
        self, features: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], int]: ...


class GoodwarePrevalencePort(Protocol):
    async def prepare(self, baseline_ref: UUID | str) -> PreparedGoodwarePrevalencePort: ...


class SimilarityArtifactStorePort(Protocol):
    async def ingest(
        self, source: BinaryIO, *, logical_bucket: str, mime_type: str
    ) -> BlobRecord: ...


class SampleSimilarityError(RuntimeError):
    """A frozen-snapshot, feature-pack, or artifact contract failed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredFamilyFeatureComparison:
    comparison: FamilyFeatureComparisonV1
    artifact_blob_id: UUID
    artifact_sha256: str
    artifact_size: int


class SampleSimilarityService:
    """Compare exact SHA-256 samples using feature packs already stored by AutoWork."""

    def __init__(
        self,
        *,
        feature_packs: SampleFeaturePackPort,
        corpus_snapshots: CorpusSnapshotPort,
        artifacts: SimilarityArtifactStorePort,
        goodware: GoodwarePrevalencePort | None = None,
        policy: SimilarityPolicy | None = None,
    ) -> None:
        self._feature_packs = feature_packs
        self._corpus_snapshots = corpus_snapshots
        self._artifacts = artifacts
        self._goodware = goodware
        self._policy = policy or SimilarityPolicy()

    @property
    def policy(self) -> SimilarityPolicy:
        return self._policy

    async def compare(
        self,
        *,
        investigation_id: UUID,
        sample_sha256s: Sequence[str],
    ) -> StoredFamilyFeatureComparison:
        selected = _canonical_sample_hashes(sample_sha256s, self._policy.maximum_selected_samples)
        snapshot = await self._corpus_snapshots.get_snapshot(investigation_id)
        if snapshot.investigation_id != investigation_id:
            raise SampleSimilarityError("corpus snapshot is bound to another investigation")
        unknown = set(selected) - set(snapshot.sample_sha256s)
        if unknown:
            raise SampleSimilarityError(
                "every comparison sample must be in the frozen investigation corpus"
            )

        snapshot, prepared_goodware = await self._prepare_goodware(snapshot)
        context_hashes = tuple(
            sample_hash for sample_hash in snapshot.sample_sha256s if sample_hash not in selected
        )
        maximum = self._policy.maximum_corpus_samples
        analysis_hashes = (*selected, *context_hashes[: max(0, maximum - len(selected))])
        corpus_limit_applied = len(snapshot.members) > len(analysis_hashes)

        sample_inputs: list[SimilaritySampleInput] = []
        missing: list[str] = []
        per_sample_limits: list[str] = []
        feature_loss_counts: dict[str, int] = {}
        feature_sets_by_sample: dict[str, tuple[MeasuredFeature, ...]] = {}
        for sample_hash in analysis_hashes:
            raw_pack = await self._feature_packs.get_feature_pack(sample_hash)
            if raw_pack is None:
                sample_inputs.append(
                    SimilaritySampleInput(sample_sha256=sample_hash, measured=False)
                )
                missing.append(sample_hash)
                continue
            if not isinstance(raw_pack, Mapping):
                raise SampleSimilarityError(
                    "feature-pack reader returned a non-object feature pack"
                )
            declared_hash = raw_pack.get("sample_sha256")
            if declared_hash is not None and declared_hash != sample_hash:
                raise SampleSimilarityError("feature pack is bound to another sample SHA-256")
            (
                features,
                extractor_versions,
                packer_markers,
                was_limited,
                pack_losses,
            ) = _extract_pack(raw_pack, sample_sha256=sample_hash, policy=self._policy)
            for cause, count in pack_losses:
                _increment_loss(feature_loss_counts, cause, count)
            if was_limited:
                per_sample_limits.append(sample_hash)
            feature_sets_by_sample[sample_hash] = features
            sample_inputs.append(
                SimilaritySampleInput(
                    sample_sha256=sample_hash,
                    measured=True,
                    features=features,
                    extractor_versions=extractor_versions,
                    packer_markers=packer_markers,
                )
            )

        feature_keys = _selected_feature_keys(feature_sets_by_sample, self._policy.maximum_features)
        feature_limit_applied = any(
            feature.key not in feature_keys
            for sample_features in feature_sets_by_sample.values()
            for feature in sample_features
        )
        if feature_limit_applied:
            discarded_feature_count = sum(
                feature.key not in feature_keys
                for sample_features in feature_sets_by_sample.values()
                for feature in sample_features
            )
            _increment_loss(feature_loss_counts, "maximum_total_features", discarded_feature_count)
            sample_inputs = [
                replace(
                    item,
                    features=tuple(
                        feature for feature in item.features if feature.key in feature_keys
                    ),
                )
                for item in sample_inputs
            ]
        descriptors_by_feature = _goodware_descriptors(
            (feature for item in sample_inputs for feature in item.features),
        )
        goodware_counts: dict[str, int] = {}
        if prepared_goodware is not None and descriptors_by_feature:
            raw_counts = await prepared_goodware.lookup_batch(
                tuple(sorted(set(descriptors_by_feature.values())))
            )
            for key, descriptor in descriptors_by_feature.items():
                count = raw_counts.get(descriptor)
                if count is not None:
                    goodware_counts[key] = count

        features_count = len(feature_keys)
        limits = SimilarityLimits(
            selected_samples=len(selected),
            corpus_samples=len(analysis_hashes),
            features=features_count,
            packs_missing=tuple(missing),
            corpus_sample_limit_applied=corpus_limit_applied,
            feature_limit_applied=feature_limit_applied,
            per_sample_feature_limit_applied=tuple(per_sample_limits),
            feature_losses=tuple(
                FeatureLoss(cause=cause, count=count)
                for cause, count in sorted(feature_loss_counts.items())
            ),
        )
        comparison = build_family_feature_comparison(
            snapshot=snapshot,
            comparison_sample_sha256s=selected,
            sample_inputs=sample_inputs,
            goodware_occurrences=goodware_counts,
            policy=self._policy,
            limits=limits,
        )
        payload = comparison.canonical_bytes
        if hashlib.sha256(payload).hexdigest() != comparison.sha256:
            raise SampleSimilarityError("canonical comparison hash changed during serialization")
        blob = await self._artifacts.ingest(
            BytesIO(payload),
            logical_bucket=_SIMILARITY_BUCKET,
            mime_type=_JSON_MIME_TYPE,
        )
        blob_descriptor = blob.descriptor
        if blob_descriptor.sha256 != comparison.sha256 or blob_descriptor.size != len(payload):
            raise SampleSimilarityError(
                "artifact store returned a mismatched comparison descriptor"
            )
        return StoredFamilyFeatureComparison(
            comparison=comparison,
            artifact_blob_id=blob.id,
            artifact_sha256=blob_descriptor.sha256,
            artifact_size=blob_descriptor.size,
        )

    async def _prepare_goodware(
        self, snapshot: CorpusSnapshot
    ) -> tuple[CorpusSnapshot, PreparedGoodwarePrevalencePort | None]:
        baseline_id = snapshot.goodware_baseline_id
        if baseline_id is None or self._goodware is None:
            return snapshot, None
        prepared = await self._goodware.prepare(baseline_id)
        actual_fingerprint = prepared.baseline_fingerprint_sha256
        actual_index_sha256 = getattr(prepared.index_descriptor, "sha256", None)
        if (
            not isinstance(actual_fingerprint, str)
            or _SHA256_RE.fullmatch(actual_fingerprint) is None
        ):
            raise SampleSimilarityError(
                "goodware provider returned an invalid baseline fingerprint"
            )
        if (
            not isinstance(actual_index_sha256, str)
            or _SHA256_RE.fullmatch(actual_index_sha256) is None
        ):
            raise SampleSimilarityError("goodware provider returned an invalid index SHA-256")
        if (
            snapshot.goodware_baseline_sha256 is not None
            and snapshot.goodware_baseline_sha256 != actual_fingerprint
        ):
            raise SampleSimilarityError(
                "goodware baseline changed since the corpus snapshot was frozen"
            )
        if (
            snapshot.goodware_index_sha256 is not None
            and snapshot.goodware_index_sha256 != actual_index_sha256
        ):
            raise SampleSimilarityError(
                "goodware index changed since the corpus snapshot was frozen"
            )
        return (
            replace(
                snapshot,
                goodware_baseline_sha256=actual_fingerprint,
                goodware_index_sha256=actual_index_sha256,
            ),
            prepared,
        )


def _canonical_sample_hashes(sample_sha256s: Sequence[str], maximum: int) -> tuple[str, ...]:
    if isinstance(sample_sha256s, (str, bytes)):
        raise ValueError("sample_sha256s must be a sequence of hashes")
    values = tuple(sample_sha256s)
    if not 1 <= len(values) <= maximum:
        raise ValueError(f"comparison requires between 1 and {maximum} exact samples")
    if any(not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None for value in values):
        raise ValueError("sample_sha256s must contain lowercase SHA-256 hashes")
    if len(set(values)) != len(values):
        raise ValueError("comparison sample hashes must be unique")
    return tuple(sorted(values))


def _extract_pack(
    pack: Mapping[str, Any],
    *,
    sample_sha256: str,
    policy: SimilarityPolicy,
) -> tuple[
    tuple[MeasuredFeature, ...],
    tuple[ExtractorVersion, ...],
    tuple[str, ...],
    bool,
    tuple[tuple[str, int], ...],
]:
    root = pack.get("features", pack)
    if not isinstance(root, Mapping):
        raise SampleSimilarityError("feature pack payload must be an object")
    features: list[MeasuredFeature] = []
    versions: list[ExtractorVersion] = []
    packer_markers: set[str] = set()
    feature_loss_counts: dict[str, int] = {}
    base_version = _text(pack.get("extractor_version", pack.get("tool_version"))) or "unknown"
    base_parameters = _valid_sha256(pack.get("parameters_sha256"))
    base_ruleset = _valid_sha256(pack.get("ruleset_sha256"))
    base_source_id = _source_id(pack)
    versions.append(
        ExtractorVersion(
            sample_sha256=sample_sha256,
            pack_kind="sample_features",
            version=base_version,
            parameters_sha256=base_parameters,
            ruleset_sha256=base_ruleset,
        )
    )
    base_source = FeatureSource(
        source_kind="sample_feature_pack",
        source_id=base_source_id,
        extractor_version=base_version,
        parameters_sha256=base_parameters,
        ruleset_sha256=base_ruleset,
    )

    def add_list(
        kind: SimilarityFeatureKind,
        values: Any,
        *,
        fields: tuple[str, ...] = ("value", "name", "pattern"),
        encoding: str | None = None,
        source_kind: str = "sample_feature_pack",
        source_id_override: str | None = None,
        source_version: str | None = None,
        parameters_sha256: str | None = None,
        ruleset_sha256: str | None = None,
    ) -> None:
        for item in _as_sequence(values):
            feature_value = _feature_text(item, fields)
            if feature_value is None:
                continue
            feature_encoding = _text(_mapping_value(item, "encoding")) or encoding
            normalized = _normalize_feature_value(kind, feature_value)
            if not normalized or len(normalized) > policy.maximum_feature_value_characters:
                if len(normalized) > policy.maximum_feature_value_characters:
                    _increment_loss(feature_loss_counts, "feature_value_too_long")
                continue
            category = _text(_mapping_value(item, "category", "noise_category"))
            offsets = _offset_value(_mapping_value(item, "offset", "start_offset"))
            function_offsets = _offset_values(
                _mapping_value(item, "function_offsets", "function_addresses", "function_offset")
            )
            item_source_id = _source_id(item) or source_id_override or base_source_id
            item_parameters_sha256 = (
                _valid_sha256(_mapping_value(item, "parameters_sha256"))
                or parameters_sha256
                or base_parameters
            )
            item_ruleset_sha256 = (
                _valid_sha256(_mapping_value(item, "ruleset_sha256"))
                or ruleset_sha256
                or base_ruleset
            )
            locations = function_offsets or (None,)
            for function_offset in locations:
                features.append(
                    MeasuredFeature(
                        kind=kind,
                        value=normalized,
                        encoding=feature_encoding,
                        category=category,
                        source=FeatureSource(
                            source_kind=source_kind,
                            source_id=item_source_id,
                            extractor_version=source_version or base_version,
                            parameters_sha256=item_parameters_sha256,
                            ruleset_sha256=item_ruleset_sha256,
                            offset=offsets,
                            function_offset=function_offset,
                        ),
                        fixed_byte_count=_nonnegative_int(_mapping_value(item, "fixed_byte_count")),
                    )
                )

    add_list(SimilarityFeatureKind.STRING, root.get("strings"), encoding=None)
    add_list(SimilarityFeatureKind.IMPORT, root.get("imports"), fields=("value", "name"))
    add_list(SimilarityFeatureKind.EXPORT, root.get("exports"), fields=("value", "name"))
    add_list(SimilarityFeatureKind.SECTION, root.get("sections"), fields=("name", "value"))
    add_list(
        SimilarityFeatureKind.API,
        root.get("apis", root.get("api_calls")),
        fields=("value", "name", "api"),
    )

    for metadata_kind, metadata_keys in (
        (SimilarityFeatureKind.PE_METADATA, ("pe_metadata", "pe")),
        (SimilarityFeatureKind.ELF_METADATA, ("elf_metadata", "elf")),
    ):
        for metadata_key in metadata_keys:
            metadata = root.get(metadata_key)
            if isinstance(metadata, Mapping):
                stable = {
                    name: value
                    for name, value in metadata.items()
                    if str(name).casefold()
                    in {
                        "format",
                        "machine",
                        "architecture",
                        "subsystem",
                        "type",
                        "class",
                        "endianness",
                    }
                }
                add_list(metadata_kind, tuple(stable.values()), fields=("value", "name"))
        if metadata_kind is SimilarityFeatureKind.PE_METADATA and root.get("format") in {
            "PE",
            "pe",
        }:
            add_list(metadata_kind, ("PE",), fields=("value",))
        if metadata_kind is SimilarityFeatureKind.ELF_METADATA and root.get("format") in {
            "ELF",
            "elf",
        }:
            add_list(metadata_kind, ("ELF",), fields=("value",))

    capability_pack = root.get(
        "capabilities",
        root.get("capa", pack.get("capabilities", pack.get("capa"))),
    )
    if isinstance(capability_pack, Mapping):
        capa_version = _text(capability_pack.get("version", capability_pack.get("tool_version")))
        capa_ruleset = _valid_sha256(capability_pack.get("ruleset_sha256"))
        capability_values = capability_pack.get("capabilities", capability_pack.get("rules", ()))
    else:
        capa_version = None
        capa_ruleset = None
        capability_values = capability_pack
    if capability_values is not None:
        version = capa_version or base_version
        if capa_version or capa_ruleset:
            versions.append(
                ExtractorVersion(
                    sample_sha256=sample_sha256,
                    pack_kind="capability",
                    version=version,
                    ruleset_sha256=capa_ruleset,
                )
            )
        add_list(
            SimilarityFeatureKind.CAPABILITY,
            capability_values,
            fields=("rule_id", "name", "value"),
            source_kind="capability_pack",
            source_id_override=_source_id(capability_pack),
            source_version=version,
            ruleset_sha256=capa_ruleset,
        )

    code_packs = _pack_sequence(
        root.get(
            "code_feature_sets",
            root.get(
                "code_features",
                root.get(
                    "code",
                    pack.get("code_feature_sets", pack.get("code_features", pack.get("code"))),
                ),
            ),
        )
    )
    if root.get("ngrams") is not None:
        code_packs = (*code_packs, root)
    for code_pack in code_packs:
        code_payload = _payload_mapping(code_pack)
        version = (
            _text(code_payload.get("tool_version", code_payload.get("smda_version")))
            or base_version
        )
        escaper = _text(code_payload.get("escaper_compatibility_version"))
        pic_hash = _text(code_payload.get("intel_pic_hash_escape_version"))
        if escaper or pic_hash or version != base_version:
            versions.append(
                ExtractorVersion(
                    sample_sha256=sample_sha256,
                    pack_kind="smda",
                    version="/".join(item for item in (version, escaper, pic_hash) if item),
                    parameters_sha256=_valid_sha256(code_payload.get("parameters_sha256"))
                    or base_parameters,
                )
            )
        packing = code_payload.get("packing")
        if isinstance(packing, Mapping):
            packer_markers.update(
                str(value)
                for value in _as_sequence(packing.get("known_packer_marker_hits"))
                if isinstance(value, str) and value
            )
        add_list(
            SimilarityFeatureKind.CODE_NGRAM,
            code_payload.get("ngrams"),
            fields=("pattern", "value"),
            source_kind="code_feature_set",
            source_id_override=_source_id(code_pack),
            source_version=version,
            parameters_sha256=_valid_sha256(code_payload.get("parameters_sha256")),
        )

    for family_kind, keys in (
        (SimilarityFeatureKind.CONFIGURATION, ("configuration", "config", "mwcp")),
        (SimilarityFeatureKind.BEHAVIOR, ("behavior", "behaviors", "behavioral_artifacts")),
        (
            SimilarityFeatureKind.NETWORK,
            ("network", "network_artifacts", "domains", "ip_addresses"),
        ),
    ):
        for name in keys:
            value = root.get(name, pack.get(name))
            if value is not None:
                family_values: list[str] = []
                for item in _as_sequence(value):
                    text = _family_trait_text(item, feature_loss_counts)
                    if text is not None:
                        family_values.append(text)
                add_list(
                    family_kind,
                    family_values,
                    fields=("value",),
                    source_kind=name,
                    source_version=base_version,
                )

    for hash_name in ("sha256", "md5", "sha1"):
        hash_value = _text(root.get(hash_name))
        if hash_value:
            features.append(
                MeasuredFeature(
                    kind=SimilarityFeatureKind.EXACT_HASH,
                    value=f"{hash_name}:{hash_value.lower()}",
                    source=replace(base_source, source_kind="exact_hash"),
                )
            )
    imphash = _text(root.get("imphash"))
    if imphash:
        features.append(
            MeasuredFeature(
                kind=SimilarityFeatureKind.IMPHASH,
                value=imphash.lower(),
                source=replace(base_source, source_kind="imphash"),
            )
        )
    tlsh = _text(root.get("tlsh"))
    if tlsh:
        features.append(
            MeasuredFeature(
                kind=SimilarityFeatureKind.TLSH_NEIGHBOR,
                value=tlsh.lower(),
                source=replace(base_source, source_kind="tlsh"),
            )
        )
    known_markers = root.get("known_packer_marker_hits")
    packer_markers.update(
        str(value) for value in _as_sequence(known_markers) if isinstance(value, str) and value
    )
    packing = root.get("packing")
    if isinstance(packing, Mapping):
        packer_markers.update(
            str(value)
            for value in _as_sequence(packing.get("known_packer_marker_hits"))
            if isinstance(value, str) and value
        )

    ordered = tuple(sorted(set(features), key=_measured_sort_key))
    was_limited = len(ordered) > policy.maximum_features_per_sample
    if was_limited:
        _increment_loss(
            feature_loss_counts,
            "maximum_features_per_sample",
            len(ordered) - policy.maximum_features_per_sample,
        )
    return (
        ordered[: policy.maximum_features_per_sample],
        tuple(
            sorted(
                set(versions),
                key=lambda item: (item.pack_kind, item.version, item.parameters_sha256 or ""),
            )
        ),
        tuple(sorted(packer_markers)),
        was_limited,
        tuple(sorted(feature_loss_counts.items())),
    )


def _goodware_descriptors(features: Iterable[MeasuredFeature]) -> dict[str, tuple[str, str]]:
    descriptors: dict[str, tuple[str, str]] = {}
    supported = set(SUPPORTED_FEATURE_KINDS)
    for item in features:
        descriptor: tuple[str, str] | None = None
        if item.kind is SimilarityFeatureKind.STRING and "string" in supported:
            descriptor = ("string", item.value.casefold())
        elif item.kind is SimilarityFeatureKind.EXPORT and "export" in supported:
            descriptor = ("export", item.value.casefold())
        elif item.kind is SimilarityFeatureKind.IMPHASH and "imphash" in supported:
            descriptor = ("imphash", item.value.casefold())
        elif item.kind is SimilarityFeatureKind.CODE_NGRAM and "opcode_fragment16" in supported:
            tokens = item.value.split()
            fixed = all(token not in {"?", "??", "*"} for token in tokens)
            if fixed and 8 <= len(tokens) <= 16:
                descriptor = ("opcode_fragment16", "".join(tokens).lower())
        if descriptor is not None:
            descriptors[item.key] = descriptor
    return descriptors


def _selected_feature_keys(
    feature_sets: Mapping[str, tuple[MeasuredFeature, ...]], maximum: int
) -> set[str]:
    support: dict[str, int] = {}
    representatives: dict[str, MeasuredFeature] = {}
    for features in feature_sets.values():
        per_sample: set[str] = set()
        for item in features:
            representatives.setdefault(item.key, item)
            per_sample.add(item.key)
        for key in per_sample:
            support[key] = support.get(key, 0) + 1
    ordered = sorted(
        representatives,
        key=lambda key: (
            -support[key],
            representatives[key].kind.value,
            representatives[key].value,
            representatives[key].encoding or "",
            key,
        ),
    )
    return set(ordered[:maximum])


def _pack_sequence(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        return (value,)
    return tuple(item for item in _as_sequence(value) if isinstance(item, Mapping))


def _payload_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = value.get("payload")
    return payload if isinstance(payload, Mapping) else value


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value)
    return ()


def _feature_text(value: Any, fields: tuple[str, ...]) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, Mapping):
        return None
    selected = next((value.get(name) for name in fields if isinstance(value.get(name), str)), None)
    return selected.strip() or None if isinstance(selected, str) else None


def _family_trait_text(value: Any, loss_counts: dict[str, int]) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    normalized = _stable_feature_json(value, loss_counts=loss_counts)
    if normalized is None:
        return None
    try:
        return json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return None


def _stable_feature_json(
    value: Any, *, depth: int = 0, loss_counts: dict[str, int] | None = None
) -> Any:
    if depth > 6:
        _increment_loss(loss_counts, "object_depth_limit")
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value == value and abs(value) != float("inf"):
            return value
        _increment_loss(loss_counts, "object_invalid_float")
        return None
    if isinstance(value, Mapping):
        ignored = {"address", "addresses", "offset", "offsets", "timestamp", "time_seen"}
        result: dict[str, Any] = {}
        ordered_keys = sorted(value, key=str)
        if len(ordered_keys) > 64:
            _increment_loss(loss_counts, "object_field_limit", len(ordered_keys) - 64)
        for key in ordered_keys[:64]:
            name = str(key)
            if name.casefold() in ignored:
                continue
            child = _stable_feature_json(value[key], depth=depth + 1, loss_counts=loss_counts)
            if child is not None:
                result[name] = child
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) > 64:
            _increment_loss(loss_counts, "object_sequence_limit", len(value) - 64)
        return [
            child
            for item in value[:64]
            if (child := _stable_feature_json(item, depth=depth + 1, loss_counts=loss_counts))
            is not None
        ]
    _increment_loss(loss_counts, "object_unsupported_value")
    return None


def _increment_loss(loss_counts: dict[str, int] | None, cause: str, count: int = 1) -> None:
    if loss_counts is None or count <= 0:
        return
    loss_counts[cause] = loss_counts.get(cause, 0) + count


def _normalize_feature_value(kind: SimilarityFeatureKind, value: str) -> str:
    normalized = value.strip()
    if kind in {
        SimilarityFeatureKind.IMPORT,
        SimilarityFeatureKind.EXPORT,
        SimilarityFeatureKind.SECTION,
        SimilarityFeatureKind.API,
        SimilarityFeatureKind.CAPABILITY,
        SimilarityFeatureKind.PE_METADATA,
        SimilarityFeatureKind.ELF_METADATA,
    }:
        return normalized.casefold()
    return normalized


def _mapping_value(value: Any, *keys: str) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in keys:
        if key in value:
            return value[key]
    return None


def _offset_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value, 0)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _offset_values(value: Any) -> tuple[int | None, ...]:
    if isinstance(value, (int, str)):
        return (_offset_value(value),) if _offset_value(value) is not None else ()
    return tuple(
        offset for item in _as_sequence(value) if (offset := _offset_value(item)) is not None
    )


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _valid_sha256(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA256_RE.fullmatch(value) else None


def _source_id(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    selected = next(
        (
            value.get(name)
            for name in ("feature_blob_sha256", "feature_blob_id", "feature_id", "blob_id", "id")
            if value.get(name) is not None
        ),
        None,
    )
    text = str(selected) if selected is not None else None
    return text[:256] if text else None


def _measured_sort_key(item: MeasuredFeature) -> tuple[str, ...]:
    return (
        item.kind.value,
        item.value,
        item.encoding or "",
        item.category or "",
        item.source.source_kind,
        item.source.source_id or "",
        str(item.source.offset if item.source.offset is not None else -1),
        str(item.source.function_offset if item.source.function_offset is not None else -1),
    )


__all__ = [
    "CorpusSnapshotPort",
    "GoodwarePrevalencePort",
    "SampleSimilarityError",
    "SampleSimilarityService",
    "SimilarityArtifactStorePort",
    "StoredFamilyFeatureComparison",
]
