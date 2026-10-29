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
    EvidenceKind,
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
    GoodwarePrevalenceStatus,
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
    def __init__(
        self,
        packs: Mapping[str, Mapping[str, object]],
        acquisition_status: Mapping[str, bool | None] | None = None,
    ) -> None:
        self.packs = packs
        self.acquisition_status = acquisition_status or {}

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, object] | None:
        return self.packs.get(sample_sha256)

    async def is_sample_acquired(self, sample_sha256: str) -> bool | None:
        return self.acquisition_status.get(sample_sha256)


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


def _service(
    packs: Mapping[str, Mapping[str, object]],
    *,
    acquisition_status: Mapping[str, bool | None] | None = None,
) -> SampleSimilarityService:
    return SampleSimilarityService(
        feature_packs=_FeaturePackReader(packs, acquisition_status),  # type: ignore[arg-type]
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
    assert function.goodware_occurrence_count is None
    assert function.to_json()["support"]["goodware_prevalence_status"] == "unmeasured"
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
    masked_candidate = next(
        candidate
        for candidate in function.signature_candidates
        if candidate.motif_kind is SignatureMotifKind.HEX
    )
    assert masked_candidate.qualified is False
    assert masked_candidate.goodware_prevalence_status is GoodwarePrevalenceStatus.UNMEASURED
    assert masked_candidate.goodware_occurrence_count is None
    assert masked_candidate.to_json()["goodware_prevalence_status"] == "unmeasured"
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
    assert any(
        "RuleWorkbench/goodware" in item and "L3" in item
        for item in comparison.recommended_next_tests
    )


@pytest.mark.asyncio
async def test_missing_selected_pack_keeps_absence_unknown_and_hash_deterministic() -> None:
    packs = _fixture_packs()
    del packs[_SAMPLE_C]
    service = _service(packs)

    first = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_A, _SAMPLE_B, _SAMPLE_C),
    )
    second = await service.compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_A, _SAMPLE_B, _SAMPLE_C),
    )

    function = first.comparison.feature(_FUNCTION_KEY)
    assert function is not None
    assert function.feature_class is FamilyFeatureClass.OPTIONAL
    missing_presence = next(
        item for item in function.sample_presence if item.sample_sha256 == _SAMPLE_C
    )
    assert missing_presence.present is False
    assert missing_presence.measured is False
    limits = first.comparison.limits.to_json()
    assert _SAMPLE_C in limits["packs_missing"]
    assert first.comparison.status is SimilarityStatus.PARTIAL_FEATURE_COVERAGE
    assert first.comparison.sha256 == second.comparison.sha256
    assert first.artifact_sha256 == second.artifact_sha256


@pytest.mark.asyncio
async def test_feature_value_and_object_truncations_are_counted_with_causes() -> None:
    packs = _fixture_packs()
    sample_pack = dict(packs[_SAMPLE_A])
    raw_features = sample_pack["features"]
    assert isinstance(raw_features, dict)
    strings = raw_features["strings"]
    assert isinstance(strings, list)
    raw_features["strings"] = [*strings, "x" * 2_100]

    nested: dict[str, object] = {"leaf": "kept"}
    for index in range(8):
        nested = {f"level_{index}": nested}
    raw_features["configuration"] = [
        {
            "deep": nested,
            "fields": {f"field_{index:02d}": "value" for index in range(70)},
        }
    ]
    sample_pack["features"] = raw_features
    packs[_SAMPLE_A] = sample_pack

    stored = await _service(packs).compare(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256s=(_SAMPLE_A, _SAMPLE_B),
    )
    losses = {
        item["cause"]: item["count"]
        for item in stored.comparison.limits.to_json()["feature_losses"]
    }
    assert losses["feature_value_too_long"] == 1
    assert losses["object_depth_limit"] >= 1
    assert losses["object_field_limit"] == 6
    assert stored.comparison.status is SimilarityStatus.PARTIAL_FEATURE_COVERAGE


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
        self.json_calls: list[dict[str, Any]] = []
        self.observation_id = UUID("c83b70e4-adba-4cfd-9c53-6d507754f467")
        self.json_observation_id = UUID("6bb1c6e9-e0f2-4be2-9cd0-01a338a7549d")

    async def record_existing_blob(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(id=self.observation_id)

    async def record_json(self, **kwargs: Any) -> SimpleNamespace:
        self.json_calls.append(kwargs)
        return SimpleNamespace(id=self.json_observation_id)


class _NoInvestigationAssociations:
    async def list_for_investigation(self, _investigation_id: UUID) -> tuple[()]:
        return ()


class _SimilarityMustNotRun:
    async def compare(self, **_kwargs: Any) -> None:
        raise AssertionError("unassociated samples must be rejected before comparison")


@pytest.mark.asyncio
async def test_unassociated_hashes_return_persisted_unavailable_for_both_tools() -> None:
    recorder = _EvidenceRecorder()
    registry = InMemoryToolRegistry()
    register_similarity_tools(
        registry,
        service=cast(SampleSimilarityService, _SimilarityMustNotRun()),
        sample_associations=cast(SampleAssociationRepository, _NoInvestigationAssociations()),
        recorder=cast(EvidenceRecorder, recorder),
    )

    for index, tool_name in enumerate(("samples.compare", "features.common"), start=1):
        registration = registry.get(tool_name, "1")
        assert registration is not None
        _definition, adapter = registration
        result = await adapter.execute(
            {"sample_sha256s": [_SAMPLE_A]},
            ToolExecutionContext(
                investigation_id=_INVESTIGATION_ID,
                envelope=None,
                budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30}),
                idempotency_key=f"unassociated-similarity-{index}",
                available_capabilities=frozenset(SIMILARITY_TOOL_NAMES),
            ),
        )

        assert result.status is ToolInvocationStatus.UNAVAILABLE
        assert result.budget_consumed == {BudgetDimension.TOOL_SECONDS: 0}
        assert result.observation_refs == (recorder.json_observation_id,)
        assert result.output["schema_version"] == "investigation-similarity-tool-output-v1"
        availability = result.output["availability"]
        assert availability["state"] == "unavailable"
        assert availability["interpretation"] == "not_a_negative_result"
        reason = availability["reasons_by_sample_sha256"][_SAMPLE_A]
        assert reason["reason"] == "not_associated_to_investigation"
        assert reason["next_action"]["code"] == "verify_investigation_association"

    assert recorder.calls == []
    assert len(recorder.json_calls) == 2
    assert all(call["kind"] is EvidenceKind.OTHER for call in recorder.json_calls)
    assert all("not a negative result" in call["limitations"][0] for call in recorder.json_calls)
    for call in recorder.json_calls:
        payload = call["payload"]
        assert payload["schema"] == "investigation-tool-observation.v1"
        assert payload["result"]["availability"]["interpretation"] == "not_a_negative_result"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acquired", "expected_reason", "expected_action"),
    (
        (False, "sample_not_acquired", "acquire_sample_if_authorized"),
        (True, "feature_pack_missing", "verify_acquisition_or_extract_features"),
    ),
)
async def test_missing_selected_pack_is_partial_with_hash_reason_and_next_action(
    acquired: bool,
    expected_reason: str,
    expected_action: str,
) -> None:
    packs = _fixture_packs()
    del packs[_SAMPLE_C]
    recorder = _EvidenceRecorder()
    registry = InMemoryToolRegistry()
    register_similarity_tools(
        registry,
        service=_service(packs, acquisition_status={_SAMPLE_C: acquired}),
        sample_associations=cast(SampleAssociationRepository, _AssociationReader()),
        recorder=cast(EvidenceRecorder, recorder),
    )
    registration = registry.get("features.common", "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute(
        {"sample_sha256s": [_SAMPLE_A, _SAMPLE_B, _SAMPLE_C]},
        ToolExecutionContext(
            investigation_id=_INVESTIGATION_ID,
            envelope=None,
            budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30}),
            idempotency_key="missing-feature-pack",
            available_capabilities=frozenset(SIMILARITY_TOOL_NAMES),
        ),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["schema_version"] == "investigation-similarity-tool-output-v1"
    availability = result.output["availability"]
    assert availability["state"] == "partial"
    assert availability["interpretation"] == "not_a_negative_result"
    assert "empty" not in result.output
    reason = availability["reasons_by_sample_sha256"][_SAMPLE_C]
    assert reason["reason"] == expected_reason
    assert reason["next_action"]["code"] == expected_action
    assert result.observation_refs == (
        recorder.observation_id,
        recorder.json_observation_id,
    )
    assert len(recorder.calls) == len(recorder.json_calls) == 1
    assert BudgetDimension.VT_UNITS not in result.budget_consumed
    assert BudgetDimension.DOWNLOADS not in result.budget_consumed


@pytest.mark.asyncio
async def test_all_unacquired_selected_samples_are_unavailable_not_empty() -> None:
    recorder = _EvidenceRecorder()
    registry = InMemoryToolRegistry()
    register_similarity_tools(
        registry,
        service=_service(
            {},
            acquisition_status={
                _SAMPLE_A: False,
                _SAMPLE_B: False,
                _SAMPLE_C: False,
            },
        ),
        sample_associations=cast(SampleAssociationRepository, _AssociationReader()),
        recorder=cast(EvidenceRecorder, recorder),
    )
    registration = registry.get("samples.compare", "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute(
        {"sample_sha256s": [_SAMPLE_A, _SAMPLE_B, _SAMPLE_C]},
        ToolExecutionContext(
            investigation_id=_INVESTIGATION_ID,
            envelope=None,
            budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30}),
            idempotency_key="all-samples-unacquired",
            available_capabilities=frozenset(SIMILARITY_TOOL_NAMES),
        ),
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["availability"]["state"] == "unavailable"
    assert result.output["availability"]["interpretation"] == "not_a_negative_result"
    assert "empty" not in result.output
    reasons = result.output["availability"]["reasons_by_sample_sha256"]
    for sample_hash in (_SAMPLE_A, _SAMPLE_B, _SAMPLE_C):
        assert reasons[sample_hash]["reason"] == "sample_not_acquired"
        assert reasons[sample_hash]["next_action"]["code"] == "acquire_sample_if_authorized"
    assert BudgetDimension.VT_UNITS not in result.budget_consumed
    assert BudgetDimension.DOWNLOADS not in result.budget_consumed


@pytest.mark.asyncio
async def test_invalid_acquired_feature_pack_remains_a_real_failure() -> None:
    packs = _fixture_packs()
    packs[_SAMPLE_A] = {"sample_sha256": _SAMPLE_A, "features": []}
    recorder = _EvidenceRecorder()
    registry = InMemoryToolRegistry()
    register_similarity_tools(
        registry,
        service=_service(packs),
        sample_associations=cast(SampleAssociationRepository, _AssociationReader()),
        recorder=cast(EvidenceRecorder, recorder),
    )
    registration = registry.get("samples.compare", "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute(
        {"sample_sha256s": [_SAMPLE_A, _SAMPLE_B]},
        ToolExecutionContext(
            investigation_id=_INVESTIGATION_ID,
            envelope=None,
            budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30}),
            idempotency_key="invalid-feature-pack",
            available_capabilities=frozenset(SIMILARITY_TOOL_NAMES),
        ),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.message == "similarity_comparison_failed:SampleSimilarityError"
    assert result.output == {}
    assert recorder.calls == []
    assert recorder.json_calls == []


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
