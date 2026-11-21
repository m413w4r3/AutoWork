from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_prompts import (
    ARTICLE_SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION,
    ARTICLE_SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION,
)
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import (
    ProductionSynthesisService,
    build_synthesis_access_policy,
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
    synthesis_input_hash,
)
from cti_app.application.production_synthesis_revision import (
    ARTICLE_TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION,
    TARGETED_SYNTHESIS_REVISION_PARSER_VERSION,
    ProductionSynthesisRevisionService,
    TargetedSynthesisRevisionPreview,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import production_extraction_to_json
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from tests import test_production_synthesis_application as revision_test_support


class _TrackedUow(revision_test_support._RevisionMemoryUow):
    def __init__(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        artifacts: Any,
        documents: Any,
        trace: list[tuple[str, object]],
    ) -> None:
        super().__init__(run=run, snapshot=snapshot, artifacts=artifacts, documents=documents)
        self._trace = trace

    async def commit(self) -> None:
        self._trace.append(("commit", self))


class _RecordingRevisionEffects:
    def __init__(self, trace: list[tuple[str, object]], gateway: Any, run: ProductionRun) -> None:
        self.trace = trace
        self.gateway = gateway
        self.run = run
        self.calls: list[tuple[str, object]] = []

    def _record(self, name: str, uow: object) -> None:
        self.trace.append((name, uow))
        self.calls.append((name, uow))

    async def acquire_run_guard(self, uow: object, production_run_id: UUID) -> object:
        self._record("acquire_run_guard", uow)
        assert production_run_id == self.run.id
        assert len(self.gateway.calls) == 1
        return SimpleNamespace(production_run_id=production_run_id)

    async def lock_run_row(self, uow: object, _run_guard: object) -> ProductionRun:
        self._record("lock_run_row", uow)
        return self.run

    async def current_snapshot_in_uow(self, uow: object, _subject_id: UUID) -> object:
        self._record("current_snapshot_in_uow", uow)
        return SimpleNamespace(production_run_id=self.run.id)

    async def apply_after_writes(self, uow: object, **values: Any) -> object:
        self._record("apply_after_writes", uow)
        assert len(self.gateway.calls) == 1
        return SimpleNamespace(snapshot=values["base_snapshot"])


class _RecordingSynthesisPersistence:
    def __init__(self, persistence: SynthesisService, trace: list[tuple[str, object]]) -> None:
        self.persistence = persistence
        self.trace = trace
        self.calls: list[object] = []

    async def store_synthesis_result(self, **kwargs: Any) -> ProductionArtifact:
        uow = kwargs["uow"]
        self.trace.append(("persist_candidate", uow))
        self.calls.append(uow)
        return await self.persistence.store_synthesis_result(**kwargs)


class _RevisionCase:
    def __init__(
        self,
        *,
        service: ProductionSynthesisRevisionService,
        request: TargetedEditorialRevisionRequest,
        gateway: Any,
        artifact_store: Any,
        repository: Any,
        effects: _RecordingRevisionEffects,
        persistence: _RecordingSynthesisPersistence,
        trace: list[tuple[str, object]],
        uows: list[_TrackedUow],
        base_artifact: ProductionArtifact,
    ) -> None:
        self.service = service
        self.request = request
        self.gateway = gateway
        self.artifact_store = artifact_store
        self.repository = repository
        self.effects = effects
        self.persistence = persistence
        self.trace = trace
        self.uows = uows
        self.base_artifact = base_artifact


async def _revision_case() -> _RevisionCase:
    world, synthesis, _evidence_pack, _handle = (
        revision_test_support._targeted_article_revision_fixture()
    )
    source = world.extraction.sources[0]
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=world.snapshot.subject_id,
        research_date=world.snapshot.research_date,
        production_input_hash=world.snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url=source.canonical_url,
                tier=source.tier,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                title="Synthetic archived report",
                publisher="Synthetic publisher",
                published_at=None,
                source_collection_id=None,
                source_document_id=source.source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=source.content_sha256,
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
                editorial_role=ProductionEditorialRole.PRIMARY,
            ),
        ),
        warnings=(),
    )
    extraction = replace(
        world.extraction,
        references_corpus_hash=references_corpus_hash(references),
    )
    synthesis = replace(synthesis, extraction_hash=canonical_extraction_hash(extraction))
    evidence_pack = build_synthesis_evidence_pack(world.snapshot, extraction)
    access_policy = await build_synthesis_access_policy(
        world.snapshot,
        extraction,
        world.docs,
        revision_test_support.MemorySourceCollections(),
    )
    input_hash = synthesis_input_hash(
        world.snapshot,
        extraction,
        evidence_pack,
        synthesis_access_policy_hash(access_policy),
    )
    payload = production_synthesis_to_json(synthesis)

    artifact_store = revision_test_support._RevisionMemoryBlobStore()
    references_artifact = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.snapshot.subject_id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=artifact_store.add_json(production_reference_corpus_to_json(references)),
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.snapshot.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=revision_test_support.EXTRACTION_ARTIFACT_HASH,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=artifact_store.add_json(production_extraction_to_json(extraction)),
    )
    base_artifact = ProductionArtifact(
        production_run_id=world.run.id,
        subject_id=world.snapshot.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=artifact_store.add_json(payload),
        metadata={"relevance_projection_hash": None},
    )
    repository = revision_test_support._RevisionArtifactRepository(
        (references_artifact, extraction_artifact, base_artifact)
    )
    trace: list[tuple[str, object]] = []
    uows: list[_TrackedUow] = []

    def uow_factory() -> _TrackedUow:
        uow = _TrackedUow(
            run=world.run,
            snapshot=world.snapshot,
            artifacts=repository,
            documents=world.docs,
            trace=trace,
        )
        uows.append(uow)
        return uow

    target = TargetedEditorialRevisionTarget(kind=TargetedEditorialRevisionTargetKind.TITLE)
    operation = TargetedEditorialRevisionOperation.REWRITE_TITLE
    after = (
        f"@@TITLE@@\n[{world.snapshot.actor_or_campaign}] Updated FooRAT activity\n@@END TITLE@@"
    )
    patch = revision_test_support._targeted_wire_patch(payload, target, operation, after)
    wire = "\n".join(
        (
            f"@@TARGET@@\n{patch.target}",
            f"@@ACTION@@\n{operation.value}",
            f"@@BASE_ARTIFACT_HASH@@\n{patch.base_artifact_hash}",
            f"@@BEFORE_HASH@@\n{patch.before_hash}",
            f"@@AFTER@@\n{patch.after}",
            f"@@RATIONALE@@\n{patch.rationale}",
            "@@END@@",
        )
    )
    gateway = revision_test_support._RecordingGateway(
        lambda model_request: revision_test_support._succeeded(model_request, None, text=wire)
    )
    persistence = SynthesisService(cast(Any, uow_factory), cast(Any, artifact_store))
    recording_persistence = _RecordingSynthesisPersistence(persistence, trace)
    effects = _RecordingRevisionEffects(trace, gateway, world.run)
    synthesis_service = ProductionSynthesisService(
        uow_factory=cast(Any, uow_factory),
        artifact_store=cast(Any, artifact_store),
        model_gateway=cast(Any, gateway),
        synthesis_service=cast(Any, persistence),
    )
    service = ProductionSynthesisRevisionService(
        synthesis_service=synthesis_service,
        synthesis_persistence=cast(Any, recording_persistence),
        enrichment_persistence=EditorialEnrichmentService(
            cast(Any, uow_factory), cast(Any, artifact_store)
        ),
        publication_render_service=cast(Any, SimpleNamespace()),
        revision_effects=cast(Any, effects),
    )
    request = TargetedEditorialRevisionRequest(
        subject_id=world.snapshot.subject_id,
        base_artifact_id=base_artifact.id,
        base_version=base_artifact.version,
        base_input_hash=base_artifact.input_hash,
        base_canonical_sha256=hashlib.sha256(
            artifact_store.bytes[base_artifact.canonical_blob_id]
        ).hexdigest(),
        target=target,
        operation=operation,
        instruction="Correct the title using the archived report.",
        mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
        requester="synthetic-analyst",
    )
    return _RevisionCase(
        service=service,
        request=request,
        gateway=gateway,
        artifact_store=artifact_store,
        repository=repository,
        effects=effects,
        persistence=recording_persistence,
        trace=trace,
        uows=uows,
        base_artifact=base_artifact,
    )


@pytest.mark.asyncio
async def test_legacy_revise_uses_shared_effects_in_one_uow() -> None:
    case = await _revision_case()

    result = await case.service.revise(case.request)

    assert result.refusal is None, (
        f"{result.refusal.refusal_reason}: {result.refusal.reason}" if result.refusal else None
    )
    assert result.artifact is not None
    assert len(case.gateway.calls) == 1
    assert len(case.persistence.calls) == 1
    assert len(case.effects.calls) == 4
    write_uow = next(uow for name, uow in case.trace if name == "persist_candidate")
    transaction_events = [
        (name, uow)
        for name, uow in case.trace
        if name
        in {
            "acquire_run_guard",
            "lock_run_row",
            "current_snapshot_in_uow",
            "persist_candidate",
            "apply_after_writes",
            "commit",
        }
    ]
    assert [name for name, _uow in transaction_events] == [
        "acquire_run_guard",
        "lock_run_row",
        "current_snapshot_in_uow",
        "persist_candidate",
        "apply_after_writes",
        "commit",
    ]
    assert all(uow is write_uow for _name, uow in transaction_events)

    identity_payload = case.service._request_identity_payload(
        case.request,
        publication_kind=revision_test_support.PublicationKind.ARTICLE,
    )
    assert identity_payload["prompt_version"] == ("synthesis-targeted-revision-v6-article-overview")
    assert identity_payload["contract_version"] == ("synthesis-targeted-patch-v8-article-overview")
    assert identity_payload["parser_version"] == (
        "targeted-synthesis-patch-parser-v5-end-marker-tolerance"
    )
    assert identity_payload["validator_version"] == (
        "targeted-synthesis-patch-validator-v8-article-overview"
    )
    assert result.revision is not None
    assert result.revision["request_identity"] == (
        revision_test_support.synthesis_revision_module._sha256(identity_payload)
    )
    model_request = case.gateway.calls[0][0]
    assert model_request.prompt_template_version == (
        ARTICLE_SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION
    )
    assert model_request.metadata["targeted_revision_contract_version"] == (
        ARTICLE_SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION
    )
    assert model_request.metadata["targeted_revision_parser_version"] == (
        TARGETED_SYNTHESIS_REVISION_PARSER_VERSION
    )
    assert model_request.metadata["targeted_revision_validator_version"] == (
        ARTICLE_TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION
    )


@pytest.mark.asyncio
async def test_legacy_replay_returns_same_adopted_artifact() -> None:
    case = await _revision_case()

    first = await case.service.revise(case.request)
    assert first.artifact is not None, (
        f"{first.refusal.refusal_reason}: {first.refusal.reason}" if first.refusal else None
    )
    assert first.artifact.canonical_blob_id is not None
    first_bytes = case.artifact_store.bytes[first.artifact.canonical_blob_id]
    first_ids = (first.artifact.id, first.artifact.canonical_blob_id, first.artifact.model_run_id)
    first_identity = first.revision["request_identity"] if first.revision is not None else None
    first_synthesis_count = sum(
        item.stage is ProductionArtifactStage.SYNTHESIS for item in case.repository.artifacts
    )
    first_commit_count = sum(name == "commit" for name, _item in case.trace)

    replay = await case.service.revise(case.request)

    assert replay.artifact is not None
    assert replay.artifact.canonical_blob_id is not None
    replay_ids = (
        replay.artifact.id,
        replay.artifact.canonical_blob_id,
        replay.artifact.model_run_id,
    )
    assert replay_ids == first_ids
    replay_bytes = case.artifact_store.bytes[replay.artifact.canonical_blob_id]
    assert replay_bytes == first_bytes
    assert hashlib.sha256(replay_bytes).hexdigest() == hashlib.sha256(first_bytes).hexdigest()
    assert replay.revision is not None
    assert replay.revision["request_identity"] == first_identity
    assert len(case.gateway.calls) == 1
    assert len(case.persistence.calls) == 1
    assert (
        sum(item.stage is ProductionArtifactStage.SYNTHESIS for item in case.repository.artifacts)
        == first_synthesis_count
    )
    assert sum(name == "commit" for name, _item in case.trace) == first_commit_count


@pytest.mark.asyncio
async def test_preview_never_writes() -> None:
    case = await _revision_case()
    artifacts_before = tuple(case.repository.artifacts)
    workspace_snapshots_before = tuple(case.repository.workspace_snapshots.records)
    bytes_before = dict(case.artifact_store.bytes)
    payloads_before = dict(case.artifact_store.payloads)

    preview = await case.service.preview(case.request)

    assert isinstance(preview, TargetedSynthesisRevisionPreview)
    assert preview.expected_model_calls == 1
    assert tuple(case.repository.artifacts) == artifacts_before
    assert tuple(case.repository.workspace_snapshots.records) == workspace_snapshots_before
    assert case.artifact_store.bytes == bytes_before
    assert case.artifact_store.payloads == payloads_before
    assert case.gateway.calls == []
    assert case.persistence.calls == []
    assert case.effects.calls == []
    assert not any(name == "commit" for name, _item in case.trace)
