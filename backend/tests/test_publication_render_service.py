from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cti_app.application import publication_rendering, typst_render_execution
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import PublicationRenderUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.application.typst_bundle import hash_bundle_contents
from cti_app.application.typst_compilation import (
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    CompiledTypstDocument,
    FontBundleSnapshot,
    TypstCompilationError,
    TypstCompileFailedError,
    TypstCompileRequest,
    TypstCompilerUnavailableError,
    TypstCompilerVersionMismatchError,
    TypstCompileTimeoutError,
    TypstOutputInvalidError,
    TypstOutputTooLargeError,
)
from cti_app.application.typst_rendering import (
    TemplateFile,
    TypstMediaRef,
    TypstRenderer,
    TypstRenderSource,
    TypstTemplateBundle,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.errors import BlobIntegrityError
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    publication_document_v4_to_json,
    serialize_publication_document,
)
from cti_app.domain.publication_render import (
    PUBLICATION_RENDER_POLICY_VERSION,
    PublicationRender,
    compute_publication_render_input_hash,
)
from cti_app.domain.typst_render import (
    TypstRenderAcquisition,
    TypstRenderAcquisitionOutcome,
    TypstRenderFormat,
    TypstRenderStatus,
    invalid_succeeded_reacquisition_outcome,
    typst_render_acquisition_outcome,
    typst_render_retrying,
)
from tests.test_publication_document_v6 import _approved_opinion, _base_v5


def _document() -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version="4",
        subject_id=uuid4(),
        publication_language="fr",
        title="Service fixture",
        lead=(),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )


def test_pdf_identity_label_uses_artifact_version_and_hash_for_each_preview() -> None:
    artifact_id = uuid4()
    opinion_id = uuid4()
    common = {
        "nature": "opinion_draft_preview",
        "artifact_id": artifact_id,
        "artifact_version": 1,
        "opinion_id": opinion_id,
        "opinion_version": 27,
        "opinion_status": "draft",
    }
    first = publication_rendering.publication_identity_label(
        **common,
        artifact_sha256="a" * 64,
    )
    second = publication_rendering.publication_identity_label(
        **common,
        artifact_sha256="b" * 64,
    )

    assert first != second
    assert str(artifact_id) in first
    assert "v1" in first
    assert "a" * 64 in first
    assert "Publication V5" not in first


def test_publication_without_opinion_is_labeled_as_base_publication() -> None:
    label = publication_rendering.publication_identity_label(
        nature="base_publication",
        artifact_id=uuid4(),
        artifact_version=1,
        artifact_sha256="c" * 64,
    )

    assert label.startswith("Publication de base")
    assert "avis" not in label


def test_lineage_render_data_carries_document_label_and_exact_render_hash() -> None:
    label = "Aperçu d'avis opinion-1 v27 — draft; artefact artifact-1 v1"
    source_bytes = b"typst source"
    render_data_bytes = b"{}"
    source = TypstRenderSource(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        render_data_bytes=render_data_bytes,
        render_data_sha256=hashlib.sha256(render_data_bytes).hexdigest(),
        media_refs=(),
    )

    enriched = publication_rendering.with_publication_lineage(
        source,
        identity_label=label,
        lineage={"nature": "opinion_draft_preview"},
    )
    payload = json.loads(enriched.render_data_bytes)

    assert payload["document_label"] == label
    assert payload["pdf_lineage"]["nature"] == "opinion_draft_preview"
    assert enriched.render_data_sha256 == hashlib.sha256(enriched.render_data_bytes).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_artifact_resolution",
    ("matched", "missing", "wrong_id", "wrong_stage", "wrong_subject", "wrong_run"),
)
@pytest.mark.parametrize(
    "decision_event_state", ("found", "empty", "missing_repository", "wrong_actor")
)
async def test_prepare_lineage_records_only_canonical_augmentation_actors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    base_artifact_resolution: str,
    decision_event_state: str,
) -> None:
    base_document = _base_v5()
    base_artifact_id = uuid4()
    base_blob_id = uuid4()
    run_id = uuid4()
    base_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(base_document)
    )
    base_artifact = ProductionArtifact(
        id=base_artifact_id,
        production_run_id=run_id,
        subject_id=base_document.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
        metadata={"previous_publication_artifact_id": str(uuid4())},
    )

    opinion, context = _approved_opinion(base_document)
    opinion = replace(
        opinion,
        base_publication_artifact_id=base_artifact_id,
        content_sha256="",
    )
    context = replace(context, base_publication_artifact_id=base_artifact_id)
    augmented_document = augment_publication_with_opinion(base_document, opinion, context)
    augmented_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(augmented_document)
    )
    augmented_blob_id = uuid4()
    augmented_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=base_document.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash=hashlib.sha256(augmented_bytes).hexdigest(),
        canonical_blob_id=augmented_blob_id,
        metadata={
            "previous_publication_artifact_id": str(base_artifact.id),
            "publication_augmentation": {
                "base_publication_artifact_id": str(base_artifact.id),
                "base_publication_sha256": opinion.base_publication_sha256,
                "investigation_id": str(opinion.investigation_id),
                "opinion_id": str(opinion.id),
                "opinion_version": opinion.version,
                "opinion_content_sha256": opinion.content_sha256,
                "reviewer_id": opinion.reviewer_id,
                "approver_id": context.approver_id,
            },
        },
    )
    artifact_store = FakeArtifactStore()
    artifact_store.payloads[base_blob_id] = serialize_publication_document(base_document)
    artifact_store.payloads[augmented_blob_id] = serialize_publication_document(augmented_document)
    uow_factory = FakeUnitOfWorkFactory(
        {base_artifact.id: base_artifact, augmented_artifact.id: augmented_artifact}
    )
    if base_artifact_resolution == "missing":
        del uow_factory.production_artifacts.artifacts[base_artifact.id]
    elif base_artifact_resolution != "matched":
        mismatched_base = {
            "wrong_id": replace(base_artifact, id=uuid4()),
            "wrong_stage": replace(base_artifact, stage=ProductionArtifactStage.SYNTHESIS),
            "wrong_subject": replace(base_artifact, subject_id=uuid4()),
            "wrong_run": replace(base_artifact, production_run_id=uuid4()),
        }[base_artifact_resolution]
        uow_factory.production_artifacts.artifacts[base_artifact.id] = mismatched_base
    approval_decision = AnalystDecision(
        investigation_id=opinion.investigation_id,
        decision_type=AnalystDecisionType.NOTE_APPROVE,
        target_type=AnalystDecisionTargetType.NOTE,
        target_id=opinion.id,
        actor_id=(
            "different-reviewer" if decision_event_state == "wrong_actor" else opinion.reviewer_id
        ),
        reason="Reviewed opinion approved",
        correlation_id="mission04-review",
        occurred_at=datetime(2026, 10, 24, tzinfo=UTC),
    )
    if decision_event_state != "missing_repository":
        uow_factory.analyst_decisions = SimpleNamespace(
            list_for_investigation=lambda requested_investigation_id: _async_value(
                [approval_decision]
                if requested_investigation_id == opinion.investigation_id
                and decision_event_state in {"found", "wrong_actor"}
                else []
            )
        )
    template_bundle = TypstTemplateBundle(
        "unit-template-v1",
        "c" * 64,
        (TemplateFile("RENDERER/publication.typ", b'#let publication = json("render-data.json")'),),
    )
    monkeypatch.setattr(
        publication_rendering, "load_template_bundle", lambda _root: template_bundle
    )
    monkeypatch.setattr(
        publication_rendering,
        "load_font_bundle_snapshot",
        lambda *_args: FontBundleSnapshot("unit-fonts-v1", ()),
    )

    service = object.__new__(publication_rendering.PublicationRenderService)
    service._uow_factory = uow_factory
    service._artifact_store = artifact_store
    service._renderer = FakeRenderer()
    service._chp_typst_root = tmp_path
    service._font_bundle_root = tmp_path
    service._typst_fonts_lock_path = tmp_path / "typst-fonts.lock"
    service._graph_diagram_compiler = None

    if base_artifact_resolution != "matched":
        with pytest.raises(
            publication_rendering.PublicationRenderArtifactInvalidError,
            match=(
                "V6 publication base artifact cannot be resolved to a matching publication artifact"
            ),
        ):
            await service._prepare(augmented_artifact.id)
        return

    base_prepared = await service._prepare(base_artifact.id)
    base_lineage = json.loads(base_prepared.build_source().render_data_bytes)["pdf_lineage"]
    if decision_event_state == "wrong_actor":
        with pytest.raises(
            publication_rendering.PublicationRenderArtifactInvalidError,
            match="V6 reviewer provenance does not match its NOTE_APPROVE decision",
        ):
            await service._prepare(augmented_artifact.id)
        return

    augmented_prepared = await service._prepare(augmented_artifact.id)
    augmented_lineage = json.loads(augmented_prepared.build_source().render_data_bytes)[
        "pdf_lineage"
    ]

    assert base_lineage["nature"] == "base_publication"
    assert base_lineage["opinion"] is None
    assert "actors" not in base_lineage
    assert augmented_lineage["nature"] == "augmented_publication"
    reviewer_lineage = augmented_lineage["actors"]["reviewer"]
    assert reviewer_lineage == {
        "actor_id": opinion.reviewer_id,
        "role": "reviewer",
        "decision_label": "opinion_approved",
        "investigation_id": str(opinion.investigation_id),
        "opinion_id": str(opinion.id),
        "opinion_version": opinion.version,
        "opinion_content_sha256": opinion.content_sha256,
        "decision_id": str(approval_decision.id) if decision_event_state == "found" else None,
        "occurred_at": approval_decision.occurred_at.isoformat()
        if decision_event_state == "found"
        else None,
        "decision_reference_status": "verified"
        if decision_event_state == "found"
        else "unavailable",
    }
    assert augmented_lineage["actors"]["approver"] == {
        "actor_id": context.approver_id,
        "role": "approver",
        "decision_label": "publication_augmented",
        "base_publication": {
            "artifact_id": str(base_artifact.id),
            "version": base_artifact.version,
            "sha256": opinion.base_publication_sha256,
        },
        "augmented_publication": {
            "artifact_id": str(augmented_artifact.id),
            "version": augmented_artifact.version,
            "sha256": hashlib.sha256(augmented_bytes).hexdigest(),
        },
    }


@pytest.mark.asyncio
async def test_lineage_marks_a_pinned_opinion_from_another_investigation_historical() -> None:
    run_id = uuid4()
    subject_id = uuid4()
    artifact_id = uuid4()
    canonical_blob_id = uuid4()
    render_data_blob_id = uuid4()
    opinion_id = uuid4()
    pinned_investigation_id = uuid4()
    current_investigation_id = uuid4()
    artifact = ProductionArtifact(
        id=artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash="d" * 64,
        canonical_blob_id=canonical_blob_id,
        metadata={
            "publication_augmentation": {
                "opinion_id": str(opinion_id),
                "opinion_version": 16,
                "opinion_content_sha256": "a" * 64,
                "investigation_id": str(pinned_investigation_id),
            }
        },
    )
    opinion = SimpleNamespace(
        id=opinion_id,
        version=16,
        content_sha256="a" * 64,
        status=SimpleNamespace(value="approved"),
        investigation_id=pinned_investigation_id,
    )

    class _UnitOfWork:
        production_artifacts = SimpleNamespace(
            get=lambda requested_id: _async_value(
                artifact if requested_id == artifact_id else None
            ),
            get_current=lambda requested_run_id, _stage: _async_value(
                artifact if requested_run_id == run_id else None
            ),
        )
        production_runs = SimpleNamespace(
            get_current_for_subject=lambda requested_subject_id: _async_value(
                SimpleNamespace(id=run_id) if requested_subject_id == subject_id else None
            )
        )
        analyst_investigations = SimpleNamespace(
            get_for_run=lambda requested_run_id: _async_value(
                SimpleNamespace(id=current_investigation_id) if requested_run_id == run_id else None
            )
        )
        analyst_opinions = SimpleNamespace(get=lambda _opinion_id, **_kwargs: _async_value(opinion))

        async def commit(self) -> None:
            return None

    class _Context:
        async def __aenter__(self) -> _UnitOfWork:
            return _UnitOfWork()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Store:
        async def read_bytes(self, blob_id: UUID) -> bytes:
            return b"{}" if blob_id == render_data_blob_id else b"canonical publication"

    service = object.__new__(publication_rendering.PublicationRenderService)
    service._uow_factory = lambda: _Context()
    service._artifact_store = _Store()
    render = replace(
        _proposed_render(artifact_id),
        render_data_blob_id=render_data_blob_id,
        output_blob_id=uuid4(),
        output_sha256="b" * 64,
        output_byte_size=10,
        status=TypstRenderStatus.SUCCEEDED,
    )

    lineage = await service.lineage_manifest_for_render(render)

    assert lineage["historical"] is True
    assert lineage["is_current"] is False
    assert "pinned_opinion_investigation_mismatch" in lineage["history_reasons"]


@pytest.mark.asyncio
async def test_lineage_reconstruction_preserves_stored_actors_without_inventing_legacy_actors() -> (
    None
):
    run_id = uuid4()
    subject_id = uuid4()
    artifact_id = uuid4()
    canonical_blob_id = uuid4()
    render_data_blob_id = uuid4()
    opinion_id = uuid4()
    investigation_id = uuid4()
    reviewer_id = "reviewer:canonical"
    approver_id = "approver:canonical"
    actors = {
        "reviewer": {
            "actor_id": reviewer_id,
            "role": "reviewer",
            "decision_label": "opinion_approved",
            "investigation_id": str(investigation_id),
            "opinion_id": str(opinion_id),
            "opinion_version": 3,
            "opinion_content_sha256": "a" * 64,
        },
        "approver": {
            "actor_id": approver_id,
            "role": "approver",
            "decision_label": "publication_augmented",
            "base_publication": {
                "artifact_id": str(uuid4()),
                "version": 1,
                "sha256": "b" * 64,
            },
            "augmented_publication": {
                "artifact_id": str(artifact_id),
                "version": 2,
                "sha256": "c" * 64,
            },
        },
    }
    artifact = ProductionArtifact(
        id=artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash="d" * 64,
        canonical_blob_id=canonical_blob_id,
        metadata={
            "publication_augmentation": {
                "investigation_id": str(investigation_id),
                "opinion_id": str(opinion_id),
                "opinion_version": 3,
                "opinion_content_sha256": "a" * 64,
                "reviewer_id": reviewer_id,
                "approver_id": approver_id,
            }
        },
    )
    opinion = SimpleNamespace(
        id=opinion_id,
        version=3,
        content_sha256="a" * 64,
        status=SimpleNamespace(value="approved"),
        investigation_id=investigation_id,
    )

    class _UnitOfWork:
        production_artifacts = SimpleNamespace(
            get=lambda requested_id: _async_value(
                artifact if requested_id == artifact_id else None
            ),
            get_current=lambda requested_run_id, _stage: _async_value(
                artifact if requested_run_id == run_id else None
            ),
        )
        production_runs = SimpleNamespace(
            get_current_for_subject=lambda requested_subject_id: _async_value(
                SimpleNamespace(id=run_id) if requested_subject_id == subject_id else None
            )
        )
        analyst_investigations = SimpleNamespace(
            get_for_run=lambda requested_run_id: _async_value(
                SimpleNamespace(id=investigation_id) if requested_run_id == run_id else None
            )
        )
        analyst_opinions = SimpleNamespace(get=lambda _opinion_id, **_kwargs: _async_value(opinion))

        async def commit(self) -> None:
            return None

    class _Context:
        def __init__(self, _lineage: dict[str, Any]) -> None:
            self.lineage = _lineage

        async def __aenter__(self) -> _UnitOfWork:
            return _UnitOfWork()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Store:
        def __init__(self, render_data: bytes) -> None:
            self.render_data = render_data

        async def read_bytes(self, blob_id: UUID) -> bytes:
            return self.render_data if blob_id == render_data_blob_id else b"canonical"

    for stored_actors in (actors, None):
        stored_lineage: dict[str, Any] = {
            "schema_version": "pdf-lineage.v1",
            "nature": "augmented_publication",
            "artifact": {
                "id": str(artifact.id),
                "version": artifact.version,
                "sha256": "c" * 64,
                "document_schema": "6",
            },
            "opinion": {
                "id": str(opinion_id),
                "version": 3,
                "status": "approved",
                "sha256": "a" * 64,
                "investigation_id": str(investigation_id),
            },
            "investigation_id": str(investigation_id),
        }
        if stored_actors is not None:
            stored_lineage["actors"] = stored_actors
        render_data = json.dumps({"pdf_lineage": stored_lineage}).encode()
        service = object.__new__(publication_rendering.PublicationRenderService)
        service._uow_factory = lambda lineage=stored_lineage: _Context(lineage)
        service._artifact_store = _Store(render_data)
        render = replace(
            _proposed_render(artifact_id),
            render_data_blob_id=render_data_blob_id,
            output_blob_id=uuid4(),
            output_sha256="e" * 64,
            output_byte_size=10,
            status=TypstRenderStatus.SUCCEEDED,
        )

        lineage = await service.lineage_manifest_for_render(render)

        if stored_actors is None:
            assert "actors" not in lineage
        else:
            assert lineage["actors"] == stored_actors


async def _async_value(value: Any) -> Any:
    return value


def _proposed_render(artifact_id: UUID, *, input_hash: str | None = None) -> PublicationRender:
    now = datetime.now(UTC)
    return PublicationRender(
        id=uuid4(),
        publication_artifact_id=artifact_id,
        renderer="typst",
        renderer_version="publication-v4-typst-v1",
        template_version="unit-template-v1",
        template_sha256="a" * 64,
        compiler="typst",
        compiler_version="0.15.1",
        font_bundle_version="chp-fonts-v1",
        render_policy_version="typst-publication-v4-v1",
        format=TypstRenderFormat.PDF,
        input_hash=input_hash or hashlib.sha256(uuid4().bytes).hexdigest(),
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=None,
        output_sha256=None,
        output_byte_size=None,
        status=TypstRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


class FakeProductionArtifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)


class FakePublicationRenders:
    def __init__(self) -> None:
        self.renders: dict[UUID, PublicationRender] = {}
        self.in_progress_observed: asyncio.Event | None = None

    async def get_by_input_hash(self, input_hash: str) -> PublicationRender | None:
        return next(
            (render for render in self.renders.values() if render.input_hash == input_hash),
            None,
        )

    async def get_pinned_succeeded_for_artifact(
        self, artifact_id: UUID
    ) -> PublicationRender | None:
        matches = [
            render
            for render in self.renders.values()
            if render.publication_artifact_id == artifact_id
            and render.status is TypstRenderStatus.SUCCEEDED
        ]
        return min(matches, key=lambda render: (render.created_at, str(render.id)), default=None)

    async def acquire_for_render(
        self, proposed: PublicationRender, *, stale_running_before: datetime
    ) -> TypstRenderAcquisition:
        existing = await self.get_by_input_hash(proposed.input_hash)
        if existing is None:
            self.renders[proposed.id] = proposed
            return TypstRenderAcquisition(
                TypstRenderAcquisitionOutcome.ACQUIRED,
                proposed,
            )
        outcome = typst_render_acquisition_outcome(
            existing,
            stale_running_before=stale_running_before,
        )
        if outcome is TypstRenderAcquisitionOutcome.ACQUIRED:
            existing = typst_render_retrying(existing, now=datetime.now(UTC))
            self.renders[existing.id] = existing
        elif outcome is TypstRenderAcquisitionOutcome.IN_PROGRESS:
            if self.in_progress_observed is not None:
                self.in_progress_observed.set()
        return TypstRenderAcquisition(outcome, existing)

    async def reacquire_invalid_succeeded(
        self,
        proposed: PublicationRender,
        *,
        observed_output_blob_id: UUID,
        observed_output_sha256: str,
    ) -> TypstRenderAcquisition:
        existing = self.renders.get(proposed.id)
        if existing is None:
            return await self.acquire_for_render(
                proposed,
                stale_running_before=datetime.min.replace(tzinfo=UTC),
            )
        outcome = invalid_succeeded_reacquisition_outcome(
            existing,
            observed_output_blob_id=observed_output_blob_id,
            observed_output_sha256=observed_output_sha256,
        )
        if outcome is TypstRenderAcquisitionOutcome.ACQUIRED:
            existing = typst_render_retrying(existing, now=datetime.now(UTC))
            self.renders[existing.id] = existing
        return TypstRenderAcquisition(outcome, existing)

    async def add(self, render: PublicationRender) -> PublicationRender:
        if await self.get_by_input_hash(render.input_hash) is not None:
            raise ValueError("duplicate_publication_render_input_hash")
        self.renders[render.id] = render
        return render

    async def mark_succeeded(
        self,
        render_id: UUID,
        *,
        output_blob_id: UUID,
        output_sha256: str,
        output_byte_size: int,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> PublicationRender:
        render = self.renders[render_id]
        if render.status is not TypstRenderStatus.RUNNING:
            raise ValueError("publication_render_not_running")
        updated = replace(
            render,
            source_blob_id=source_blob_id,
            render_data_blob_id=render_data_blob_id,
            output_blob_id=output_blob_id,
            output_sha256=output_sha256,
            output_byte_size=output_byte_size,
            status=TypstRenderStatus.SUCCEEDED,
            error_code=None,
            error_message=None,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = updated
        return updated

    async def mark_failed(
        self,
        render_id: UUID,
        *,
        error_code: str,
        error_message: str | None = None,
        source_blob_id: UUID | None = None,
        render_data_blob_id: UUID | None = None,
    ) -> PublicationRender:
        render = self.renders[render_id]
        if render.status is not TypstRenderStatus.RUNNING:
            raise ValueError("publication_render_not_running")
        updated = replace(
            render,
            source_blob_id=source_blob_id,
            render_data_blob_id=render_data_blob_id,
            status=TypstRenderStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            updated_at=datetime.now(UTC),
        )
        self.renders[render_id] = updated
        return updated


class FakeUnitOfWork:
    def __init__(
        self,
        production_artifacts: FakeProductionArtifacts,
        publication_renders: FakePublicationRenders,
        analyst_decisions: Any | None = None,
    ) -> None:
        self.production_artifacts = production_artifacts
        self.publication_renders = publication_renders
        if analyst_decisions is not None:
            self.analyst_decisions = analyst_decisions

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeUnitOfWorkFactory:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.production_artifacts = FakeProductionArtifacts(artifacts)
        self.publication_renders = FakePublicationRenders()
        self.analyst_decisions: Any | None = None

    def __call__(self) -> FakeUnitOfWork:
        return FakeUnitOfWork(
            self.production_artifacts,
            self.publication_renders,
            self.analyst_decisions,
        )


class FakeArtifactStore:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}
        self.blobs: dict[UUID, bytes] = {}
        self.writes: list[tuple[str, str, UUID, bytes]] = []
        self.read_json_error: Exception | None = None
        self.default_payload = payload

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        if self.read_json_error is not None:
            raise self.read_json_error
        if blob_id in self.payloads:
            return self.payloads[blob_id]
        assert self.default_payload is not None
        return self.default_payload

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        del max_bytes
        return self.blobs[blob_id]

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        blob_id = uuid4()
        self.blobs[blob_id] = content
        self.writes.append((bucket, mime_type, blob_id, content))
        return blob_id


class FakeMediaAssets:
    def __init__(self) -> None:
        self.manifests: dict[UUID, object] = {}
        self.content: dict[UUID, bytes] = {}
        self.read_error: Exception | None = None

    async def get(self, asset_id: UUID) -> object | None:
        return self.manifests.get(asset_id)

    async def read(self, asset_id: UUID) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.content[asset_id]


class FakeRenderer:
    def __init__(self, media_refs: tuple[TypstMediaRef, ...] = ()) -> None:
        self.media_refs = media_refs
        self.call_count = 0

    def render(
        self,
        document: PublicationDocumentV4,
        template_bundle: TypstTemplateBundle,
        *,
        document_label: str,
        target_artifact_identities: dict[str, dict[str, Any]] | None = None,
    ) -> TypstRenderSource:
        del document, document_label, target_artifact_identities
        self.call_count += 1
        source_bytes = next(
            file.content
            for file in template_bundle.files
            if file.relative_path == "RENDERER/publication.typ"
        )
        render_data = b'{"schema_version":"typst-publication-model-v5-dated-references-ioc-groups"}'
        return TypstRenderSource(
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            render_data_bytes=render_data,
            render_data_sha256=hashlib.sha256(render_data).hexdigest(),
            media_refs=self.media_refs,
        )


class FakeCompiler:
    def __init__(
        self,
        failures: tuple[Exception, ...] = (),
        *,
        compile_started: asyncio.Event | None = None,
        release_compile: asyncio.Event | None = None,
    ) -> None:
        self.failures = list(failures)
        self.call_count = 0
        self.requests: list[TypstCompileRequest] = []
        self.workspace_snapshots: list[dict[str, bytes]] = []
        self.font_snapshots: list[dict[str, bytes]] = []
        self.content = b"%PDF-canned-publication"
        self.compile_started = compile_started
        self.release_compile = release_compile

    async def compile(self, request: TypstCompileRequest) -> CompiledTypstDocument:
        self.call_count += 1
        self.requests.append(request)
        self.workspace_snapshots.append(
            {
                str(path.relative_to(request.workspace_root)): path.read_bytes()
                for path in request.workspace_root.rglob("*")
                if path.is_file()
            }
        )
        font_root = Path(os.path.commonpath([str(path) for path in request.font_paths]))
        self.font_snapshots.append(
            {
                path.relative_to(font_root).as_posix(): path.read_bytes()
                for search_root in request.font_paths
                for path in search_root.rglob("*")
                if path.is_file()
            }
        )
        if self.compile_started is not None:
            self.compile_started.set()
        if self.release_compile is not None:
            await self.release_compile.wait()
        if self.failures:
            raise self.failures.pop(0)
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=self.content,
            sha256=hashlib.sha256(self.content).hexdigest(),
            byte_size=len(self.content),
            compiler="typst",
            compiler_version="0.15.1",
        )


def _media_ref(
    *,
    kind: MediaAssetKind,
    asset_id: UUID | None = None,
    sha256: str | None = None,
    mime_type: str = "image/svg+xml",
    byte_size: int | None = None,
) -> TypstMediaRef:
    identifier = asset_id or uuid4()
    extension = ".svg" if mime_type == "image/svg+xml" else ".png"
    return TypstMediaRef(
        asset_id=identifier,
        expected_kind=kind,
        expected_mime_type=mime_type,
        expected_sha256=sha256,
        expected_byte_size=byte_size,
        media_path=f"media/{identifier}{extension}",
    )


def _make_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    media_refs: tuple[TypstMediaRef, ...] = (),
    compiler: FakeCompiler | None = None,
    valid_document: bool = True,
    artifact_status: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED,
    artifact_stage: ProductionArtifactStage = ProductionArtifactStage.PUBLICATION,
    canonical_blob: UUID | None = None,
    missing_canonical: bool = False,
) -> tuple[
    publication_rendering.PublicationRenderService,
    ProductionArtifact,
    FakeUnitOfWorkFactory,
    FakeArtifactStore,
    FakeMediaAssets,
    FakeCompiler,
]:
    document = _document()
    payload = (
        publication_document_v4_to_json(document) if valid_document else {"schema_version": "bad"}
    )
    artifact_store = FakeArtifactStore(payload)
    canonical_blob_id = None if missing_canonical else (canonical_blob or uuid4())
    artifact = ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=document.subject_id,
        stage=artifact_stage,
        version=1,
        input_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        status=artifact_status,
        canonical_blob_id=canonical_blob_id,
    )
    uow_factory = FakeUnitOfWorkFactory({artifact.id: artifact})
    media_assets = FakeMediaAssets()
    renderer = FakeRenderer(media_refs)
    fake_compiler = compiler or FakeCompiler()
    template_root = tmp_path / "chpTypst"
    for relative, content in (
        ("RENDERER/publication.typ", b'#let publication = json("render-data.json")\n'),
        ("UTILS/helpers.typ", b"// renderer helper\n"),
    ):
        path = template_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (template_root / "TEMPLATES").mkdir()
    (template_root / "TEMPLATES" / "reference.typ").write_bytes(b"// not copied\n")
    (template_root / "renderer-manifest.json").write_text(
        json.dumps(
            {
                "template_version": "unit-template-v1",
                "files": ["RENDERER/publication.typ", "UTILS/helpers.typ"],
            }
        ),
        encoding="utf-8",
    )
    font_bundle_root = tmp_path / "typst-fonts"
    font_files = (
        "HankenGrotesk/HankenGrotesk-Regular.ttf",
        "CascadiaCode/ttf/CascadiaCode.ttf",
        "CascadiaCode/ttf/static/CascadiaCode-Regular.ttf",
    )
    for relative_path in font_files:
        font_path = font_bundle_root / relative_path
        font_path.parent.mkdir(parents=True, exist_ok=True)
        font_path.write_bytes(b"test font")
    lock_path = tmp_path / "typst-fonts.lock"
    lock_path.write_text(
        json.dumps({"font_bundle_label": "unit-fonts-v1", "files": list(font_files)}),
        encoding="utf-8",
    )
    service = publication_rendering.PublicationRenderService(
        uow_factory=cast(PublicationRenderUnitOfWorkFactory, uow_factory),
        artifact_store=cast(ProductionArtifactStore, artifact_store),
        media_asset_store=cast(MediaAssetStore, media_assets),
        renderer=cast(TypstRenderer, renderer),
        compiler=fake_compiler,
        chp_typst_root=template_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=lock_path,
    )
    return service, artifact, uow_factory, artifact_store, media_assets, fake_compiler


@pytest.mark.asyncio
async def test_fake_acquire_for_render_decision_table() -> None:
    proposed = _proposed_render(uuid4())
    stale_before = datetime.now(UTC) - timedelta(seconds=30)

    empty = FakePublicationRenders()
    created = await empty.acquire_for_render(proposed, stale_running_before=stale_before)
    assert created.outcome is TypstRenderAcquisitionOutcome.ACQUIRED
    assert created.render == proposed

    now = datetime.now(UTC)
    cases = (
        (
            replace(
                proposed,
                status=TypstRenderStatus.SUCCEEDED,
                output_blob_id=uuid4(),
                output_sha256="b" * 64,
                output_byte_size=1,
            ),
            TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED,
        ),
        (
            replace(
                proposed,
                status=TypstRenderStatus.FAILED,
                error_code="compile_failed",
            ),
            TypstRenderAcquisitionOutcome.ACQUIRED,
        ),
        (
            replace(proposed, updated_at=now),
            TypstRenderAcquisitionOutcome.IN_PROGRESS,
        ),
        (
            replace(proposed, updated_at=now - timedelta(minutes=10)),
            TypstRenderAcquisitionOutcome.ACQUIRED,
        ),
    )
    for existing, expected in cases:
        repository = FakePublicationRenders()
        repository.renders[existing.id] = existing
        acquisition = await repository.acquire_for_render(
            replace(proposed, id=uuid4()),
            stale_running_before=stale_before,
        )
        assert acquisition.outcome is expected
        assert acquisition.render.id == existing.id
        if expected is TypstRenderAcquisitionOutcome.ACQUIRED:
            assert acquisition.render.status is TypstRenderStatus.RUNNING
            assert acquisition.render.error_code is None
            assert acquisition.render.error_message is None


@pytest.mark.asyncio
async def test_fake_reacquire_invalid_succeeded_has_a_single_repair_owner() -> None:
    proposed = _proposed_render(uuid4())
    output_blob_id = uuid4()
    output_sha256 = "c" * 64
    succeeded = replace(
        proposed,
        status=TypstRenderStatus.SUCCEEDED,
        output_blob_id=output_blob_id,
        output_sha256=output_sha256,
        output_byte_size=1,
    )
    repository = FakePublicationRenders()
    repository.renders[succeeded.id] = succeeded
    repair_proposal = replace(succeeded, status=TypstRenderStatus.RUNNING)

    owner = await repository.reacquire_invalid_succeeded(
        repair_proposal,
        observed_output_blob_id=output_blob_id,
        observed_output_sha256=output_sha256,
    )
    waiter = await repository.reacquire_invalid_succeeded(
        repair_proposal,
        observed_output_blob_id=output_blob_id,
        observed_output_sha256=output_sha256,
    )

    assert owner.outcome is TypstRenderAcquisitionOutcome.ACQUIRED
    assert owner.render.status is TypstRenderStatus.RUNNING
    assert waiter.outcome is TypstRenderAcquisitionOutcome.IN_PROGRESS

    replaced_success = replace(
        succeeded,
        output_blob_id=uuid4(),
        output_sha256="d" * 64,
    )
    repository.renders[succeeded.id] = replaced_success
    stale_repair = await repository.reacquire_invalid_succeeded(
        repair_proposal,
        observed_output_blob_id=output_blob_id,
        observed_output_sha256=output_sha256,
    )
    assert stale_repair.outcome is TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED


@pytest.mark.asyncio
async def test_publication_render_service_happy_path_persists_content_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, uow_factory, store, _, compiler = _make_service(tmp_path, monkeypatch)

    render = await service.render_pdf(artifact.id)

    assert render.status is TypstRenderStatus.SUCCEEDED
    assert render.publication_artifact_id == artifact.id
    assert render.source_blob_id is not None
    assert render.render_data_blob_id is not None
    assert render.output_blob_id is not None
    assert render.output_sha256 == hashlib.sha256(compiler.content).hexdigest()
    assert render.output_byte_size == len(compiler.content)
    source_snapshot = json.loads(store.blobs[render.source_blob_id])
    assert source_snapshot["schema_version"] == "typst-render-input-snapshot-v1"
    template_files = {item["relative_path"]: item for item in source_snapshot["files"]}
    assert set(template_files) == {"RENDERER/publication.typ", "UTILS/helpers.typ"}
    publication_entrypoint = template_files["RENDERER/publication.typ"]
    assert base64.b64decode(publication_entrypoint["content_base64"]) == (
        b'#let publication = json("render-data.json")\n'
    )
    assert (
        publication_entrypoint["sha256"]
        == hashlib.sha256(b'#let publication = json("render-data.json")\n').hexdigest()
    )
    assert set(compiler.workspace_snapshots[0]) == {
        "RENDERER/publication.typ",
        "RENDERER/render-data.json",
        "UTILS/helpers.typ",
    }
    assert [write[0] for write in store.writes] == [
        "publication-renders-source",
        "publication-renders-render-data",
        "publication-renders-output",
    ]
    assert compiler.call_count == 1
    assert len(compiler.requests[0].font_paths) == 2
    assert all(not path.exists() for path in compiler.requests[0].font_paths)
    assert compiler.font_snapshots[0] == {
        "HankenGrotesk/HankenGrotesk-Regular.ttf": b"test font",
        "CascadiaCode/ttf/CascadiaCode.ttf": b"test font",
        "CascadiaCode/ttf/static/CascadiaCode-Regular.ttf": b"test font",
    }
    assert uow_factory.publication_renders.renders[render.id] == render


@pytest.mark.asyncio
async def test_publication_render_service_concurrent_calls_compile_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compile_started = asyncio.Event()
    release_compile = asyncio.Event()
    compiler = FakeCompiler(
        compile_started=compile_started,
        release_compile=release_compile,
    )
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path,
        monkeypatch,
        compiler=compiler,
    )
    in_progress_observed = asyncio.Event()
    uow_factory.publication_renders.in_progress_observed = in_progress_observed

    concurrent_calls = asyncio.gather(
        service.render_pdf(artifact.id),
        service.render_pdf(artifact.id),
    )
    await compile_started.wait()
    await in_progress_observed.wait()
    release_compile.set()
    first, second = await concurrent_calls

    assert first == second
    assert first.status is TypstRenderStatus.SUCCEEDED
    assert compiler.call_count == 1
    assert len(uow_factory.publication_renders.renders) == 1


@pytest.mark.asyncio
async def test_publication_render_service_reuses_succeeded_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, _, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_pdf(artifact.id)
    second = await service.render_pdf(artifact.id)

    assert second == first
    assert compiler.call_count == 1


@pytest.mark.asyncio
async def test_publication_render_preview_exposes_and_reuses_the_lifecycle_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, _, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_preview(artifact.id)
    second = await service.render_preview(artifact.id)

    assert first.status is TypstRenderStatus.SUCCEEDED
    assert first.render is not None
    assert second.status is TypstRenderStatus.SUCCEEDED
    assert second.render == first.render
    assert second.render.input_hash == first.render.input_hash
    assert compiler.call_count == 1


@pytest.mark.asyncio
async def test_publication_render_service_keeps_the_pinned_row_when_template_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, _, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_pdf(artifact.id)
    template_file = service._chp_typst_root / "UTILS/helpers.typ"
    template_file.write_bytes(template_file.read_bytes() + b" changed")

    async def fail_if_prepared(_artifact_id: UUID) -> Any:
        pytest.fail("a pinned PDF read must not load the current Typst bundle")

    monkeypatch.setattr(service, "_prepare", fail_if_prepared)
    second = await service.render_pdf(artifact.id)

    assert first == second
    assert first.publication_artifact_id == second.publication_artifact_id == artifact.id
    assert compiler.call_count == 1


@pytest.mark.asyncio
async def test_publication_render_service_keeps_the_pinned_row_when_font_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, _, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_pdf(artifact.id)
    font_file = service._font_bundle_root / "HankenGrotesk/HankenGrotesk-Regular.ttf"
    font_file.write_bytes(font_file.read_bytes() + b" changed")
    second = await service.render_pdf(artifact.id)

    assert first == second
    assert first.publication_artifact_id == second.publication_artifact_id == artifact.id
    assert compiler.call_count == 1


@pytest.mark.asyncio
async def test_render_uses_the_loaded_template_and_font_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, store, _, compiler = _make_service(tmp_path, monkeypatch)
    loaded_templates: list[TypstTemplateBundle] = []
    loaded_fonts: list[FontBundleSnapshot] = []
    original_load_template = publication_rendering.load_template_bundle
    original_load_fonts = publication_rendering.load_font_bundle_snapshot

    def capture_template(root: Path) -> TypstTemplateBundle:
        bundle = original_load_template(root)
        loaded_templates.append(bundle)
        return bundle

    def mutate_sources_after_font_snapshot(root: Path, lock_path: Path) -> FontBundleSnapshot:
        snapshot = original_load_fonts(root, lock_path)
        loaded_fonts.append(snapshot)
        (service._chp_typst_root / "UTILS/helpers.typ").unlink()
        (root / "HankenGrotesk/HankenGrotesk-Regular.ttf").unlink()
        return snapshot

    monkeypatch.setattr(publication_rendering, "load_template_bundle", capture_template)
    monkeypatch.setattr(
        publication_rendering,
        "load_font_bundle_snapshot",
        mutate_sources_after_font_snapshot,
    )

    render = await service.render_pdf(artifact.id)

    template_bundle = loaded_templates[0]
    font_bundle = loaded_fonts[0]
    assert render.template_sha256 == template_bundle.sha256
    assert render.font_bundle_version == font_bundle.font_bundle_version
    assert render.render_policy_version == PUBLICATION_RENDER_POLICY_VERSION
    assert PUBLICATION_RENDER_POLICY_VERSION == "typst-publication-v7-chapsvision-cited-context"
    # C8 intentionally stamps the anchored editorial section layout as v11/v25.
    assert (
        publication_rendering.PUBLICATION_RENDERER_VERSION
        == "publication-v14-typst-v32-literal-notes"
    )
    expected_templates = {file.relative_path: file.content for file in template_bundle.files}
    observed_templates = {
        path: content
        for path, content in compiler.workspace_snapshots[0].items()
        if path in expected_templates
    }
    assert observed_templates == expected_templates
    assert hash_bundle_contents(tuple(observed_templates.items())) == render.template_sha256
    assert compiler.font_snapshots[0] == dict(font_bundle.files)
    assert hash_bundle_contents(tuple(compiler.font_snapshots[0].items())) == (
        font_bundle.font_bundle_version
    )
    assert store.default_payload is not None
    publication_content_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(store.default_payload)
    ).hexdigest()
    assert render.input_hash == compute_publication_render_input_hash(
        publication_artifact_id=artifact.id,
        publication_content_sha256=publication_content_sha256,
        renderer=publication_rendering.PUBLICATION_RENDERER,
        renderer_version=publication_rendering.PUBLICATION_RENDERER_VERSION,
        template_version=template_bundle.template_version,
        template_sha256=template_bundle.sha256,
        compiler=TYPST_COMPILER,
        compiler_version=TYPST_COMPILER_VERSION,
        format=TypstRenderFormat.PDF,
        font_bundle_version=render.font_bundle_version,
        render_policy_version=render.render_policy_version,
    )


@pytest.mark.asyncio
async def test_render_maps_blob_reference_integrity_error_to_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, uow_factory, _, _, _ = _make_service(tmp_path, monkeypatch)

    async def fail_mark_succeeded(*_args: object, **_kwargs: object) -> None:
        raise IntegrityError(
            "UPDATE publication_renders", {}, RuntimeError("foreign key violation")
        )

    monkeypatch.setattr(uow_factory.publication_renders, "mark_succeeded", fail_mark_succeeded)

    with pytest.raises(publication_rendering.PublicationRenderStorageFailedError) as raised:
        await service.render_pdf(artifact.id)

    assert isinstance(raised.value.__cause__, IntegrityError)
    failed = next(iter(uow_factory.publication_renders.renders.values()))
    assert failed.status is TypstRenderStatus.FAILED
    assert failed.error_code == "publication_render_storage_failed"


@pytest.mark.asyncio
async def test_publication_render_service_retries_failed_input_using_same_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = TypstCompileFailedError("initial compile failed")
    compiler = FakeCompiler((failure,))
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path, monkeypatch, compiler=compiler
    )

    with pytest.raises(TypstCompileFailedError) as raised:
        await service.render_pdf(artifact.id)
    failed = next(iter(uow_factory.publication_renders.renders.values()))
    retried = await service.render_pdf(artifact.id)

    assert raised.value is failure
    assert failed.status is TypstRenderStatus.FAILED
    assert failed.error_code == "typst_compile_failed"
    assert retried.id == failed.id
    assert retried.status is TypstRenderStatus.SUCCEEDED
    assert retried.error_code is None
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_publication_render_service_rejects_missing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _artifact, uow_factory, _, _, compiler = _make_service(tmp_path, monkeypatch)

    with pytest.raises(publication_rendering.PublicationRenderArtifactMissingError) as raised:
        await service.render_pdf(uuid4())

    assert raised.value.code == "publication_render_artifact_missing"
    assert not uow_factory.publication_renders.renders
    assert compiler.call_count == 0


@pytest.mark.parametrize(
    ("artifact_stage", "artifact_status", "missing_canonical"),
    (
        (ProductionArtifactStage.EXTRACTION, ProductionArtifactStatus.VERIFIED, False),
        (ProductionArtifactStage.PUBLICATION, ProductionArtifactStatus.STALE, False),
        (ProductionArtifactStage.PUBLICATION, ProductionArtifactStatus.VERIFIED, True),
    ),
)
@pytest.mark.asyncio
async def test_publication_render_service_rejects_invalid_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_stage: ProductionArtifactStage,
    artifact_status: ProductionArtifactStatus,
    missing_canonical: bool,
) -> None:
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path,
        monkeypatch,
        artifact_stage=artifact_stage,
        artifact_status=artifact_status,
        missing_canonical=missing_canonical,
    )

    with pytest.raises(publication_rendering.PublicationRenderArtifactInvalidError) as raised:
        await service.render_pdf(artifact.id)

    assert raised.value.code == "publication_render_artifact_invalid"
    assert not uow_factory.publication_renders.renders
    assert compiler.call_count == 0


@pytest.mark.asyncio
async def test_publication_render_service_maps_document_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path, monkeypatch, valid_document=False
    )

    with pytest.raises(publication_rendering.PublicationRenderDocumentInvalidError) as raised:
        await service.render_pdf(artifact.id)

    assert raised.value.code == "publication_render_document_invalid"
    assert not uow_factory.publication_renders.renders
    assert compiler.call_count == 0


@pytest.mark.asyncio
async def test_publication_render_service_marks_missing_media_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, uow_factory, _, _, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )

    with pytest.raises(publication_rendering.PublicationRenderMediaMissingError) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_missing"
    assert render.status is TypstRenderStatus.FAILED
    assert render.error_code == "publication_render_media_missing"


@pytest.mark.asyncio
async def test_publication_render_service_marks_diagram_kind_mismatch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, uow_factory, _, media_assets, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.SOURCE_FIGURE,
        sha256="a" * 64,
        mime_type="image/png",
        byte_size=8,
    )

    with pytest.raises(publication_rendering.PublicationRenderMediaKindMismatchError) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_kind_mismatch"
    assert render.status is TypstRenderStatus.FAILED
    assert render.error_code == "publication_render_media_kind_mismatch"


@pytest.mark.asyncio
async def test_publication_render_service_writes_resolved_media_to_synthetic_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, _, _, media_assets, compiler = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    content = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.DIAGRAM_SVG,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/svg+xml",
        byte_size=len(content),
    )
    media_assets.content[ref.asset_id] = content

    render = await service.render_pdf(artifact.id)

    assert render.status is TypstRenderStatus.SUCCEEDED
    assert compiler.workspace_snapshots[0][f"RENDERER/{ref.media_path}"] == content


@pytest.mark.asyncio
async def test_publication_render_service_recompiles_if_cached_output_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, _, store, _, compiler = _make_service(tmp_path, monkeypatch)

    first = await service.render_pdf(artifact.id)
    assert first.output_blob_id is not None
    del store.blobs[first.output_blob_id]
    second = await service.render_pdf(artifact.id)

    assert second.id == first.id
    assert second.status is TypstRenderStatus.SUCCEEDED
    assert compiler.call_count == 2


@pytest.mark.asyncio
async def test_publication_render_service_marks_figure_integrity_mismatch_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(
        kind=MediaAssetKind.SOURCE_FIGURE,
        sha256="a" * 64,
        mime_type="image/png",
        byte_size=12,
    )
    service, artifact, uow_factory, _, media_assets, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.SOURCE_FIGURE,
        sha256="b" * 64,
        mime_type="image/jpeg",
        byte_size=13,
    )

    with pytest.raises(
        publication_rendering.PublicationRenderMediaIntegrityMismatchError
    ) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_integrity_mismatch"
    assert render.status is TypstRenderStatus.FAILED
    assert render.error_code == "publication_render_media_integrity_mismatch"


@pytest.mark.asyncio
async def test_publication_render_service_maps_media_blob_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _media_ref(kind=MediaAssetKind.DIAGRAM_SVG)
    service, artifact, uow_factory, _, media_assets, _ = _make_service(
        tmp_path, monkeypatch, media_refs=(ref,)
    )
    media_assets.manifests[ref.asset_id] = SimpleNamespace(
        asset_id=ref.asset_id,
        kind=MediaAssetKind.DIAGRAM_SVG,
        sha256="a" * 64,
        mime_type="image/svg+xml",
        byte_size=8,
    )
    media_assets.read_error = BlobIntegrityError("digest mismatch")

    with pytest.raises(
        publication_rendering.PublicationRenderMediaIntegrityMismatchError
    ) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value.code == "publication_render_media_integrity_mismatch"
    assert render.status is TypstRenderStatus.FAILED


@pytest.mark.parametrize(
    "error_type",
    (
        TypstCompilerUnavailableError,
        TypstCompilerVersionMismatchError,
        TypstCompileTimeoutError,
        TypstCompileFailedError,
        TypstOutputTooLargeError,
        TypstOutputInvalidError,
    ),
)
@pytest.mark.asyncio
async def test_publication_render_service_persists_typed_compiler_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[TypstCompilationError],
) -> None:
    failure = error_type("compiler failure")
    compiler = FakeCompiler((failure,))
    service, artifact, uow_factory, _, _, compiler = _make_service(
        tmp_path, monkeypatch, compiler=compiler
    )

    with pytest.raises(error_type) as raised:
        await service.render_pdf(artifact.id)

    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert raised.value is failure
    assert render.status is TypstRenderStatus.FAILED
    assert render.error_code == failure.code
    assert render.source_blob_id is not None
    assert render.render_data_blob_id is not None
    assert compiler.call_count == 1
    assert not compiler.requests[0].workspace_root.exists()
    assert all(not path.exists() for path in compiler.requests[0].font_paths)


@pytest.mark.asyncio
async def test_font_materialization_failure_maps_to_storage_error_and_cleans_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, artifact, uow_factory, _, _, _ = _make_service(tmp_path, monkeypatch)
    attempted_paths: list[Path] = []

    def fail_materialization(
        snapshot: FontBundleSnapshot, destination_root: Path
    ) -> tuple[Path, ...]:
        del snapshot
        attempted_paths.append(destination_root)
        raise OSError("font copy failed")

    monkeypatch.setattr(typst_render_execution, "materialize_font_bundle", fail_materialization)

    with pytest.raises(publication_rendering.PublicationRenderStorageFailedError):
        await service.render_pdf(artifact.id)

    assert len(attempted_paths) == 1
    assert not attempted_paths[0].exists()
    render = next(iter(uow_factory.publication_renders.renders.values()))
    assert render.status is TypstRenderStatus.FAILED
    assert render.error_code == "publication_render_storage_failed"
