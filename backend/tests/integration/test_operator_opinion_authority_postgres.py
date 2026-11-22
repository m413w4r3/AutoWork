"""PostgreSQL proof of the ADR-0011 D4 Bridge-only prose-authority gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    OpinionCompositionSelection,
    OpinionError,
    OpinionErrorCode,
    OpinionRevisionEdits,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.analyst_opinion import (
    AnalystOpinionV1,
    BridgeDraftReferenceV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionWriterKind,
    investigation_opinion_id,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    TLPLevel,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProseAuthority,
)
from cti_app.domain.production_synthesis import (
    production_synthesis_to_json,
)
from cti_app.domain.publication_document import (
    serialize_publication_document,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_builder_v4 import _canonical_inputs
from tests.test_publication_document_v6 import _base_v5

pytestmark = pytest.mark.integration


class _Store:
    def __init__(self, uow_factory: Any) -> None:
        self._uow_factory = uow_factory
        self.blobs: dict[UUID, bytes] = {}

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        content = self.blobs[blob_id]
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("Blob exceeds the requested read limit")
        return content

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        sha256 = hashlib.sha256(content).hexdigest()
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=sha256,
                size=len(content),
                mime_type=mime_type,
                logical_bucket=bucket,
            )
        )
        async with self._uow_factory() as uow:
            await uow.blobs.add(record)
            await uow.commit()
        self.blobs[record.id] = content
        return record.id


class _LedgerService(InvestigationService):
    """Minimal real-repository loop service for the BRIDGE_LED writer oracle."""

    def __init__(self, uow: SqlAlchemyUnitOfWork) -> None:
        super().__init__(
            post_synthesis=SimpleNamespace(),
            investigations=uow.analyst_investigations,
            states=SimpleNamespace(),
            context_snapshots=SimpleNamespace(),
            envelopes=SimpleNamespace(),
            context_builder=SimpleNamespace(),
            bridge=SimpleNamespace(),
            registry=SimpleNamespace(),
            dispatcher=SimpleNamespace(),
            invocations=SimpleNamespace(),
            observations=SimpleNamespace(),
            findings=SimpleNamespace(),
            opinions=SimpleNamespace(),
        )


def _bridge_call_payload(text: str) -> tuple[dict[str, Any], bytes, str]:
    invocation_id = str(uuid4())
    output_sha256 = hashlib.sha256(f"pg-bridge-output:{invocation_id}".encode()).hexdigest()
    output = {
        "schema": "advisor-response.v3",
        "usable": True,
        "output_sha256": output_sha256,
        "blocks": [
            {
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "form": "body",
                "anchor": None,
                "usable": True,
                "sentences": [{"text": text}],
            }
        ],
        "bridge_provenance": {
            "bridge_invocation_id": invocation_id,
            "bridge_invocation_id_kind": "conversation_turn",
            "output_sha256": output_sha256,
            "input_refs": [],
        },
    }
    payload = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": "draft_text",
        "advisor_status": "succeeded",
        "bridge_invocation_id": invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "output_sha256": output_sha256,
        "input_refs": [],
        "output": output,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return payload, raw, invocation_id


@pytest.mark.asyncio
async def test_bridge_only_opinion_authority_is_enforced_on_postgres(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    base_document = _base_v5()
    subject_id = uuid4()
    base_document = replace(
        base_document,
        document=replace(base_document.document, subject_id=subject_id),
    )
    base_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(base_document)
    )
    _, _, _, synthesis = _canonical_inputs()
    synthesis = replace(
        synthesis,
        subject_id=subject_id,
        title=base_document.document.title,
        publication_language=base_document.document.publication_language,
    )
    synthesis_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_synthesis_to_json(synthesis)
    )
    observation_bytes = b"harmless integration observation evidence"
    suffix = uuid4().hex
    edition = Edition(
        country=f"Opinion authority {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        id=subject_id,
        edition_id=edition.id,
        title="Bridge-only opinion authority fixture",
        slug=f"bridge-only-opinion-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    store = _Store(uow_factory)
    base_blob_id = await store.put_bytes(
        base_bytes, bucket="analyst-opinion-base", mime_type="application/json"
    )
    synthesis_blob_id = await store.put_bytes(
        synthesis_bytes, bucket="analyst-opinion-synthesis", mime_type="text/plain"
    )
    observation_blob_id = await store.put_bytes(
        observation_bytes, bucket="analyst-opinion-observation", mime_type="text/plain"
    )
    base_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=hashlib.sha256(synthesis_bytes).hexdigest(),
        canonical_blob_id=synthesis_blob_id,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=synthesis_artifact.id,
        budget=LoopBudget(),
        prose_authority=ProseAuthority.BRIDGE_ONLY,
    )
    observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=observation_blob_id,
        sha256=hashlib.sha256(observation_bytes).hexdigest(),
        source="fixture-static-analysis",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sha256",
        object_id=hashlib.sha256(b"harmless binary identity").hexdigest(),
        limitations=("Static observation only.",),
    )
    finding = InvestigationFinding(
        investigation_id=investigation.id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The fixture contains a second-stage loading capability.",
        observation_refs=(observation.id,),
        limitations=("Execution was not observed.",),
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation.id,
        allowed_providers=(),
        budget=InvestigationBudget(limits={}),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.CLEAR, do_not_submit=False),
        approval_decision_id=uuid4(),
        approval_actor_id="editor:integration",
    )
    plain_text = "Un texte analytique humain sans provenance Bridge."
    bridge_text = "Le comportement mesuré correspond au finding cité."
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
            await uow.production_runs.add(run)
            await uow.production_artifacts.append(synthesis_artifact)
            await uow.production_artifacts.append(base_artifact)
            await uow.analyst_investigations.add(investigation)
            await uow.evidence_observations.append_if_absent(observation)
            await uow.investigation_findings.append_version(finding, expected_previous_id=None)
            await uow.investigation_envelopes.save_if_version(envelope, expected_version=None)
            await uow.commit()

        service = AnalystOpinionService(
            uow_factory=uow_factory,
            artifact_store=store,  # type: ignore[arg-type]
        )
        plain_opinion = AnalystOpinionV1(
            id=investigation_opinion_id(investigation.id),
            version=1,
            investigation_id=investigation.id,
            synthesis_artifact_id=synthesis_artifact.id,
            synthesis_sha256=hashlib.sha256(synthesis_bytes).hexdigest(),
            base_publication_artifact_id=base_artifact.id,
            base_publication_sha256=hashlib.sha256(base_bytes).hexdigest(),
            blocks=(
                OpinionBlock(
                    block_type=OpinionBlockType.KEY_FINDINGS,
                    text=plain_text,
                    finding_refs=(finding.id,),
                    evidence_refs=(observation.id,),
                    qualification=OpinionClaimQualification.FACT,
                    certainty=0.8,
                ),
            ),
            author_id="editor:seed",
        )
        async with uow_factory() as uow:
            await uow.analyst_opinions.append_version(plain_opinion, expected_version=0)
            await uow.commit()

        with pytest.raises(OpinionError) as approve_refused:
            await service.approve(
                plain_opinion.id,
                plain_opinion.version,
                plain_opinion.content_sha256,
                "editor:reviewer",
                "Attempted approval of non-Bridge prose.",
                bridge_drafting_gap_acknowledged=True,
            )
        assert approve_refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED

        with pytest.raises(OpinionError) as changes_refused:
            await service.request_changes(
                plain_opinion.id,
                plain_opinion.version,
                plain_opinion.content_sha256,
                "editor:reviewer",
                "Attempted copy of non-Bridge prose.",
            )
        assert changes_refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED

        _, raw_call, invocation_id = _bridge_call_payload(bridge_text)
        call_blob_id = await store.put_bytes(
            raw_call, bucket="model-outputs", mime_type="application/json"
        )
        call_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=call_blob_id,
            sha256=hashlib.sha256(raw_call).hexdigest(),
            source="advisor.consult",
            source_version="advisor-consult.v1",
            observed_at=datetime.now(UTC),
            object_type="advisor_call",
            object_id="draft_text",
        )
        async with uow_factory() as uow:
            await uow.evidence_observations.append_if_absent(call_observation)
            await uow.commit()

        bridge_block = OpinionBlock(
            block_type=OpinionBlockType.KEY_FINDINGS,
            text=bridge_text,
            finding_refs=(finding.id,),
            evidence_refs=(observation.id,),
            qualification=OpinionClaimQualification.FACT,
            certainty=0.8,
            bridge_draft_ref=BridgeDraftReferenceV1(
                invocation_id,
                hashlib.sha256(bridge_text.encode()).hexdigest(),
                None,
                call_observation.id,
            ),
        )
        composed = await service.compose_from_findings(
            investigation.id,
            OpinionCompositionSelection(),
            draft_blocks=(bridge_block,),
        )
        assert composed.version == 2
        assert composed.blocks[0].bridge_draft_ref is not None

        with pytest.raises(OpinionError) as revise_refused:
            await service.revise(
                composed.id,
                composed.version,
                OpinionRevisionEdits(
                    (
                        OpinionBlock(
                            block_type=OpinionBlockType.KEY_FINDINGS,
                            text="Réécriture humaine hors provenance Bridge.",
                            finding_refs=(finding.id,),
                            evidence_refs=(observation.id,),
                            qualification=OpinionClaimQualification.FACT,
                            certainty=0.8,
                        ),
                    )
                ),
                "editor:human",
                expected_sha256=composed.content_sha256,
            )
        assert revise_refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED

        with pytest.raises(OpinionError) as operator_refused:
            await service.revise(
                composed.id,
                composed.version,
                OpinionRevisionEdits((bridge_block,)),
                "operator:claude-client",
                expected_sha256=composed.content_sha256,
                writer_kind=OpinionWriterKind.OPERATOR_TOOL,
            )
        assert operator_refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED

        approved = await service.approve(
            composed.id,
            composed.version,
            composed.content_sha256,
            "editor:reviewer",
            "Approved exact Bridge prose.",
            bridge_drafting_gap_acknowledged=True,
        )
        assert approved.version == composed.version + 1
        assert approved.status.value == "approved"

        async with uow_factory() as uow:
            ledger_service = _LedgerService(uow)
            with pytest.raises(OpinionError) as bridge_led_refused:
                await ledger_service._apply_opinion_delta(
                    investigation_id=investigation.id,
                    state=SimpleNamespace(
                        base_publication_artifact_id=base_artifact.id,
                        base_publication_sha256=hashlib.sha256(base_bytes).hexdigest(),
                    ),
                    delta=SimpleNamespace(blocks=(), notes=(), unresolved_points=()),
                )
        assert bridge_led_refused.value.code is OpinionErrorCode.BRIDGE_AUTHORSHIP_REQUIRED
    finally:
        await engine.dispose()
