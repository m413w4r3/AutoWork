"""AW-011 cutover: the live EXTRACTION stage is the canonical service.

These tests drive ``ProductionWorkflowOrchestrator._execute_extraction_stage``
against a fake world: the stage reads the frozen corpus, resolves the exact
archived documents, asks the gateway for Q2 wire text, verifies the proposals
locally and persists a single ``ProductionExtractionV1``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.model_gateway import (
    ModelRequest,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_extraction import build_extraction_plan
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    SourceExtraction,
    SourceExtractionStatus,
)
from cti_app.domain.production_extraction import (
    ExtractionReuseState,
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    is_eligible_for_extraction,
)
from tests.test_production_extraction_service import (
    _batch_blocks,
    _JsonTextGateway,
    _q2_wire_output,
)

CORE_A_URL = "https://example.test/core-a"
CORE_B_URL = "https://example.test/core-b"
SUPPORT_URL = "https://example.test/support"
UNAVAILABLE_URL = "https://example.test/unavailable"

CORE_TEXT = (
    "ExampleRAT was deployed by Actor-X on 2026-07-10.\n"
    "The operator ran the command powershell -enc ZXhhbXBsZQ== during the intrusion.\n"
    "The implant beaconed to evil.security-lab.io and exploited CVE-2026-12345.\n"
    "rule ExampleRAT { condition: true }\n"
)
SUPPORT_TEXT = (
    "The loader beaconed to loader.security-lab.io on 2026-07-11.\n"
    "rule SupportRule { condition: true }\n"
)


def _full_output() -> Q2SourceOutput:
    return Q2SourceOutput(
        facts=[
            Q2FactProposal(category="malware", value="ExampleRAT"),
            Q2FactProposal(category="commands", value="powershell -enc ZXhhbXBsZQ=="),
        ],
        events=[
            Q2EventProposal(
                event_date=date(2026, 7, 10),
                text="ExampleRAT was deployed by Actor-X on 2026-07-10.",
            )
        ],
        artifacts=[
            Q2ArtifactProposal(
                value="evil.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
            Q2ArtifactProposal(
                value="ghost.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
        ],
        rules=[
            Q2RuleProposal(
                rule_type=DetectionRuleType.YARA,
                name="ExampleRAT",
                body="rule ExampleRAT { condition: true }",
            )
        ],
        uncertainties=["Attribution of Actor-X remains unconfirmed."],
    )


def _support_output() -> Q2SourceOutput:
    return Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="loader.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ],
        rules=[
            Q2RuleProposal(
                rule_type=DetectionRuleType.YARA,
                name="SupportRule",
                body="rule SupportRule { condition: true }",
            )
        ],
        uncertainties=["The loader family is unconfirmed."],
    )


# --- fake world -------------------------------------------------------------


class _BlobStore:
    def __init__(self) -> None:
        self.bytes: dict[UUID, bytes] = {}
        self.payloads: dict[UUID, dict[str, Any]] = {}
        self.stage_writes: list[tuple[str | None, dict[str, Any] | None]] = []
        self.source_writes = 0

    def put_bytes(self, content: bytes) -> tuple[UUID, str]:
        blob_id = uuid4()
        self.bytes[blob_id] = content
        return blob_id, hashlib.sha256(content).hexdigest()

    def put_json(self, payload: dict[str, Any]) -> UUID:
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        del max_bytes
        if blob_id in self.bytes:
            return self.bytes[blob_id]
        return json.dumps(self.payloads[blob_id]).encode("utf-8")

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        del rendered
        self.stage_writes.append((raw, canonical))
        raw_id = self.put_bytes(raw.encode("utf-8"))[0] if raw is not None else None
        canonical_id = self.put_json(canonical) if canonical is not None else None
        return raw_id, canonical_id, None

    async def put_repair_evidence(self, payload: dict[str, Any]) -> UUID:
        self.repair_evidence = payload
        return self.put_json(payload)

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, Any]
    ) -> tuple[UUID | None, UUID]:
        self.source_writes += 1
        raw_id = self.put_bytes(raw.encode("utf-8"))[0] if raw else None
        return raw_id, self.put_json(canonical)


class _Artifacts:
    def __init__(self) -> None:
        self.current: dict[tuple[UUID, str], ProductionArtifact] = {}
        self.items: list[ProductionArtifact] = []
        self.stale: list[tuple[UUID, str]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        return self.current.get((run_id, stage))

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [artifact for artifact in self.items if artifact.production_run_id == run_id]

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)
        self.current[(artifact.production_run_id, artifact.stage.value)] = artifact

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        self.stale.append((run_id, stage))


class _Runs:
    def __init__(self, run: ProductionRun) -> None:
        self.run = run
        self.saved = 0

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.run if self.run.id == run_id else None

    async def get_for_update(self, run_id: UUID) -> ProductionRun | None:
        return await self.get(run_id)

    async def save(self, run: ProductionRun) -> None:
        self.run = run
        self.saved += 1


class _Documents:
    def __init__(self) -> None:
        self.rows: dict[UUID, Any] = {}

    async def get(self, document_id: UUID) -> Any | None:
        return self.rows.get(document_id)


class _Collections(_Documents):
    pass


class _Extractions:
    FIELDS = (
        "source_content_sha256",
        "profile",
        "contract_version",
        "prompt_version",
        "parser_version",
        "verifier_version",
        "source_text_contract_version",
        "model_policy_version",
        "routing_policy_version",
    )

    def __init__(self) -> None:
        self.rows: dict[tuple[str, ...], SourceExtraction] = {}

    @classmethod
    def _key(cls, values: dict[str, str]) -> tuple[str, ...]:
        return tuple(values[field] for field in cls.FIELDS)

    @classmethod
    def _row_key(cls, row: SourceExtraction) -> tuple[str, ...]:
        return cls._key(
            {
                "source_content_sha256": row.source_content_sha256,
                "profile": row.profile.value,
                "contract_version": row.contract_version,
                "prompt_version": row.prompt_version,
                "parser_version": row.parser_version,
                "verifier_version": row.verifier_version,
                "source_text_contract_version": row.source_text_contract_version,
                "model_policy_version": row.model_policy_version,
                "routing_policy_version": row.routing_policy_version,
            }
        )

    async def get_by_identity(self, **values: str) -> SourceExtraction | None:
        return self.rows.get(self._key(dict(values)))

    async def claim(self, extraction: SourceExtraction, *, force: bool = False) -> bool:
        key = self._row_key(extraction)
        existing = self.rows.get(key)
        verified = existing is not None and existing.status is SourceExtractionStatus.VERIFIED
        if verified and not force:
            return False
        self.rows[key] = extraction
        return True

    async def save(self, extraction: SourceExtraction) -> None:
        self.rows[self._row_key(extraction)] = extraction


class _NoRepairDecisions:
    async def effective_decisions(self, edition_id: UUID, subject_id: UUID) -> tuple[()]:
        del edition_id, subject_id
        return ()


class _Uow:
    def __init__(self, world: _World) -> None:
        self.world = world
        self.production_artifacts = world.artifacts
        self.production_runs = world.runs
        self.source_documents = world.documents
        self.source_collections = world.collections
        self.source_extractions = world.extractions
        self.production_repair_decisions = _NoRepairDecisions()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        return None


class _World:
    def __init__(self, run: ProductionRun) -> None:
        self.store = _BlobStore()
        self.artifacts = _Artifacts()
        self.runs = _Runs(run)
        self.documents = _Documents()
        self.collections = _Collections()
        self.extractions = _Extractions()

    def uow(self) -> _Uow:
        return _Uow(self)


class _Gateway:
    """One fake provider answering with the canonical Q2 wire format."""

    def __init__(self, outputs: dict[str, Q2SourceOutput] | None = None) -> None:
        self.outputs = dict(outputs or {})
        self.calls: list[ModelRequest] = []
        self.ambiguous = False

    def output_for(self, text: str) -> Q2SourceOutput:
        for marker, output in self.outputs.items():
            if marker in text:
                return output
        return Q2SourceOutput()

    def _execution(self, output_text: str) -> SimpleNamespace:
        return SimpleNamespace(
            run=SimpleNamespace(
                id=uuid4(),
                status=ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            structured_output=None,
            output_text=output_text,
            metadata={},
        )

    async def draft(self, request: ModelRequest) -> SimpleNamespace:
        self.calls.append(request)
        if self.ambiguous:
            raise ModelSubmissionReconciliationRequiredError(
                "The submission state is unknown",
                details={"provider_reference": "opaque"},
            )
        if request.prompt_template_id.endswith("-batch"):
            output_text = "\n\n".join(
                f"@@Q2:{handle}@@\n{_q2_wire_output(self.output_for(body))}"
                for handle, body in _batch_blocks(request.text)
            )
        else:
            output_text = _q2_wire_output(self.output_for(request.text))
        return self._execution(output_text)


# --- scenario helpers -------------------------------------------------------


def _snapshot(subject_id: UUID) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        edition_id=uuid4(),
        subject_id=subject_id,
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(),
        discovery_summary="Summary",
        actor_or_campaign="Actor-X",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        publication_language="fr",
        research_date=date(2026, 8, 1),
    )


def _register_source(
    world: _World,
    *,
    subject_id: UUID,
    url: str,
    text: str,
    corrupt: bool = False,
) -> tuple[UUID, str]:
    encoded = text.encode("utf-8")
    blob_id, sha256 = world.store.put_bytes(encoded)
    if corrupt:
        world.store.bytes[blob_id] = b"the archive changed after REFERENCES froze it"
    document_id, collection_id = uuid4(), uuid4()
    world.documents.rows[document_id] = SimpleNamespace(
        id=document_id,
        subject_id=subject_id,
        source_collection_id=collection_id,
        decoded_blob_id=blob_id,
        detected_mime_type="text/plain",
        tlp=TLP.CLEAR,
        external_llm_allowed=True,
        do_not_submit=False,
    )
    world.collections.rows[collection_id] = SimpleNamespace(
        id=collection_id,
        subject_id=subject_id,
        canonical_url=url,
        source_document_id=document_id,
        source_tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
        do_not_submit=False,
    )
    return document_id, sha256


def _reference(
    *,
    url: str,
    tier: ProductionReferenceTier,
    document_id: UUID | None,
    sha256: str | None,
    state: CollectionState = CollectionState.ARCHIVED,
) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        title=f"Archived {url}",
        publisher="Publisher",
        published_at=date(2026, 7, 10),
        source_collection_id=None,
        source_document_id=document_id,
        discovery_candidate_ids=(),
        collection_state=state,
        content_sha256=sha256,
        relevance_reason=None,
        proposed_by_model=False,
        eligible_for_extraction=is_eligible_for_extraction(
            collection_state=state,
            source_document_id=document_id,
            content_sha256=sha256,
        ),
    )


def _corpus(
    *,
    subject_id: UUID,
    input_hash: str,
    sources: tuple[ProductionReferenceSourceV1, ...],
) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 8, 1),
        production_input_hash=input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


def _publish(world: _World, run: ProductionRun, corpus: ProductionReferenceCorpusV1) -> None:
    blob_id = world.store.put_json(production_reference_corpus_to_json(corpus))
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash=corpus.production_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
    )
    world.artifacts.current[(run.id, ProductionArtifactStage.REFERENCES.value)] = artifact
    world.artifacts.items.append(artifact)


async def _run_stage(
    world: _World,
    run: ProductionRun,
    *,
    snapshot: ProductionInputSnapshot,
    gateway: _Gateway,
) -> dict[str, Any]:
    orchestrator = ProductionWorkflowOrchestrator(
        cast(Any, world.uow),
        model_gateway=cast(Any, gateway),
        artifact_store=cast(Any, world.store),
    )
    return await orchestrator._execute_extraction_stage(run, None, snapshot)


def _tiered_world() -> tuple[_World, ProductionRun, ProductionInputSnapshot]:
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    run = ProductionRun(subject_id=subject_id, edition_id=snapshot.edition_id)
    world = _World(run)
    core_a, core_a_sha = _register_source(
        world, subject_id=subject_id, url=CORE_A_URL, text=CORE_TEXT
    )
    core_b, core_b_sha = _register_source(
        world, subject_id=subject_id, url=CORE_B_URL, text=CORE_TEXT
    )
    support, support_sha = _register_source(
        world, subject_id=subject_id, url=SUPPORT_URL, text=SUPPORT_TEXT
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_A_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_a,
                sha256=core_a_sha,
            ),
            _reference(
                url=CORE_B_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core_b,
                sha256=core_b_sha,
            ),
            _reference(
                url=SUPPORT_URL,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=support,
                sha256=support_sha,
            ),
        ),
    )
    _publish(world, run, corpus)
    return world, run, snapshot


def _gateway() -> _Gateway:
    return _Gateway({"ExampleRAT": _full_output(), "loader": _support_output()})


# --- cutover ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_extraction_stage_persists_one_canonical_v1_artifact() -> None:
    world, run, snapshot = _tiered_world()
    gateway = _gateway()

    result = await _run_stage(world, run, snapshot=snapshot, gateway=gateway)

    assert result["status"] == "success"
    assert world.artifacts.stale == [(run.id, "extraction")]
    artifact = world.artifacts.items[-1]
    assert artifact.stage is ProductionArtifactStage.EXTRACTION
    assert artifact.status is ProductionArtifactStatus.VERIFIED
    assert artifact.model_run_id is None
    assert artifact.raw_blob_id is None
    assert artifact.canonical_blob_id is not None
    payload = await world.store.read_json(artifact.canonical_blob_id)
    extraction = production_extraction_from_json(payload)

    profiles = {source.canonical_url: source.profile for source in extraction.sources}
    assert profiles[CORE_A_URL] is ExtractionProfile.FULL
    assert profiles[CORE_B_URL] is ExtractionProfile.FULL
    assert profiles[SUPPORT_URL] is ExtractionProfile.IOC_RULES
    assert [source.tier for source in extraction.sources] == [
        ProductionReferenceTier.CORE,
        ProductionReferenceTier.CORE,
        ProductionReferenceTier.SUPPORTING,
    ]
    # Every canonical element keeps the exact document it came from.
    for source in extraction.sources:
        for item in (*source.facts, *source.events, *source.indicators, *source.rules):
            assert source.source_document_id in item.source_document_ids

    # The metadata stays a bounded projection: counts and versions only.
    assert "facts" not in artifact.metadata
    assert artifact.metadata["source_count"] == 3
    assert artifact.metadata["full_source_count"] == 2
    assert artifact.metadata["ioc_rules_source_count"] == 1
    assert artifact.metadata["profile_policy_version"] == "production-reference-tier-core-only-v3"
    assert artifact.metadata["contract_version"]
    assert "ExampleRAT" not in repr(artifact.metadata)

    # No provider ever received a web instruction: the archive was the input.
    assert gateway.calls
    for request in gateway.calls:
        assert request.web_search is False
        assert request.prompt_template_id.startswith("production-extraction-archive")
    assert any("evil.security-lab.io" in request.text for request in gateway.calls)


@pytest.mark.asyncio
async def test_ineligible_core_source_is_omitted_without_any_model_call() -> None:
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    run = ProductionRun(subject_id=subject_id, edition_id=snapshot.edition_id)
    world = _World(run)
    core, core_sha = _register_source(world, subject_id=subject_id, url=CORE_A_URL, text=CORE_TEXT)
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_A_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=core,
                sha256=core_sha,
            ),
            _reference(
                url=UNAVAILABLE_URL,
                tier=ProductionReferenceTier.SUPPORTING,
                document_id=None,
                sha256=None,
                state=CollectionState.UNAVAILABLE,
            ),
        ),
    )
    _publish(world, run, corpus)
    gateway = _gateway()

    result = await _run_stage(world, run, snapshot=snapshot, gateway=gateway)

    payload = await world.store.read_json(cast(UUID, world.artifacts.items[-1].canonical_blob_id))
    extraction = production_extraction_from_json(payload)
    assert result["omitted_source_count"] == 1
    assert [source.canonical_url for source in extraction.sources] == [CORE_A_URL]
    assert [omission.canonical_url for omission in extraction.omitted_sources] == [UNAVAILABLE_URL]
    assert all(request.web_search is False for request in gateway.calls)


@pytest.mark.asyncio
async def test_two_providers_produce_the_same_canonical_contract() -> None:
    first_world, first_run, snapshot = _tiered_world()
    second_world, second_run, second_snapshot = _tiered_world()

    await _run_stage(first_world, first_run, snapshot=snapshot, gateway=_gateway())
    await _run_stage(
        second_world,
        second_run,
        snapshot=second_snapshot,
        gateway=_JsonTextGateway({"ExampleRAT": _full_output(), "loader": _support_output()}),
    )

    first_payload = await first_world.store.read_json(
        cast(UUID, first_world.artifacts.items[-1].canonical_blob_id)
    )
    second_payload = await second_world.store.read_json(
        cast(UUID, second_world.artifacts.items[-1].canonical_blob_id)
    )
    first = production_extraction_from_json(first_payload)
    second = production_extraction_from_json(second_payload)

    assert _canonical_shape(first) == _canonical_shape(second)


def _canonical_shape(extraction: ProductionExtractionV1) -> list[tuple[Any, ...]]:
    """The provider-independent content of one extraction, without world UUIDs."""

    return [
        (
            source.canonical_url,
            source.profile,
            tuple((fact.category, fact.value, fact.evidence_quote) for fact in source.facts),
            tuple((event.text, event.event_date) for event in source.events),
            tuple(
                (item.artifact_type.value, item.value, item.indicator_status.value)
                for item in source.indicators
            ),
            tuple((rule.rule_type.value, rule.body) for rule in source.rules),
            source.uncertainties,
        )
        for source in extraction.sources
    ]


@pytest.mark.asyncio
async def test_sha_mismatch_blocks_before_any_model_call() -> None:
    subject_id = uuid4()
    snapshot = _snapshot(subject_id)
    run = ProductionRun(subject_id=subject_id, edition_id=snapshot.edition_id)
    world = _World(run)
    document_id, sha256 = _register_source(
        world, subject_id=subject_id, url=CORE_A_URL, text=CORE_TEXT, corrupt=True
    )
    corpus = _corpus(
        subject_id=subject_id,
        input_hash=snapshot.input_hash,
        sources=(
            _reference(
                url=CORE_A_URL,
                tier=ProductionReferenceTier.CORE,
                document_id=document_id,
                sha256=sha256,
            ),
        ),
    )
    _publish(world, run, corpus)
    gateway = _gateway()

    result = await _run_stage(world, run, snapshot=snapshot, gateway=gateway)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "extraction_source_content_mismatch"
    assert gateway.calls == []
    assert world.artifacts.stale == []
    assert [
        artifact
        for artifact in world.artifacts.items
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    ] == []


@pytest.mark.asyncio
async def test_submission_ambiguity_needs_review_without_replay() -> None:
    world, run, snapshot = _tiered_world()
    gateway = _gateway()
    gateway.ambiguous = True

    result = await _run_stage(world, run, snapshot=snapshot, gateway=gateway)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "model_submission_reconciliation_required"
    assert len(gateway.calls) == 1
    assert [
        artifact
        for artifact in world.artifacts.items
        if artifact.stage is ProductionArtifactStage.EXTRACTION
    ] == []


@pytest.mark.asyncio
async def test_stage_fails_explicitly_without_the_canonical_dependencies() -> None:
    world, run, snapshot = _tiered_world()
    orchestrator = ProductionWorkflowOrchestrator(cast(Any, world.uow))

    result = await orchestrator._execute_extraction_stage(run, None, snapshot)

    assert result["status"] == "terminal_error"
    assert result["error_code"] == "extraction_service_unavailable"
    assert world.artifacts.stale == []


@pytest.mark.asyncio
async def test_corpus_without_a_canonical_references_artifact_is_a_control_error() -> None:
    world, run, snapshot = _tiered_world()
    world.artifacts.current.clear()

    result = await _run_stage(world, run, snapshot=snapshot, gateway=_gateway())

    assert result["status"] == "needs_review"
    assert result["error_code"] == "extraction_reference_corpus_missing"


@pytest.mark.asyncio
async def test_identical_input_reuses_the_run_artifact_without_a_model_call() -> None:
    world, run, snapshot = _tiered_world()
    corpus_payload = await world.store.read_json(
        cast(
            UUID,
            world.artifacts.current[
                (run.id, ProductionArtifactStage.REFERENCES.value)
            ].canonical_blob_id,
        )
    )
    from cti_app.application.production_references import production_reference_corpus_from_json

    plan = build_extraction_plan(production_reference_corpus_from_json(corpus_payload))
    existing = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=plan.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=world.store.put_json({"schema_version": 1}),
    )
    world.artifacts.current[(run.id, ProductionArtifactStage.EXTRACTION.value)] = existing
    world.artifacts.items.append(existing)
    gateway = _gateway()

    result = await _run_stage(world, run, snapshot=snapshot, gateway=gateway)

    assert result["status"] == "cached"
    assert result["artifact_id"] == str(existing.id)
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_same_content_under_two_urls_keeps_two_sources_with_one_call() -> None:
    world, run, snapshot = _tiered_world()
    gateway = _gateway()

    result = await _run_stage(world, run, snapshot=snapshot, gateway=gateway)
    payload = await world.store.read_json(cast(UUID, world.artifacts.items[-1].canonical_blob_id))
    extraction = production_extraction_from_json(payload)
    reuse_states = {source.canonical_url: source.reuse_state for source in extraction.sources}

    # A and B carry the exact same bytes: one computation, two canonical sources.
    assert reuse_states[CORE_A_URL] is ExtractionReuseState.FRESH
    assert reuse_states[CORE_B_URL] is ExtractionReuseState.CONTENT_DUPLICATE
    assert result["source_count"] == 3
    # The duplicate bytes were computed once: only one request carried them.
    assert sum("ExampleRAT was deployed" in call.text for call in gateway.calls) == 1


@pytest.mark.asyncio
async def test_cross_run_reuse_hits_the_source_checkpoint_without_a_call() -> None:
    first_world, first_run, snapshot = _tiered_world()
    await _run_stage(first_world, first_run, snapshot=snapshot, gateway=_gateway())
    second_run = ProductionRun(subject_id=first_run.subject_id, edition_id=snapshot.edition_id)
    second_world = _World(second_run)
    second_world.extractions = first_world.extractions
    second_world.store = first_world.store
    second_world.documents = first_world.documents
    second_world.collections = first_world.collections
    corpus_payload = await first_world.store.read_json(
        cast(
            UUID,
            first_world.artifacts.current[
                (first_run.id, ProductionArtifactStage.REFERENCES.value)
            ].canonical_blob_id,
        )
    )
    from cti_app.application.production_references import production_reference_corpus_from_json

    _publish(second_world, second_run, production_reference_corpus_from_json(corpus_payload))
    gateway = _gateway()

    result = await _run_stage(second_world, second_run, snapshot=snapshot, gateway=gateway)

    assert result["status"] == "success"
    assert gateway.calls == []
    payload = await second_world.store.read_json(
        cast(UUID, second_world.artifacts.items[-1].canonical_blob_id)
    )
    extraction = production_extraction_from_json(payload)
    assert all(
        source.reuse_state in {ExtractionReuseState.REUSED, ExtractionReuseState.CONTENT_DUPLICATE}
        for source in extraction.sources
    )
    reuse_states = {source.canonical_url: source.reuse_state for source in extraction.sources}
    assert reuse_states[CORE_A_URL] is ExtractionReuseState.REUSED
    assert reuse_states[SUPPORT_URL] is ExtractionReuseState.REUSED
    assert isinstance(extraction, ProductionExtractionV1)
