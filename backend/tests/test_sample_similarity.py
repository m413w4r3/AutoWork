from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid5

import pytest

from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.investigation_persistence import SampleAssociationRepository
from cti_app.application.investigation_similarity_tools import (
    SIMILARITY_TOOL_NAMES,
    register_similarity_tools,
    similarity_tool_capabilities,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolEffect,
    ToolExecutionContext,
)
from cti_app.application.sample_similarity import SampleSimilarityService
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.investigation import (
    BudgetDimension,
    InvestigationBudget,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.sample_similarity import (
    CorpusMember,
    CorpusSnapshot,
    FamilyFeatureClass,
    FeatureRejectionReason,
    SignatureMotifKind,
    SimilarityFeatureKind,
    SimilarityPolicy,
    SimilarityStatus,
    feature_key,
)

_INVESTIGATION_ID = UUID("e38d071b-e244-4edb-9167-b2beca9294b9")
_GOODWARE_BASELINE_ID = UUID("ca84cb10-6ff6-4d03-9830-951dd9558cbf")
_GOODWARE_FINGERPRINT = "a" * 64
_GOODWARE_INDEX_SHA256 = "b" * 64
_CODE_PATTERN = "55 48 89 ?? 48 83 ec 20 48 89 f8"
_TRIVIAL_PATTERN = "90 90 90 90 90 90 90 90 90 90"
_FUNCTION_KEY = feature_key(SimilarityFeatureKind.CODE_NGRAM, _CODE_PATTERN)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_SAMPLE_A = _sha("sample-a")
_SAMPLE_B = _sha("sample-b")
_SAMPLE_C = _sha("sample-c")
_GOODWARE = _sha("goodware-counterexample")


def _pack(
    sample_sha256: str,
    *,
    function_offset: int | None = None,
    start_offset: int | None = None,
    include_trivial: bool = False,
    include_family_string: bool = False,
) -> dict[str, object]:
    ngrams: list[dict[str, object]] = []
    if function_offset is not None and start_offset is not None:
        ngrams.append(
            {
                "pattern": _CODE_PATTERN,
                "function_offset": function_offset,
                "start_offset": start_offset,
                "fixed_byte_count": 9,
                "instruction_count": 5,
            }
        )
    if include_trivial:
        ngrams.append(
            {
                "pattern": _TRIVIAL_PATTERN,
                "function_offset": 0x5000,
                "start_offset": 0x5010,
                "fixed_byte_count": 10,
                "instruction_count": 10,
            }
        )
    strings = [
        {
            "value": "This program cannot be run in DOS mode",
            "encoding": "ascii",
            "offset": 0x80,
        }
    ]
    if include_family_string:
        strings.append({"value": "LoaderHandshakeAlpha", "encoding": "ascii", "offset": 0x330})
        strings.append({"value": "WideEvidenceMarker", "encoding": "utf-16le", "offset": 0x350})
    return {
        "sample_sha256": sample_sha256,
        "extractor_version": "static-v1",
        "parameters_sha256": "c" * 64,
        "features": {
            "sha256": sample_sha256,
            "format": "PE",
            "strings": strings,
            "code_features": {
                "feature_blob_sha256": "e" * 64,
                "tool_version": "4.5.0",
                "escaper_compatibility_version": "4.4.5",
                "intel_pic_hash_escape_version": "4.3.5",
                "parameters_sha256": "d" * 64,
                "status": "SUCCEEDED",
                "ngrams": ngrams,
                "packing": {"known_packer_marker_hits": []},
            },
        },
    }


def _corpus_snapshot() -> CorpusSnapshot:
    return CorpusSnapshot(
        investigation_id=_INVESTIGATION_ID,
        members=(
            CorpusMember(sample_sha256=_SAMPLE_A, qualification="confirmed", origin="seed"),
            CorpusMember(sample_sha256=_SAMPLE_B, qualification="candidate", origin="vt_pivot"),
            CorpusMember(sample_sha256=_SAMPLE_C, qualification="candidate", origin="relationship"),
            CorpusMember(sample_sha256=_GOODWARE, qualification="rejected", origin="manual"),
        ),
        goodware_baseline_id=_GOODWARE_BASELINE_ID,
        goodware_baseline_sha256=_GOODWARE_FINGERPRINT,
        goodware_index_sha256=_GOODWARE_INDEX_SHA256,
    )


class _FeaturePackReader:
    def __init__(self, packs: Mapping[str, Mapping[str, object]]) -> None:
        self.packs = packs

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, object] | None:
        return self.packs.get(sample_sha256)


class _CorpusSnapshotReader:
    async def get_snapshot(self, investigation_id: UUID) -> CorpusSnapshot:
        assert investigation_id == _INVESTIGATION_ID
        return _corpus_snapshot()


class _PreparedGoodware:
    def __init__(self) -> None:
        self.baseline_fingerprint_sha256 = _GOODWARE_FINGERPRINT
        self.index_descriptor = BlobDescriptor(
            sha256=_GOODWARE_INDEX_SHA256,
            size=1,
            mime_type="application/vnd.sqlite3",
            logical_bucket="goodware-indexes",
        )

    async def lookup_batch(
        self, features: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], int]:
        return {
            feature: (
                10_000 if feature == ("string", "this program cannot be run in dos mode") else 0
            )
            for feature in features
        }


class _GoodwareReader:
    async def prepare(self, baseline_ref: UUID | str) -> _PreparedGoodware:
        assert baseline_ref == _GOODWARE_BASELINE_ID
        return _PreparedGoodware()


class _ArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}

    async def ingest(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobRecord:
        payload = source.read()
        sha256 = hashlib.sha256(payload).hexdigest()
        self.payloads[sha256] = payload
        return BlobRecord(
            descriptor=BlobDescriptor(
                sha256=sha256,
                size=len(payload),
                mime_type=mime_type,
                logical_bucket=logical_bucket,
            ),
            id=uuid5(UUID("99693bc4-a983-49c2-8998-617e4e771d31"), sha256),
        )


def _service(packs: Mapping[str, Mapping[str, object]]) -> SampleSimilarityService:
    return SampleSimilarityService(
        feature_packs=_FeaturePackReader(packs),  # type: ignore[arg-type]
        corpus_snapshots=_CorpusSnapshotReader(),
        artifacts=_ArtifactStore(),
        goodware=_GoodwareReader(),  # type: ignore[arg-type]
        policy=SimilarityPolicy(),
    )


def _fixture_packs(*, include_trivial: bool = False) -> dict[str, Mapping[str, object]]:
    return {
        _SAMPLE_A: _pack(
            _SAMPLE_A,
            function_offset=0x1000,
            start_offset=0x1010,
            include_trivial=include_trivial,
            include_family_string=True,
        ),
        _SAMPLE_B: _pack(
            _SAMPLE_B,
            function_offset=0x2000,
            start_offset=0x2024,
            include_trivial=include_trivial,
            include_family_string=True,
        ),
        _SAMPLE_C: _pack(_SAMPLE_C),
        _GOODWARE: _pack(_GOODWARE),
    }


@pytest.mark.asyncio
async def test_masked_function_support_offsets_and_goodware_rejection_are_traced() -> None:
    service = _service(_fixture_packs())

    stored = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_C, _SAMPLE_B, _SAMPLE_A),
    )

    comparison = stored.comparison
    assert comparison.status is SimilarityStatus.COMPLETE
    assert comparison.comparison_sample_sha256s == tuple(sorted((_SAMPLE_A, _SAMPLE_B, _SAMPLE_C)))
    assert comparison.corpus_sample_sha256s == tuple(
        sorted((_SAMPLE_A, _SAMPLE_B, _SAMPLE_C, _GOODWARE))
    )
    function = comparison.feature(_FUNCTION_KEY)
    assert function is not None
    assert function.kind is SimilarityFeatureKind.CODE_NGRAM
    assert function.feature_class is FamilyFeatureClass.SUBFAMILY_DISCRIMINANT
    assert function.positive_support == 2
    assert function.positive_sample_count == 3
    assert function.comparison_exclusive is True
    positive_locations = {
        item.sample_sha256: (item.offsets, item.function_offsets)
        for item in function.sample_presence
        if item.present and item.selected
    }
    assert positive_locations[_SAMPLE_A] == ((0x1010,), (0x1000,))
    assert positive_locations[_SAMPLE_B] == ((0x2024,), (0x2000,))
    third_variant = next(
        item for item in function.sample_presence if item.sample_sha256 == _SAMPLE_C
    )
    assert third_variant.measured is True
    assert third_variant.present is False
    source = next(
        source
        for item in function.sample_presence
        if item.sample_sha256 == _SAMPLE_A
        for source in item.sources
    )
    assert source.source_kind == "code_feature_set"
    assert source.source_id == "e" * 64
    assert "??" in function.value
    assert any(
        candidate.motif_kind is SignatureMotifKind.HEX
        for candidate in function.signature_candidates
    )
    goodware_presence = next(
        item for item in function.sample_presence if item.sample_sha256 == _GOODWARE
    )
    assert goodware_presence.measured is True
    assert goodware_presence.present is False

    boilerplate = next(
        item
        for item in comparison.features
        if item.value == "This program cannot be run in DOS mode"
    )
    assert boilerplate.feature_class is FamilyFeatureClass.LIBRARY_OR_PACKER_CONTAMINATION
    assert boilerplate.goodware_occurrence_count == 10_000
    assert FeatureRejectionReason.HIGH_GOODWARE_PREVALENCE in boilerplate.rejection_reasons
    assert FeatureRejectionReason.BOILERPLATE_OR_PADDING in boilerplate.rejection_reasons
    assert boilerplate.signature_candidates == ()
    ascii_feature = next(
        item for item in comparison.features if item.value == "LoaderHandshakeAlpha"
    )
    assert any(
        candidate.motif_kind is SignatureMotifKind.ASCII
        for candidate in ascii_feature.signature_candidates
    )
    wide_feature = next(item for item in comparison.features if item.value == "WideEvidenceMarker")
    assert any(
        candidate.motif_kind is SignatureMotifKind.WIDE
        for candidate in wide_feature.signature_candidates
    )
    assert stored.artifact_sha256 == comparison.sha256


@pytest.mark.asyncio
async def test_single_sample_returns_unqualified_motifs_and_next_test() -> None:
    service = _service(_fixture_packs())

    stored = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_A,),
    )

    comparison = stored.comparison
    assert comparison.status is SimilarityStatus.INSUFFICIENT_COMPARATIVE_SUPPORT
    family_string = next(
        item for item in comparison.features if item.value == "LoaderHandshakeAlpha"
    )
    candidate = family_string.signature_candidates[0]
    assert candidate.qualified is False
    assert FeatureRejectionReason.INSUFFICIENT_COMPARATIVE_SUPPORT in candidate.rejection_reasons
    assert any("additional exact SHA-256" in item for item in comparison.recommended_next_tests)


@pytest.mark.asyncio
async def test_trivial_padding_is_explained_but_never_a_signature_candidate() -> None:
    service = _service(_fixture_packs(include_trivial=True))

    stored = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_A, _SAMPLE_B, _SAMPLE_C),
    )

    trivial = next(item for item in stored.comparison.features if item.value == _TRIVIAL_PATTERN)
    assert FeatureRejectionReason.BOILERPLATE_OR_PADDING in trivial.rejection_reasons
    assert trivial.signature_candidates == ()


@pytest.mark.asyncio
async def test_canonical_comparison_hash_is_deterministic_for_same_inputs() -> None:
    service = _service(_fixture_packs())

    first = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_A, _SAMPLE_B, _SAMPLE_C),
    )
    second = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_C, _SAMPLE_A, _SAMPLE_B),
    )

    assert first.comparison.sha256 == second.comparison.sha256
    assert first.artifact_sha256 == second.artifact_sha256
    assert first.comparison.canonical_bytes == second.comparison.canonical_bytes


class _AssociationReader:
    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        assert investigation_id == _INVESTIGATION_ID
        return (
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=_SAMPLE_A,
                origin=SampleOrigin.SEED,
                qualification=SampleQualification.CANDIDATE,
            ),
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=_SAMPLE_B,
                origin=SampleOrigin.VT_PIVOT,
                qualification=SampleQualification.CANDIDATE,
            ),
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=_SAMPLE_C,
                origin=SampleOrigin.RELATIONSHIP,
                qualification=SampleQualification.CANDIDATE,
            ),
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=_GOODWARE,
                origin=SampleOrigin.MANUAL,
                qualification=SampleQualification.REJECTED,
            ),
        )


class _EvidenceRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.observation_id = UUID("c83b70e4-adba-4cfd-9c53-6d507754f467")

    async def record_existing_blob(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(id=self.observation_id)


@pytest.mark.asyncio
async def test_similarity_tools_are_versioned_read_only_and_return_evidence_refs() -> None:
    service = _service(_fixture_packs())
    associations = _AssociationReader()
    recorder = _EvidenceRecorder()
    registry = InMemoryToolRegistry()
    register_similarity_tools(
        registry,
        service=service,
        sample_associations=cast(SampleAssociationRepository, associations),
        recorder=cast(EvidenceRecorder, recorder),
    )

    assert similarity_tool_capabilities(
        service=service,
        sample_associations=cast(SampleAssociationRepository, associations),
        recorder=cast(EvidenceRecorder, recorder),
    ) == frozenset(SIMILARITY_TOOL_NAMES)
    for name in SIMILARITY_TOOL_NAMES:
        registration = registry.get(name, "1")
        assert registration is not None
        definition, _adapter = registration
        assert definition.effect is ToolEffect.LOCAL_READ
        assert definition.input_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert definition.capability_key == name

    budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30})
    context = ToolExecutionContext(
        investigation_id=_INVESTIGATION_ID,
        envelope=None,
        budget=budget,
        idempotency_key="similarity-test-1",
        available_capabilities=frozenset(SIMILARITY_TOOL_NAMES),
    )
    tool_arguments = {
        "sample_sha256s": [_SAMPLE_A, _SAMPLE_B, _SAMPLE_C],
    }
    results: dict[str, Any] = {}
    for name in SIMILARITY_TOOL_NAMES:
        registration = registry.get(name, "1")
        assert registration is not None
        _definition, adapter = registration
        arguments: dict[str, Any] = dict(tool_arguments)
        if name == "features.common":
            arguments["limit"] = 4
        elif name == "signatures.candidates":
            arguments["limit"] = 4
        elif name == "features.explain":
            arguments["feature_key"] = _FUNCTION_KEY
        results[name] = await adapter.execute(arguments, context)

    for result in results.values():
        assert result.status is ToolInvocationStatus.SUCCEEDED
        assert result.observation_refs == (recorder.observation_id,)
        assert result.output["schema_version"] == "investigation-similarity-tool-output-v1"
        assert result.output["comparison_sha256"] == result.output["artifact_sha256"]
    assert results["features.common"].output["features"]
    assert results["signatures.candidates"].output["candidates"]
    assert results["features.explain"].output["feature"]["feature_key"] == _FUNCTION_KEY
    assert len(recorder.calls) == len(SIMILARITY_TOOL_NAMES)
    assert recorder.calls[0]["object_type"] == "family_feature_comparison"
    assert recorder.calls[0]["sha256"] == results["samples.compare"].output["comparison_sha256"]

    explain_registration = registry.get("features.explain", "1")
    assert explain_registration is not None
    _definition, explain_adapter = explain_registration
    missing_feature_result = await explain_adapter.execute(
        {
            "sample_sha256s": [_SAMPLE_A, _SAMPLE_B, _SAMPLE_C],
            "feature_key": "f" * 64,
        },
        ToolExecutionContext(
            investigation_id=_INVESTIGATION_ID,
            envelope=None,
            budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30}),
            idempotency_key="similarity-missing-feature",
            available_capabilities=frozenset(SIMILARITY_TOOL_NAMES),
        ),
    )
    assert missing_feature_result.status is ToolInvocationStatus.EMPTY
    assert missing_feature_result.output["feature"] is None
    assert missing_feature_result.output["empty"] is True
    assert missing_feature_result.output["reason"] == "feature_key_not_in_comparison"
    assert missing_feature_result.observation_refs == (recorder.observation_id,)
