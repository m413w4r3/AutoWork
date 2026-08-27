from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.invariants import InvariantRegistryService
from cti_app.config import Settings
from cti_app.domain.code_features import CodeNgram, GoodwareVerdict, PackingSignals
from cti_app.domain.goodware import Banality
from cti_app.domain.invariants import (
    AnalystManualProvenance,
    CandidateInvariant,
    CapabilityProvenance,
    CodeFeatureProvenance,
    FeatureMeasurements,
    InvariantCategory,
    InvariantRejectionCause,
    InvariantStatus,
    InvariantType,
    ReportClaimProvenance,
    ResolvedFeature,
    SampleFeatureProvenance,
    ToolOutputProvenance,
    likely_packed,
    make_proposal_key,
)

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
INVESTIGATION_ID = uuid4()
SAMPLE_ID = uuid4()


class FakeInvariantRepository:
    def __init__(self) -> None:
        self.resolved: ResolvedFeature | None = None
        self.measurements = FeatureMeasurements()
        self.invariants: dict[str, CandidateInvariant] = {}
        self.rejections = {}
        self.transitions = []

    async def lock_proposal(self, proposal_key: str) -> None:
        return None

    async def get_proposal_outcome(self, proposal_key: str):
        return self.invariants.get(proposal_key), self.rejections.get(proposal_key)

    async def resolve_provenance(self, **kwargs: object) -> ResolvedFeature | None:
        return self.resolved

    async def measure_feature(self, **kwargs: object) -> FeatureMeasurements:
        return self.measurements

    async def add_invariant(self, invariant: CandidateInvariant) -> CandidateInvariant:
        return self.invariants.setdefault(invariant.proposal_key, invariant)

    async def add_rejection(self, rejection):
        return self.rejections.setdefault(rejection.proposal_key, rejection)

    async def transition(self, **kwargs: object) -> CandidateInvariant:
        invariant = next(
            item for item in self.invariants.values() if item.id == kwargs["invariant_id"]
        )
        transition = invariant.status
        updated = CandidateInvariant(
            **{
                name: getattr(invariant, name)
                for name in invariant.__dataclass_fields__
                if name != "status"
            },
            status=kwargs["to_status"],
        )
        self.invariants[invariant.proposal_key] = updated
        self.transitions.append((transition, kwargs))
        return updated

    async def rejection_statistics(self, **kwargs: object) -> dict[str, int]:
        output: dict[str, int] = {}
        for rejection in self.rejections.values():
            output[rejection.cause.value] = output.get(rejection.cause.value, 0) + 1
        return output


class FakeGoodwareRepository:
    def __init__(self) -> None:
        self.occurrence: int | None = None

    async def get_feature_occurrence(self, *args: object) -> int | None:
        return self.occurrence


class FakeInvestigationBaselineRepository:
    async def get(self, investigation_id: UUID) -> UUID:
        return UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class FakeUow:
    def __init__(self) -> None:
        self.invariants = FakeInvariantRepository()
        self.goodware_baselines = FakeGoodwareRepository()
        self.investigation_goodware_baselines = FakeInvestigationBaselineRepository()
        self.analyst_investigations = SimpleNamespace(
            get=self._get_investigation,
        )
        self.commits = 0

    async def _get_investigation(self, investigation_id: UUID) -> SimpleNamespace:
        return SimpleNamespace(cycle_number=2)

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _service() -> tuple[InvariantRegistryService, FakeUow]:
    uow = FakeUow()
    return (
        InvariantRegistryService(
            lambda: uow,
            settings=Settings(
                goodware_suspicious_count=3,
                goodware_banal_count=10,
                invariant_max_pattern_chars=96,
                code_ngram_max_mask_ratio=0.20,
                code_ngram_min_contiguous_fixed_bytes=6,
            ),
        ),
        uow,
    )


def _manual(pattern: str = "CreateMutexW", moment: datetime = NOW) -> AnalystManualProvenance:
    return AnalystManualProvenance(actor_id="analyst-1", occurred_at=moment, motif=pattern)


def test_closed_taxonomies_are_exact() -> None:
    assert {item.value for item in InvariantType} == {
        "literal_string", "hex_pattern", "code_ngram", "opcode_sequence",
        "import_name", "export_name", "section_name", "capability",
        "similarity_hash", "structural_metadata", "relation",
    }
    assert {item.value for item in InvariantCategory} == {
        "c2_indicator", "mutex_or_event", "pdb_or_build_path", "config_marker",
        "crypto_constant", "custom_protocol", "ransom_or_ui_text", "code_sequence",
        "capability_pattern", "similarity_key", "library_noise", "packer_artifact",
        "compiler_artifact", "generic_winapi", "unknown",
    }


def test_provenances_require_their_own_fields() -> None:
    assert SampleFeatureProvenance(
        sample_sha256="a" * 64, feature_id="f", offsets=(1,)
    ).kind == "sample_feature"
    assert CodeFeatureProvenance(
        sample_sha256="a" * 64,
        function_address=0x1000,
        offset=2,
        disassembler_version="smda",
    ).kind == "code_feature"
    assert ToolOutputProvenance(
        sample_sha256="a" * 64, tool="capa", version="1", internal_id="row"
    ).kind == "tool_output"
    assert CapabilityProvenance(
        sample_sha256="a" * 64, capability_id="rule", addresses=("0x1",)
    ).kind == "capability"
    assert (
        ReportClaimProvenance(claim_id="claim", source_document="document").kind
        == "report_claim"
    )
    assert _manual().kind == "analyst_manual"
    with pytest.raises(ValueError):
        SampleFeatureProvenance(sample_sha256="bad", feature_id="f", offsets=())
    with pytest.raises(ValueError):
        CapabilityProvenance(sample_sha256="a" * 64, capability_id="rule", addresses=())
    with pytest.raises(ValueError):
        AnalystManualProvenance(actor_id="", occurred_at=NOW, motif="x")


@pytest.mark.asyncio
async def test_surviving_manual_proposal_is_proposed_and_scored() -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual",
        sample_sha256=None,
        feature_kind="string",
        normalized_value="createmutexw",
    )
    uow.invariants.measurements = FeatureMeasurements(
        eligible_samples_by_family={"luna": 5},
        benign_prevalence=0,
        positive_support=2,
    )
    uow.goodware_baselines.occurrence = 3
    result = await service.propose_manual(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(SAMPLE_ID,),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.C2_INDICATOR,
        motif="CreateMutexW",
        pattern="CreateMutexW",
        actor_id="analyst-1",
        occurred_at=NOW,
    )
    assert result.accepted
    assert result.invariant is not None
    assert result.invariant.status is InvariantStatus.PROPOSED
    assert result.invariant.banality is Banality.SUSPICIOUS_COMMON
    assert result.invariant.goodware_baseline_id is not None


@pytest.mark.asyncio
async def test_invalid_provenance_is_journalled() -> None:
    service, uow = _service()
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.C2_INDICATOR,
        pattern="x",
        provenance=None,  # type: ignore[arg-type]
    )
    assert result.rejection is not None
    assert result.rejection.cause is InvariantRejectionCause.PROVENANCE_INVALID
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_invalid_category_is_journalled() -> None:
    service, _ = _service()
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category="not-a-p09-category",
        pattern="x",
        provenance=_manual("x", NOW.replace(microsecond=4)),
    )
    assert result.rejection is not None
    assert result.rejection.cause is InvariantRejectionCause.INVALID_CATEGORY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "cause"),
    [
        (InvariantCategory.LIBRARY_NOISE, InvariantRejectionCause.LIBRARY_NOISE),
        (InvariantCategory.PACKER_ARTIFACT, InvariantRejectionCause.PACKER_ARTIFACT),
        (InvariantCategory.COMPILER_ARTIFACT, InvariantRejectionCause.COMPILER_ARTIFACT),
        (InvariantCategory.GENERIC_WINAPI, InvariantRejectionCause.GENERIC_WINAPI),
    ],
)
async def test_artifact_categories_are_rejected(category, cause) -> None:
    service, _ = _service()
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category=category,
        pattern=f"x-{cause.value}",
        provenance=_manual(f"x-{cause.value}"),
    )
    assert result.rejection is not None
    assert result.rejection.cause is cause


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "cause"),
    [
        ("", InvariantRejectionCause.EMPTY_PATTERN),
        ("x" * 97, InvariantRejectionCause.PATTERN_TOO_LONG),
    ],
)
async def test_pattern_limits_are_rejected(pattern, cause) -> None:
    service, _ = _service()
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN,
        pattern=pattern,
        provenance=(
            ReportClaimProvenance(claim_id="empty-claim", source_document="doc")
            if not pattern
            else _manual(pattern)
        ),
    )
    assert result.rejection is not None
    assert result.rejection.cause is cause


@pytest.mark.asyncio
async def test_banal_and_multi_family_are_rejected_but_small_corpus_survives() -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="x"
    )
    uow.invariants.measurements = FeatureMeasurements(
        reference_members=((uuid4(), "luna"), (uuid4(), "other")),
        eligible_samples_by_family={"luna": 5, "other": 5},
        benign_prevalence=0,
        positive_support=0,
    )
    uow.goodware_baselines.occurrence = 10
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="banal"
    )
    banal = await service.propose(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN, pattern="banal", provenance=_manual("banal", NOW),
    )
    assert banal.rejection and banal.rejection.cause is InvariantRejectionCause.BANAL

    uow.goodware_baselines.occurrence = None
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="multi"
    )
    multi = await service.propose(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN,
        pattern="multi",
        provenance=_manual("multi", NOW.replace(microsecond=1)),
    )
    assert multi.rejection and multi.rejection.cause is InvariantRejectionCause.MULTI_FAMILY

    uow.invariants.measurements = FeatureMeasurements(
        eligible_samples_by_family={"luna": 1}, benign_prevalence=0, positive_support=0
    )
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="small"
    )
    small = await service.propose(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN,
        pattern="small",
        provenance=_manual("small", NOW.replace(microsecond=2)),
    )
    assert small.accepted
    assert small.invariant and small.invariant.corpus_verdict.value == "CORPUS_TOO_SMALL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invariant_type", [InvariantType.STRUCTURAL_METADATA, InvariantType.RELATION]
)
@pytest.mark.parametrize(
    "provenances",
    [
        (_manual("metadata"),),
        (ReportClaimProvenance(claim_id="claim", source_document="document"),),
        (
            _manual("metadata"),
            ReportClaimProvenance(claim_id="claim", source_document="document"),
        ),
    ],
)
async def test_structural_types_require_technical_provenance(
    invariant_type, provenances
) -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind=None, normalized_value=None
    )
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=invariant_type,
        category=InvariantCategory.UNKNOWN,
        pattern="metadata",
        provenances=provenances,
        occurred_at=NOW.replace(microsecond=5),
    )
    assert result.invariant is None
    assert result.rejection
    assert result.rejection.cause is InvariantRejectionCause.PROVENANCE_INVALID


def _ngram(*, masked: int, longest: int) -> CodeNgram:
    return CodeNgram(
        pattern="90 " * 10,
        instruction_count=4,
        byte_count=10,
        fixed_byte_count=10 - masked,
        masked_byte_count=masked,
        longest_fixed_run=longest,
        function_offset=0x1000,
        start_offset=0x1000,
        mnemonics=("nop",),
        goodware_verdict=GoodwareVerdict.UNKNOWN,
    )


def _code_provenance(moment: datetime = NOW) -> CodeFeatureProvenance:
    return CodeFeatureProvenance(
        sample_sha256="a" * 64,
        function_address=0x1000,
        offset=moment.microsecond,
        disassembler_version="smda",
    )


def _packing() -> PackingSignals:
    return PackingSignals(
        max_executable_section_entropy=0.0,
        executable_bytes=0,
        recovered_function_count=0,
        executable_bytes_per_function=0,
        known_packer_marker_hits=(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ngram", "cause"),
    [
        (_ngram(masked=3, longest=7), InvariantRejectionCause.CODE_NGRAM_MASK_RATIO),
        (_ngram(masked=2, longest=5), InvariantRejectionCause.CODE_NGRAM_CONTIGUOUS_FIXED_RUN),
    ],
)
async def test_code_ngram_thresholds_are_strict_at_the_boundary(ngram, cause) -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="code", sample_sha256="a" * 64, feature_kind="code_ngram",
        normalized_value=ngram.pattern.strip().lower(), code_ngram=ngram, packing=_packing(),
    )
    uow.invariants.measurements = FeatureMeasurements(benign_prevalence=0)
    result = await service.propose(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.CODE_NGRAM,
        category=InvariantCategory.CODE_SEQUENCE,
        pattern=ngram.pattern,
            provenance=_code_provenance(),
    )
    assert result.rejection and result.rejection.cause is cause

    if cause is InvariantRejectionCause.CODE_NGRAM_MASK_RATIO:
        uow.invariants.resolved = ResolvedFeature(
            source_id="code", sample_sha256="a" * 64, feature_kind="code_ngram",
            normalized_value=ngram.pattern.strip().lower(), code_ngram=_ngram(masked=2, longest=8),
            packing=_packing(),
        )
        result = await service.propose(
            investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.CODE_NGRAM,
            category=InvariantCategory.CODE_SEQUENCE, pattern=ngram.pattern,
            provenance=_code_provenance(NOW.replace(microsecond=3)),
        )
        assert result.accepted


def test_likely_packed_uses_all_human_signals() -> None:
    thresholds = {
        "operator": "ALL",
        "max_executable_section_entropy_gte": 7.2,
        "executable_bytes_per_function_gte": 1200,
        "known_packer_marker_hit": True,
    }
    assert likely_packed(
        PackingSignals(
            max_executable_section_entropy=7.2,
            executable_bytes=1200,
            recovered_function_count=1,
            executable_bytes_per_function=1200,
            known_packer_marker_hits=("upx",),
        ), **thresholds
    ) is True
    assert likely_packed(
        PackingSignals(
            max_executable_section_entropy=7.2,
            executable_bytes=1200,
            recovered_function_count=1,
            executable_bytes_per_function=1200,
            known_packer_marker_hits=(),
        ), **thresholds
    ) is False
    assert likely_packed(
        PackingSignals(
            max_executable_section_entropy=None,
            executable_bytes=0,
            recovered_function_count=0,
            executable_bytes_per_function=None,
            known_packer_marker_hits=(),
        ), **thresholds
    ) is None
    assert likely_packed(
        PackingSignals(
            max_executable_section_entropy=1.0,
            executable_bytes=1,
            recovered_function_count=1,
            executable_bytes_per_function=1,
            known_packer_marker_hits=("upx",),
        ),
        operator="ANY",
        max_executable_section_entropy_gte=7.2,
        executable_bytes_per_function_gte=1200,
        known_packer_marker_hit=True,
    ) is True


@pytest.mark.asyncio
async def test_report_claim_confirmation_requires_positive_support() -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="claim", sample_sha256=None, feature_kind="string", normalized_value="x"
    )
    uow.invariants.measurements = FeatureMeasurements(benign_prevalence=0, positive_support=0)
    unconfirmed = await service.propose(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.C2_INDICATOR, pattern="x",
        provenance=ReportClaimProvenance(claim_id="claim-1", source_document="doc-1"),
        positive_sample_confirmed=True,
    )
    assert unconfirmed.accepted and not unconfirmed.invariant.positive_sample_confirmed

    uow.invariants.measurements = FeatureMeasurements(benign_prevalence=0, positive_support=2)
    confirmed = await service.propose(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.C2_INDICATOR, pattern="x",
        provenance=ReportClaimProvenance(claim_id="claim-2", source_document="doc-1"),
        positive_sample_confirmed=True,
    )
    assert confirmed.accepted and confirmed.invariant.positive_sample_confirmed


@pytest.mark.asyncio
async def test_manual_and_regular_paths_share_scorer_and_transition_audit() -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="x"
    )
    uow.invariants.measurements = FeatureMeasurements(benign_prevalence=0)
    uow.goodware_baselines.occurrence = 3
    result = await service.propose_manual(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN, motif="x", actor_id="analyst", occurred_at=NOW,
        pattern="x",
    )
    assert result.invariant and result.invariant.banality is Banality.SUSPICIOUS_COMMON
    transitioned = await service.transition(
        invariant_id=result.invariant.id, to_status=InvariantStatus.APPROVED_FOR_PIVOT,
        actor_id="analyst", occurred_at=NOW, reason="human review",
    )
    assert transitioned.status is InvariantStatus.APPROVED_FOR_PIVOT
    assert uow.invariants.transitions[0][1]["reason"] == "human review"


@pytest.mark.asyncio
async def test_replay_and_rejection_statistics_are_idempotent() -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="x"
    )
    uow.invariants.measurements = FeatureMeasurements(benign_prevalence=0)
    first = await service.propose_manual(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN, motif="x", actor_id="analyst", occurred_at=NOW,
        pattern="x",
    )
    second = await service.propose_manual(
        investigation_id=INVESTIGATION_ID, sample_ids=(), type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN, motif="x", actor_id="analyst", occurred_at=NOW,
        pattern="x",
    )
    assert first.invariant == second.invariant
    assert len(uow.invariants.invariants) == 1

    stats = await service.rejection_statistics(investigation_id=INVESTIGATION_ID, cycle_number=2)
    assert stats == {}


def test_replay_key_is_canonical_and_stable() -> None:
    provenance = _manual("x")
    first = make_proposal_key(
        investigation_id=INVESTIGATION_ID, invariant_type=InvariantType.LITERAL_STRING,
        pattern=" x ", provenance=provenance,
    )
    second = make_proposal_key(
        investigation_id=INVESTIGATION_ID, invariant_type=InvariantType.LITERAL_STRING,
        pattern="x", provenance=provenance,
    )
    assert first == second


def test_provenance_order_does_not_change_replay_key() -> None:
    manual = _manual("analyst note")
    report = ReportClaimProvenance(claim_id="claim", source_document="document")
    first = make_proposal_key(
        investigation_id=INVESTIGATION_ID,
        invariant_type=InvariantType.LITERAL_STRING,
        pattern="x",
        provenances=(manual, report),
    )
    second = make_proposal_key(
        investigation_id=INVESTIGATION_ID,
        invariant_type=InvariantType.LITERAL_STRING,
        pattern="x",
        provenances=(report, manual),
    )
    assert first == second


@pytest.mark.asyncio
async def test_manual_pattern_is_not_its_motif() -> None:
    service, uow = _service()
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual", sample_sha256=None, feature_kind="string", normalized_value="x"
    )
    uow.invariants.measurements = FeatureMeasurements(benign_prevalence=0)
    result = await service.propose_manual(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN,
        motif="analyst explanation",
        pattern="x",
        actor_id="analyst",
        occurred_at=NOW.replace(microsecond=6),
    )
    assert result.invariant is not None
    assert result.invariant.pattern == "x"
    assert isinstance(result.invariant.provenances[0], AnalystManualProvenance)
    assert result.invariant.provenances[0].motif == "analyst explanation"


@pytest.mark.asyncio
async def test_manual_code_ngram_without_technical_origin_is_rejected() -> None:
    service, uow = _service()
    ngram = _ngram(masked=0, longest=10)
    uow.invariants.resolved = ResolvedFeature(
        source_id="manual",
        sample_sha256="a" * 64,
        feature_kind="code_ngram",
        normalized_value=ngram.pattern.strip().lower(),
        code_ngram=ngram,
        packing=_packing(),
    )
    result = await service.propose_manual(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.CODE_NGRAM,
        category=InvariantCategory.CODE_SEQUENCE,
        motif="analyst explanation",
        pattern=ngram.pattern,
        actor_id="analyst",
        occurred_at=NOW.replace(microsecond=7),
    )
    assert result.rejection is not None
    assert result.rejection.cause is InvariantRejectionCause.PROVENANCE_INVALID


@pytest.mark.asyncio
async def test_invalid_member_rejects_the_whole_multi_provenance_proposal() -> None:
    service, uow = _service()
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN,
        pattern="x",
        provenances=(_manual("x"), None),  # type: ignore[arg-type]
    )
    assert result.invariant is None
    assert result.rejection is not None
    assert result.rejection.cause is InvariantRejectionCause.PROVENANCE_INVALID
    assert not uow.invariants.invariants


@pytest.mark.asyncio
async def test_duplicate_provenance_member_is_rejected_deterministically() -> None:
    service, uow = _service()
    manual = _manual("duplicate")
    result = await service.propose(
        investigation_id=INVESTIGATION_ID,
        sample_ids=(),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.UNKNOWN,
        pattern="duplicate",
        provenances=(manual, manual),
    )
    assert result.rejection is not None
    assert result.rejection.cause is InvariantRejectionCause.PROVENANCE_INVALID
    assert not uow.invariants.invariants
