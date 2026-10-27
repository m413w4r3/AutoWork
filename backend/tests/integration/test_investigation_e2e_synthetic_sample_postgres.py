from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_audit_export import (
    load_audit_events,
    write_audit_bundle,
)
from cti_app.application.investigation_governance import (
    InvestigationActor,
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.sample_acquisition import (
    SAMPLES_QUARANTINE_BUCKET,
    SampleAcquisitionPolicy,
    VirusTotalSampleAcquisitionService,
)
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.application.virustotal import VirusTotalCapabilities
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import (
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleState
from cti_app.domain.entities import (
    SampleOrigin as EntitySampleOrigin,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationFinding,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
)
from cti_app.domain.investigation_state import Hypothesis
from cti_app.domain.production import (
    ProductionArtifactStage,
    SampleAcquisitionReason,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.database.investigation_audit_reader import (
    read_investigation_audit_snapshot,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler as _TypstCompiler
from tests.workflow_emulator.investigation_support import (
    SyntheticSampleMaterial,
    build_investigation_app,
    composition_factory,
    seed_verified_article,
)

pytestmark = pytest.mark.integration

_SAMPLE_MIME = "application/octet-stream"


class _IdentityProvider:
    def __init__(self) -> None:
        self.identity = Identity("editor:m05c-human", frozenset({IdentityScope.HUMAN_OPERATOR}))

    async def current(self) -> Identity:
        return self.identity


class _HashCheckedPort:
    """The sole local download seam used by the real acquisition service."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0
        self.downloaded_hashes: list[str] = []

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        assert hashlib.sha256(self.payload).hexdigest() == file_hash
        self.calls += 1
        self.downloaded_hashes.append(file_hash)
        sink.write(self.payload)


class _PostgresEvidenceRecorder:
    """Use a fresh real PostgreSQL evidence repository for each recorder call."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def record_existing_blob(self, **kwargs: Any) -> Any:
        async with self._uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(
                self._artifact_store,
                uow.evidence_observations,
            )
            observation = await recorder.record_existing_blob(**kwargs)
            await uow.commit()
            return observation


class _FakeAdvisorTransport:
    """Records local Advisor submissions and returns one fixed synthetic response."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = ""

    async def submit(self, **kwargs: Any) -> TurnSubmission:
        self.calls.append(dict(kwargs))
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=self.response,
            bridge_invocation_id=str(uuid4()),
        )


class _FakeLocalMeasurements:
    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, payload: bytes, sample_sha256: str) -> dict[str, Any]:
        self.calls += 1
        assert hashlib.sha256(payload).hexdigest() == sample_sha256
        assert b"NO-EXECUTION" in payload
        return {
            "schema": "m05c.synthetic-measurement.v1",
            "sample_sha256": sample_sha256,
            "byte_length": len(payload),
            "mime_type": _SAMPLE_MIME,
            "measurements": [
                {
                    "kind": "inert_fixture_marker",
                    "value": "NO-EXECUTION",
                    "present": True,
                }
            ],
            "limitations": ["Deterministic test measurement; no sample execution."],
        }


def _advisor_draft_response(assertion_id: str) -> str:
    return json.dumps(
        {
            "schema": "advisor-response.v3",
            "consultation_type": "draft_text",
            "status": "succeeded",
            "language": "fr",
            "blocks": [
                {
                    "block_type": "key_findings",
                    "form": "body",
                    "anchor": None,
                    "sentences": [
                        {
                            "text": (
                                "Le fixture synthétique persistant enregistre son empreinte "
                                "déclarée."
                            ),
                            "assertion_ids": [assertion_id],
                        }
                    ],
                }
            ],
            "limitations": [],
            "untrusted": True,
        },
        ensure_ascii=False,
    )


async def _persist_observation(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    *,
    investigation_id: UUID,
    sample_sha256: str,
    payload: dict[str, Any],
    source: str,
    source_version: str,
    idempotency_key: str,
) -> EvidenceObservation:
    raw = ProductionArtifactStore.canonical_json_bytes(payload)
    blob_id = await artifact_store.put_bytes(
        raw,
        bucket="investigation-evidence",
        mime_type="application/json",
    )
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=blob_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        source=source,
        source_version=source_version,
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id=sample_sha256,
        limitations=("Synthetic fixture only; no execution or external analysis.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    return observation


async def _persist_policy_sample(
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    *,
    investigation_id: UUID,
    subject_id: UUID,
    label: str,
    tlp: TLP = TLP.CLEAR,
    do_not_submit: bool = False,
    external_llm_allowed: bool = True,
    license_restriction: str | None = None,
) -> tuple[Sample, SampleAssociation, EvidenceObservation, InvestigationFinding]:
    payload = f"AUTOWORK M05C inert policy fixture {label}\n".encode()
    sample_hash = hashlib.sha256(payload).hexdigest()
    blob_id = await artifact_store.put_bytes(
        payload,
        bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type=_SAMPLE_MIME,
    )
    sample = Sample(
        subject_id=subject_id,
        blob_id=blob_id,
        original_name=f"m05c-policy-{label}.txt",
        origin="M05-C inert policy fixture",
        acquired_at=datetime.now(UTC),
        license_restriction=license_restriction,
        tlp=tlp,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_llm_allowed,
        origin_kind=EntitySampleOrigin.MANUAL,
        state=SampleState.QUARANTINED,
        source_service="m05c-policy-fixture",
        source_object_id=sample_hash,
        expected_hash=sample_hash,
    )
    association = SampleAssociation(
        investigation_id=investigation_id,
        sample_sha256=sample_hash,
        origin=SampleOrigin.MANUAL,
    )
    observation_payload = {
        "schema": "m05c.policy-observation.v1",
        "sample_sha256": sample_hash,
        "byte_length": len(payload),
        "mime_type": _SAMPLE_MIME,
        "measurement": "inert UTF-8 fixture persisted for D7 policy coverage",
    }
    observation = await _persist_observation(
        uow_factory,
        artifact_store,
        investigation_id=investigation_id,
        sample_sha256=sample_hash,
        payload=observation_payload,
        source="sample.m05c_policy_fixture",
        source_version="m05c-policy-observation.v1",
        idempotency_key=f"m05c-policy-observation:{label}",
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement=f"The inert {label} policy fixture is associated with its stored observation.",
        observation_refs=(observation.id,),
        limitations=("Policy-control fixture; it is not an analyzed threat sample.",),
    )
    async with uow_factory() as uow:
        await uow.samples.add(sample)
        await uow.sample_associations.append_version(association, expected_version=0)
        await uow.investigation_findings.append_version(finding, expected_previous_id=None)
        await uow.provenance.append(
            ProvenanceEvent(
                aggregate_type="sample",
                aggregate_id=sample.id,
                subject_id=subject_id,
                event_type="m05c_synthetic_policy_fixture_persisted",
                payload={"sample_sha256": sample_hash, "policy_fixture": label},
                tlp=tlp,
                actor_id="test:m05c-fixture",
            )
        )
        await uow.commit()
    await SampleQualificationService(uow_factory).qualify(
        association.id,
        SampleQualification.CONFIRMED,
        InvestigationActor("editor:m05c-reviewer", QualificationActorKind.HUMAN),
        "Persisted synthetic policy fixture reviewed for the D7 veto matrix.",
        (observation.id,),
        correlation_id=f"m05c-policy-g3:{label}",
    )
    return sample, association, observation, finding


async def _pdf_text(artifact_store: ProductionArtifactStore, blob_id: UUID, path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    assert pdftotext is not None, "Poppler pdftotext is required for the real PDF assertion"
    pdf_bytes = await artifact_store.read_bytes(blob_id)
    await asyncio.to_thread(path.write_bytes, pdf_bytes)
    assert pdf_bytes.startswith(b"%PDF-")
    assert hashlib.sha256(pdf_bytes).hexdigest()
    result = await asyncio.to_thread(
        subprocess.run,
        (pdftotext, "-layout", str(path), "-"),
        capture_output=True,
        check=True,
        timeout=30,
    )
    return " ".join(result.stdout.decode("utf-8", errors="replace").split())


@pytest.mark.asyncio
async def test_investigation_e2e_synthetic_sample_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    typed_uow_factory = cast(UnitOfWorkFactory, uow_factory)
    blob_root = (tmp_path / "m05c-isolated-blob-store").resolve()
    catalog = BlobCatalogService(FilesystemBlobStore(blob_root), typed_uow_factory)
    artifact_store = ProductionArtifactStore(catalog)
    transport = _FakeAdvisorTransport()
    identity_provider = _IdentityProvider()
    settings = Settings(
        _env_file=None,
        investigation_max_cycles=12,
        openai_bridge_min_submission_interval_seconds=0,
        openai_bridge_submission_jitter_fraction=0,
        local_analysis_enabled=False,
        rule_workbench_enabled=False,
    )
    composition = composition_factory(
        uow_factory=typed_uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=cast(Any, transport),
    )
    app, _, _ = build_investigation_app(
        uow_factory=typed_uow_factory,
        artifact_store=artifact_store,
        composition_factory=composition,
    )
    app.state.identity_provider = identity_provider
    suffix = uuid4().hex[:10]

    try:
        seeded = await seed_verified_article(
            typed_uow_factory,
            artifact_store,
            suffix=suffix,
            include_seed=True,
            article_claim=(
                "The synthetic article reports an inert sample marker and no external "
                "network behavior."
            ),
        )
        investigation_id = seeded.investigation.id
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = await client.post(
                f"/api/subjects/{seeded.subject.id}/investigations",
                json={
                    "production_run_id": str(seeded.run.id),
                    "synthesis_artifact_id": str(seeded.synthesis.id),
                    "synthesis_sha256": seeded.synthesis.input_hash,
                    "execution_mode": "operator_led",
                },
            )
            assert started.status_code == 200, started.text
            assert started.json()["investigation"]["envelope_approved"] is False

            envelope_service = InvestigationEnvelopeService(typed_uow_factory, settings=settings)
            approved = await envelope_service.approve(
                InvestigationEnvelopeProposal(
                    investigation_id=investigation_id,
                    allowed_providers=("bridge", "vt"),
                    budget=InvestigationBudget(
                        limits={
                            BudgetDimension.DOWNLOADS: 1,
                            BudgetDimension.VT_UNITS: 1,
                            BudgetDimension.BRIDGE_TURNS: 12,
                            BudgetDimension.TOOL_SECONDS: (
                                9 * settings.investigation_max_tool_seconds
                            ),
                        }
                    ),
                    acquire_hits_allowed=True,
                    disclosure=DisclosureBounds(
                        tlp=TLPLevel.CLEAR,
                        do_not_submit=False,
                        external_llm_allowed=True,
                    ),
                ),
                actor_id="editor:m05c-g2-approver",
                reason="Approve one bounded local fake-provider download of inert synthetic bytes.",
                correlation_id=f"m05c-g2:{investigation_id}",
            )

            material = SyntheticSampleMaterial.unique()
            # The real service receives only this local hash-checking download port.
            provider = _HashCheckedPort(material.sample_bytes)
            download_key = f"m05c.synthetic.download:{investigation_id}:{material.sample_hash}"
            acquisition = VirusTotalSampleAcquisitionService(
                provider,
                VirusTotalCapabilities(file_download=True),
                catalog,
                typed_uow_factory,
                LocalIdentityProvider(),
            )
            hit_acquisition = HitAcquisitionService(
                typed_uow_factory,
                acquisition=acquisition,
                recorder=cast(Any, _PostgresEvidenceRecorder(typed_uow_factory, artifact_store)),
            )
            association = await hit_acquisition.register_hit(
                investigation_id,
                material.sample_hash,
                SampleOrigin.VT_PIVOT,
            )
            acquisition_result = await hit_acquisition.acquire_hit(
                association.id,
                purpose="Acquire the hash-checked inert fixture through the real VT service.",
                operation_key=download_key,
            )
            assert acquisition_result.status.value == "succeeded"
            assert acquisition_result.sample_id is not None
            assert acquisition_result.reused_existing_sample is False
            assert acquisition_result.association.id == association.id
            assert acquisition_result.budget_consumed == {
                BudgetDimension.VT_UNITS: 1,
                BudgetDimension.DOWNLOADS: 1,
            }
            assert provider.calls == 1
            assert provider.downloaded_hashes == [material.sample_hash]
            sample_bytes = material.sample_bytes
            assert hashlib.sha256(sample_bytes).hexdigest() == material.sample_hash
            assert sample_bytes.startswith(b"MZ")
            assert b"NO-EXECUTION" in sample_bytes

            async with typed_uow_factory() as uow:
                sample = await uow.samples.get(acquisition_result.sample_id)
                stored_association = await uow.sample_associations.get(association.id)
                stored_envelope = await uow.investigation_envelopes.get(investigation_id)
                stored_investigation = await uow.analyst_investigations.get(investigation_id)
                successful_attempt = await uow.sample_acquisition_attempts.find_successful(
                    investigation_id, material.sample_hash
                )
                provenance = await uow.provenance.list_for_aggregate(
                    "sample", acquisition_result.sample_id
                )
                stored_blob = await uow.blobs.get(sample.blob_id) if sample is not None else None
            assert sample is not None
            assert stored_envelope is not None
            assert stored_envelope.approval_decision_id == approved.approval_decision_id
            assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
            assert stored_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 1
            assert stored_investigation is not None
            assert stored_investigation.budget.consumed_vt_read_units == 1
            assert stored_investigation.budget.consumed_hits_acquired == 1
            assert sample.do_not_submit is True
            assert sample.external_llm_allowed is True
            assert sample.state is SampleState.REVIEW_CANDIDATE
            assert stored_association is not None
            assert stored_association.id == association.id
            assert stored_association.qualification is SampleQualification.CANDIDATE
            assert stored_blob is not None
            descriptor = stored_blob.descriptor
            assert descriptor.logical_bucket == SAMPLES_QUARANTINE_BUCKET
            assert descriptor.object_key == (
                f"samples-quarantine/{material.sample_hash[:2]}/{material.sample_hash}"
            )
            assert descriptor.sha256 == material.sample_hash
            assert descriptor.size == len(sample_bytes)
            assert descriptor.mime_type == "application/octet-stream"
            assert (blob_root / descriptor.object_key).is_file()
            assert await artifact_store.read_bytes(sample.blob_id, max_bytes=len(sample_bytes)) == (
                sample_bytes
            )
            assert successful_attempt is not None and successful_attempt.sample_id == sample.id
            assert [item.event_type for item in provenance].count(
                "sample_acquired_from_virustotal"
            ) == 1

            measurements = _FakeLocalMeasurements()
            archived_sample_bytes = await artifact_store.read_bytes(
                sample.blob_id, max_bytes=len(sample_bytes)
            )
            measurement_payload = measurements.analyze(archived_sample_bytes, material.sample_hash)
            analysis_observation = await _persist_observation(
                typed_uow_factory,
                artifact_store,
                investigation_id=investigation_id,
                sample_sha256=material.sample_hash,
                payload=measurement_payload,
                source="sample.m05c_fake_local_analysis",
                source_version="m05c-fake-local-analysis.v1",
                idempotency_key=f"m05c-local-analysis:{investigation_id}:{material.sample_hash}",
            )
            primary_finding = InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.CAPABILITY,
                assessment=FindingAssessment.SUPPORTED,
                statement="The inert synthetic sample has one hash-verified UTF-8 measurement.",
                observation_refs=(analysis_observation.id,),
                limitations=("A deterministic text measurement; no executable behavior tested.",),
            )
            async with typed_uow_factory() as uow:
                await uow.investigation_findings.append_version(
                    primary_finding, expected_previous_id=None
                )
                await uow.commit()
            qualified = await SampleQualificationService(typed_uow_factory).qualify(
                association.id,
                SampleQualification.CONFIRMED,
                InvestigationActor("editor:m05c-human-reviewer", QualificationActorKind.HUMAN),
                "Confirm the synthetic sample association from its hash-verified "
                "local observation.",
                (analysis_observation.id,),
                correlation_id=f"m05c-g3:{association.id}",
            )
            assert qualified.qualification is SampleQualification.CONFIRMED

            async with typed_uow_factory() as uow:
                current_state = await uow.investigation_states.get(investigation_id)
                assert current_state is not None
                state_with_hypothesis = replace(
                    current_state,
                    version=current_state.version + 1,
                    hypotheses=(
                        Hypothesis(
                            id="M05C-H1",
                            statement=(
                                "The associated sample's NO-EXECUTION marker is consistent "
                                "with inert fixture bytes."
                            ),
                            evidence_refs=(analysis_observation.id,),
                            finding_refs=(primary_finding.id,),
                        ),
                    ),
                )
                await uow.investigation_states.save_if_version(
                    state_with_hypothesis, expected_version=current_state.version
                )
                await uow.commit()

            async with typed_uow_factory() as uow:
                stored_analysis = await uow.evidence_observations.get(analysis_observation.id)
                assert stored_analysis is not None
                analysis_raw = await artifact_store.read_bytes(
                    stored_analysis.blob_id, max_bytes=64 * 1024
                )
                analysis_value = json.loads(analysis_raw)
                assert analysis_value["sample_sha256"] == material.sample_hash
                assert analysis_value["byte_length"] == len(sample_bytes)
                assert analysis_value["mime_type"] == _SAMPLE_MIME
                assert analysis_value["measurements"]
                assert stored_analysis.object_id == material.sample_hash
                assert hashlib.sha256(analysis_raw).hexdigest() == stored_analysis.sha256
            assert measurements.calls == 1

            identity_provider.identity = Identity(
                "operator:m05c-client", frozenset({IdentityScope.OPERATOR_CLIENT})
            )

            async def invoke_advisor(
                arguments: dict[str, Any], *, key: str
            ) -> tuple[httpx.Response, dict[str, Any]]:
                async with typed_uow_factory() as uow:
                    state = await uow.investigation_states.get(investigation_id)
                    assert state is not None
                    expected_version = state.version
                response = await client.post(
                    f"/api/investigations/{investigation_id}/operator/tools/advisor.consult",
                    headers={"Idempotency-Key": key},
                    json={
                        "schema_version": "advisor-consult.v1",
                        "arguments": arguments,
                        "purpose": "Exercise the local D7 Advisor boundary with inert fixtures.",
                        "expected_version": expected_version,
                    },
                )
                assert response.status_code == 200, response.text
                body = response.json()
                return response, body

            (
                advisor_sample,
                _advisor_association,
                advisor_observation,
                advisor_finding,
            ) = await _persist_policy_sample(
                typed_uow_factory,
                artifact_store,
                investigation_id=investigation_id,
                subject_id=seeded.subject.id,
                label="advisor-allowed",
            )
            advisor_assertion_id = "m05c_advisor_fixture_hash"
            transport.response = _advisor_draft_response(advisor_assertion_id)
            successful_before = len(transport.calls)
            _allowed_response, allowed = await invoke_advisor(
                {
                    "consultation_type": "draft_text",
                    "question": "Review the persisted permissive synthetic policy fixture.",
                    "evidence_refs": [str(advisor_observation.id)],
                    "assertions": [
                        {
                            "id": advisor_assertion_id,
                            "text": (
                                "Le fixture synthétique persistant enregistre son empreinte "
                                "déclarée."
                            ),
                            "nature": "analyse",
                            "refs": [
                                {
                                    "kind": "observation",
                                    "id": str(advisor_observation.id),
                                }
                            ],
                            "block_type": "key_findings",
                            "form": "body",
                            "anchor": None,
                        }
                    ],
                },
                key=f"m05c-advisor-allowed:{investigation_id}",
            )
            assert allowed["advisor_output"] is not None, {
                "invocation": allowed["invocation"],
                "output": allowed["output"],
                "fake_send_count": len(transport.calls),
            }
            assert allowed["advisor_output"]["advisor_status"] == "succeeded", {
                key: allowed["advisor_output"].get(key)
                for key in (
                    "validation_rejection",
                    "response_excerpt",
                    "parse_status",
                    "failure_stage",
                    "failure_reason",
                )
            }
            assert len(transport.calls) - successful_before == 1
            archived_prompt_id = UUID(allowed["advisor_output"]["prompt_blob_id"])
            sent_prompt = transport.calls[-1]["prompt"].encode("utf-8")
            archived_prompt = await artifact_store.read_bytes(archived_prompt_id)
            assert sent_prompt == archived_prompt
            assert (
                hashlib.sha256(sent_prompt).hexdigest()
                == allowed["advisor_output"]["prompt_sha256"]
            )
            assert (
                hashlib.sha256(archived_prompt).hexdigest()
                == hashlib.sha256(sent_prompt).hexdigest()
            )
            assert advisor_sample.expected_hash in transport.calls[-1]["prompt"]

            async def assert_sample_veto(
                *,
                label: str,
                tlp: TLP = TLP.CLEAR,
                do_not_submit: bool = False,
                external_llm_allowed: bool = True,
                license_restriction: str | None = None,
                expected_source: str,
            ) -> None:
                (
                    _policy_sample,
                    policy_association,
                    observation,
                    finding,
                ) = await _persist_policy_sample(
                    typed_uow_factory,
                    artifact_store,
                    investigation_id=investigation_id,
                    subject_id=seeded.subject.id,
                    label=label,
                    tlp=tlp,
                    do_not_submit=do_not_submit,
                    external_llm_allowed=external_llm_allowed,
                    license_restriction=license_restriction,
                )
                before = len(transport.calls)
                _, result = await invoke_advisor(
                    {
                        "consultation_type": "draft_text",
                        "question": "Review this persisted inert policy sample.",
                        "evidence_refs": [str(observation.id)],
                    },
                    key=f"m05c-advisor-veto:{label}:{uuid4()}",
                )
                advisor_output = result["advisor_output"]
                assert finding.observation_refs == (observation.id,)
                assert advisor_output["advisor_status"] == "disclosure_restricted"
                assert advisor_output["preflight_status"] == "POLICY_BLOCKED"
                assert len(transport.calls) - before == 0
                traces = advisor_output["disclosure_restricted_sample_associations"]
                trace = next(
                    item for item in traces if item["association_id"] == str(policy_association.id)
                )
                assert expected_source in trace["restriction_sources"]
                assert str(observation.id) in trace["linked_observation_refs"]

            credential_marker = "synthetic-m05c-credential-sentinel"
            credential_text = f"OPERATOR_API_TOKEN={credential_marker}"
            signed_url_marker = "synthetic-m05c-signature"
            signed_url = (
                "https://storage.invalid/synthetic?X-Amz-Credential=fixture&X-Amz-Signature="
                + signed_url_marker
            )
            assert advisor_sample.expected_hash is not None
            signed_url_observation = await _persist_observation(
                typed_uow_factory,
                artifact_store,
                investigation_id=investigation_id,
                sample_sha256=advisor_sample.expected_hash,
                payload={
                    "schema": "m05c.synthetic-signed-url-veto.v1",
                    "sample_sha256": advisor_sample.expected_hash,
                    "text": signed_url,
                },
                source="sample.m05c_signed_url_veto_fixture",
                source_version="m05c-signed-url-veto.v1",
                idempotency_key=f"m05c-signed-url-veto:{investigation_id}",
            )
            signed_url_finding = InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.CAPABILITY,
                assessment=FindingAssessment.SUPPORTED,
                statement="The persisted synthetic observation contains a signed URL marker.",
                observation_refs=(signed_url_observation.id,),
                limitations=("Synthetic refusal fixture; the URL is never submitted.",),
            )
            async with typed_uow_factory() as uow:
                await uow.investigation_findings.append_version(
                    signed_url_finding, expected_previous_id=None
                )
                await uow.commit()
            for label, marker, sensitive_text, observation, finding_refs in (
                (
                    "credential",
                    credential_marker,
                    credential_text,
                    advisor_observation,
                    [],
                ),
                (
                    "signed-url",
                    signed_url_marker,
                    "Review the persisted signed URL note.",
                    signed_url_observation,
                    [str(signed_url_finding.id)],
                ),
            ):
                before = len(transport.calls)
                advisor_arguments: dict[str, Any] = {
                    "consultation_type": ("draft_text" if label == "credential" else "figure_plan"),
                    "question": (
                        "Review the persisted sample-linked measurement. "
                        f"Source note: {sensitive_text}"
                    ),
                    "evidence_refs": [str(observation.id)],
                    "finding_refs": finding_refs,
                }
                if label == "credential":
                    advisor_arguments["assertions"] = [
                        {
                            "id": "m05c_credential_veto_fixture",
                            "text": (
                                "Le fixture synthétique persistant enregistre son empreinte "
                                "déclarée."
                            ),
                            "nature": "analyse",
                            "refs": [{"kind": "observation", "id": str(observation.id)}],
                            "block_type": "key_findings",
                            "form": "body",
                            "anchor": None,
                        }
                    ]
                _, result = await invoke_advisor(
                    advisor_arguments,
                    key=f"m05c-advisor-veto:{label}:{uuid4()}",
                )
                output = result["advisor_output"]
                assert output["advisor_status"] == "refused", (
                    f"{label}: observation={observation.id}; "
                    f"failure_stage={output.get('failure_stage')}; "
                    f"refs={output.get('disclosure_restricted_refs')}; "
                    f"samples={output.get('disclosure_restricted_sample_associations')}"
                )
                assert output["refusal_code"] == "autowork_credential_or_signed_url_detected"
                assert output["confirmed_non_execution"] is True
                assert len(transport.calls) - before == 0
                if label == "signed-url":
                    assert signed_url_finding.observation_refs == (observation.id,)
                assert marker not in json.dumps(output)
                if label == "signed-url":
                    assert signed_url not in json.dumps(output)

            await assert_sample_veto(
                label="tlp-red",
                tlp=TLP.RED,
                expected_source="tlp_red",
            )
            await assert_sample_veto(
                label="owner-restricted",
                license_restriction="Synthetic owner restriction sentinel; never disclose.",
                expected_source="license_restriction",
            )

            missing_ref = uuid4()
            before = len(transport.calls)
            _, incomplete = await invoke_advisor(
                {
                    "consultation_type": "plan_investigation",
                    "question": "Check evidence completeness before local Advisor dispatch.",
                    "evidence_refs": [str(analysis_observation.id), str(missing_ref)],
                },
                key=f"m05c-advisor-veto:missing-ref:{investigation_id}",
            )
            assert len(transport.calls) == before
            # Adapter self-attestation is not framework submission proof, so the
            # operator journal remains NEEDS_REVIEW while the Advisor output records
            # the typed pre-transport refusal.
            assert incomplete["invocation"]["status"] == "needs_review"
            assert incomplete["invocation"]["error_code"] == "invocation_needs_review"
            incomplete_output = incomplete["advisor_output"]
            assert incomplete_output["advisor_status"] == "invalid_input"
            assert incomplete_output["input_error_code"] == "advisor_input_rejected"
            assert incomplete_output["confirmed_non_execution"] is True
            assert incomplete_output["provider_usage"] == {"status": "not_submitted"}

            async with typed_uow_factory() as uow:
                current = await uow.investigation_envelopes.get(investigation_id)
                assert current is not None
            revoked = await envelope_service.revoke(
                investigation_id,
                expected_version=current.version,
                actor_id="editor:m05c-g2-reviewer",
                reason="Revoke further external consultation after the bounded permitted passage.",
                correlation_id=f"m05c-revoke:{investigation_id}",
            )
            before = len(transport.calls)
            _, revoked_result = await invoke_advisor(
                {
                    "consultation_type": "plan_investigation",
                    "question": "This persisted G2 envelope has been revoked.",
                    "evidence_refs": [str(analysis_observation.id)],
                },
                key=f"m05c-advisor-veto:revoked:{investigation_id}",
            )
            assert revoked.disclosure.external_llm_allowed is False
            assert len(transport.calls) == before
            # G2 revocation is rejected by the framework before the Advisor adapter runs.
            assert revoked_result["advisor_output"] is None
            assert revoked_result["invocation"]["status"] == "unavailable"
            assert revoked_result["invocation"]["error_code"] == "tool_unavailable"
            assert revoked_result["output"]["error"]["reason"] == "external_llm_disclosure_denied"

            async with typed_uow_factory() as uow:
                all_findings = await uow.investigation_findings.list_for_investigation(
                    investigation_id
                )
                all_observations = await uow.evidence_observations.list_for_investigation(
                    investigation_id
                )
            assert any(item.id == primary_finding.id for item in all_findings)
            assert any(item.id == analysis_observation.id for item in all_observations)
            assert primary_finding.observation_refs == (analysis_observation.id,)

            opinion_service = AnalystOpinionService(
                uow_factory=typed_uow_factory,
                artifact_store=artifact_store,
                author_id="analyst:m05c-opinion-composer",
            )
            draft = await opinion_service.compose_from_findings(
                investigation_id,
                draft_blocks=(
                    OpinionBlock(
                        block_type=OpinionBlockType.KEY_FINDINGS,
                        text=(
                            "The inert synthetic sample has one verified UTF-8 measurement "
                            "whose observation cites the quarantined sample SHA-256."
                        ),
                        finding_refs=(primary_finding.id,),
                        evidence_refs=(analysis_observation.id,),
                        qualification=OpinionClaimQualification.FACT,
                        certainty=0.9,
                    ),
                ),
            )
            approved_opinion = await opinion_service.approve(
                draft.id,
                draft.version,
                draft.content_sha256,
                "editor:m05c-reviewer",
                "The persisted finding and sample-linked observation support this statement.",
                idempotency_key=f"m05c-opinion-approve:{investigation_id}",
            )
            assert approved_opinion.reviewer_id == "editor:m05c-reviewer"
            augmentation = PublicationAugmentationArtifactService(
                uow_factory=typed_uow_factory,
                artifact_store=artifact_store,
            )
            publication_service = AnalystPublicationService(
                uow_factory=typed_uow_factory,
                artifact_store=artifact_store,
                augmentation_service=augmentation,
            )
            v6_artifact = await publication_service.publish_augmented_article(
                approved_opinion.id,
                approved_opinion.version,
                "editor:m05c-publication-approver",
                approved_opinion.base_publication_sha256,
            )
            assert v6_artifact.stage is ProductionArtifactStage.PUBLICATION
            assert v6_artifact.version == 2

            repository_root = _repository_root()
            typst_binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
            assert typst_binary is not None
            assert await asyncio.to_thread(Path(typst_binary).is_file)
            pdftotext = shutil.which("pdftotext")
            assert pdftotext is not None
            render_service = PublicationRenderService(
                uow_factory=typed_uow_factory,
                artifact_store=artifact_store,
                media_asset_store=MediaAssetStore(artifact_store, typed_uow_factory),
                renderer=TypstRenderer(),
                compiler=_TypstCompiler(binary=typst_binary),
                chp_typst_root=repository_root / "chpTypst",
                font_bundle_root=Path(
                    os.environ.get("FONT_BUNDLE_ROOT") or repository_root / "chpTypst"
                ),
                typst_fonts_lock_path=repository_root / "infra" / "typst-fonts.lock",
                graph_diagram_compiler=D2DiagramCompiler(),
            )
            base_render = await render_service.render_pdf(seeded.publication.id)
            v6_render = await render_service.render_pdf(v6_artifact.id)
            assert base_render.output_blob_id is not None
            assert v6_render.output_blob_id is not None
            assert base_render.output_sha256 is not None
            assert v6_render.output_sha256 is not None
            assert base_render.output_byte_size is not None and base_render.output_byte_size > 0
            assert v6_render.output_byte_size is not None and v6_render.output_byte_size > 0
            base_lineage = await render_service.lineage_manifest_for_render(base_render)
            v6_lineage = await render_service.lineage_manifest_for_render(v6_render)
            assert base_lineage["nature"] == "base_publication"
            assert "actors" not in base_lineage
            assert v6_lineage["actors"]["reviewer"]["actor_id"] == "editor:m05c-reviewer"
            assert v6_lineage["actors"]["approver"]["actor_id"] == (
                "editor:m05c-publication-approver"
            )
            async with typed_uow_factory() as uow:
                current_opinions = await uow.analyst_opinions.get(
                    approved_opinion.id, version=approved_opinion.version
                )
                approval_decisions = await uow.analyst_decisions.list_for_investigation(
                    investigation_id
                )
                persisted_v6 = await uow.production_artifacts.get(v6_artifact.id)
            assert current_opinions is not None
            assert current_opinions.status.value == "approved"
            reviewer_decision = next(
                item
                for item in approval_decisions
                if item.decision_type is AnalystDecisionType.NOTE_APPROVE
                and item.target_id == approved_opinion.id
            )
            assert reviewer_decision.actor_id == "editor:m05c-reviewer"
            assert persisted_v6 is not None and persisted_v6.version == 2
            v6_bytes = await artifact_store.read_bytes(persisted_v6.canonical_blob_id)
            parsed_v6 = publication_document_from_json(json.loads(v6_bytes))
            assert isinstance(parsed_v6, PublicationDocumentV6)
            assert parsed_v6.analyst_opinion is not None
            assert parsed_v6.analyst_opinion.id == approved_opinion.id
            assert parsed_v6.analyst_opinion.version == approved_opinion.version
            assert analysis_observation.id in parsed_v6.analyst_opinion.blocks[0].evidence_refs
            base_text = await _pdf_text(
                artifact_store, base_render.output_blob_id, tmp_path / "m05c-v5.pdf"
            )
            v6_text = await _pdf_text(
                artifact_store, v6_render.output_blob_id, tmp_path / "m05c-v6.pdf"
            )
            assert base_text
            assert "inert synthetic sample" in v6_text.casefold()
            assert "verified utf-8 measurement" in v6_text.casefold()
            assert "editor:m05c-reviewer" not in v6_text
            assert "editor:m05c-publication-approver" not in v6_text

            async def canonical_history() -> tuple[Any, ...]:
                async with typed_uow_factory() as uow:
                    envelope = await uow.investigation_envelopes.get(investigation_id)
                    analyst_investigation = await uow.analyst_investigations.get(investigation_id)
                    associations = await uow.sample_associations.list_for_investigation(
                        investigation_id
                    )
                    samples = await uow.samples.list_for_subject(seeded.subject.id)
                    observations = await uow.evidence_observations.list_for_investigation(
                        investigation_id
                    )
                    findings = await uow.investigation_findings.list_for_investigation(
                        investigation_id
                    )
                    decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
                    opinions = []
                    for version in range(1, approved_opinion.version + 1):
                        item = await uow.analyst_opinions.get(approved_opinion.id, version=version)
                        if item is not None:
                            opinions.append(item)
                    artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
                    successful_attempt = await uow.sample_acquisition_attempts.find_successful(
                        investigation_id, material.sample_hash
                    )
                    sample_provenance = await uow.provenance.list_for_aggregate("sample", sample.id)
                    pinned_v6_render = (
                        await uow.publication_renders.get_pinned_succeeded_for_artifact(
                            v6_artifact.id
                        )
                    )
                    return (
                        envelope,
                        analyst_investigation,
                        tuple(associations),
                        tuple(samples),
                        tuple(observations),
                        tuple(findings),
                        tuple(decisions),
                        tuple(opinions),
                        tuple(artifacts),
                        successful_attempt,
                        tuple(sample_provenance),
                        pinned_v6_render,
                    )

            history_before_replay = await canonical_history()
            async with typed_uow_factory() as uow:
                envelope_before_replay = await uow.investigation_envelopes.get(investigation_id)
                investigation_before_replay = await uow.analyst_investigations.get(investigation_id)
            assert envelope_before_replay is not None
            assert investigation_before_replay is not None
            assert envelope_before_replay.budget.consumed[BudgetDimension.DOWNLOADS] == 1
            assert envelope_before_replay.budget.consumed[BudgetDimension.VT_UNITS] == 1
            loop_budget_before_replay = (
                investigation_before_replay.budget.consumed_vt_read_units,
                investigation_before_replay.budget.consumed_hits_acquired,
            )
            assert loop_budget_before_replay == (1, 1)
            direct_replay = await acquisition.acquire(
                investigation_id=investigation_id,
                subject_id=seeded.subject.id,
                requested_hash=material.sample_hash,
                reason=SampleAcquisitionReason.HIT_REVIEW,
                policy=SampleAcquisitionPolicy(
                    tlp=TLP.CLEAR,
                    do_not_submit=False,
                    external_llm_allowed=True,
                ),
            )
            assert direct_replay.outcome.value == "success"
            assert direct_replay.sample_id == sample.id
            assert direct_replay.reused_existing_sample is True
            assert provider.calls == 1
            replay_association = await hit_acquisition.register_hit(
                investigation_id,
                material.sample_hash,
                SampleOrigin.VT_PIVOT,
            )
            assert replay_association.id == association.id
            assert provider.calls == 1
            async with typed_uow_factory() as uow:
                replay_attempt = await uow.sample_acquisition_attempts.find_successful(
                    investigation_id, material.sample_hash
                )
                replay_sample = (
                    await uow.samples.get(replay_attempt.sample_id)
                    if replay_attempt is not None and replay_attempt.sample_id is not None
                    else None
                )
                replay_association_rows = await uow.sample_associations.list_for_investigation(
                    investigation_id
                )
                envelope_after_replay = await uow.investigation_envelopes.get(investigation_id)
                investigation_after_replay = await uow.analyst_investigations.get(investigation_id)
            assert replay_attempt is not None and replay_sample is not None
            assert replay_sample.id == sample.id
            assert envelope_after_replay is not None
            assert investigation_after_replay is not None
            assert envelope_after_replay.budget.consumed == envelope_before_replay.budget.consumed
            assert (
                investigation_after_replay.budget.consumed_vt_read_units,
                investigation_after_replay.budget.consumed_hits_acquired,
            ) == loop_budget_before_replay
            assert (
                len(
                    [
                        item
                        for item in replay_association_rows
                        if item.sample_sha256 == material.sample_hash
                    ]
                )
                == 1
            )
            replay_v6 = await publication_service.publish_augmented_article(
                approved_opinion.id,
                approved_opinion.version,
                "editor:m05c-publication-approver",
                approved_opinion.base_publication_sha256,
            )
            replay_render = await render_service.render_pdf(replay_v6.id)
            assert replay_render == v6_render
            assert replay_render.output_blob_id == v6_render.output_blob_id
            assert replay_render.output_sha256 == v6_render.output_sha256
            assert replay_render.output_byte_size == v6_render.output_byte_size
            assert replay_render.output_blob_id is not None
            assert v6_render.output_blob_id is not None
            replay_pdf_bytes = await artifact_store.read_bytes(replay_render.output_blob_id)
            original_pdf_bytes = await artifact_store.read_bytes(v6_render.output_blob_id)
            assert replay_pdf_bytes == original_pdf_bytes
            assert hashlib.sha256(replay_pdf_bytes).hexdigest() == replay_render.output_sha256
            history_after_replay = await canonical_history()
            assert replay_v6.id == v6_artifact.id
            assert replay_v6.input_hash == v6_artifact.input_hash
            assert history_after_replay == history_before_replay
            assert provider.calls == 1
            assert provider.downloaded_hashes == [material.sample_hash]

            audit_snapshot = await read_investigation_audit_snapshot(
                session_factory, investigation_id, artifact_store
            )
            audit_dir = tmp_path / "m05c-audit-export"
            audit_bundle = write_audit_bundle(audit_snapshot, audit_dir, str(investigation_id))
            resumed_bundle = write_audit_bundle(None, audit_dir, str(investigation_id), resume=True)
            assert resumed_bundle.resumed_from_events is True
            assert resumed_bundle.events == audit_bundle.events
            assert resumed_bundle.trace == audit_bundle.trace
            assert resumed_bundle.index == audit_bundle.index
            assert resumed_bundle.manifest == audit_bundle.manifest
            audit_events = load_audit_events(
                audit_dir / "RUN_EVENTS.jsonl.gz", str(investigation_id)
            )
            event_types = {item["type"] for item in audit_events}
            assert "finding.version_recorded" in event_types
            assert "opinion.version_recorded" in event_types
            assert "advisor_call.metadata" in event_types
            source_events = audit_events[:-1]
            event_counts = Counter(item["type"] for item in source_events)
            # Success, two sensitive-input vetoes, two policy vetoes, and missing-ref refusal.
            expected_advisor_metadata_events = 1 + 2 + 2 + 1
            assert event_counts["evidence_observation.recorded"] == len(audit_snapshot.observations)
            assert event_counts["finding.version_recorded"] == len(audit_snapshot.findings)
            assert event_counts["opinion.version_recorded"] == len(audit_snapshot.opinions)
            assert (
                event_counts["advisor_call.metadata"]
                == len(audit_snapshot.advisor_call_metadata)
                == expected_advisor_metadata_events
            )

            observation_event_ids = Counter(
                item["record"]["id"]
                for item in source_events
                if item["type"] == "evidence_observation.recorded"
            )
            assert observation_event_ids == Counter(
                str(item.id) for item in audit_snapshot.observations
            )
            finding_event_keys = Counter(
                (item["record"]["id"], item["record"].get("version"))
                for item in source_events
                if item["type"] == "finding.version_recorded"
            )
            assert finding_event_keys == Counter(
                (str(item.id), item.version) for item in audit_snapshot.findings
            )
            opinion_event_keys = Counter(
                (item["record"]["id"], item["record"].get("version"))
                for item in source_events
                if item["type"] == "opinion.version_recorded"
            )
            assert opinion_event_keys == Counter(
                (str(item.id), item.version) for item in audit_snapshot.opinions
            )
            advisor_events = [
                item for item in source_events if item["type"] == "advisor_call.metadata"
            ]
            assert len(advisor_events) == expected_advisor_metadata_events
            advisor_event_ids = Counter(item["record"]["id"] for item in advisor_events)
            assert advisor_event_ids == Counter(
                str(item["observation_id"]) for item in audit_snapshot.advisor_call_metadata
            )
            successful_advisor_metadata = [
                item
                for item in audit_snapshot.advisor_call_metadata
                if item["metadata"]["advisor_status"] == "succeeded"
            ]
            assert len(successful_advisor_metadata) == len(transport.calls) == 1
            advisor_observation_id = str(successful_advisor_metadata[0]["observation_id"])
            assert successful_advisor_metadata[0]["metadata"]["prompt_blob_id"] == str(
                allowed["advisor_output"]["prompt_blob_id"]
            )
            assert (
                successful_advisor_metadata[0]["metadata"]["prompt_sha256"]
                == allowed["advisor_output"]["prompt_sha256"]
            )

            def unique_event_index(event_type: str, record_id: UUID, version: int | None) -> int:
                matches = [
                    index
                    for index, item in enumerate(source_events)
                    if item["type"] == event_type
                    and item["record"]["id"] == str(record_id)
                    and item["record"].get("version") == version
                ]
                assert len(matches) == 1, (event_type, record_id, version, matches)
                return matches[0]

            primary_finding_event = unique_event_index(
                "finding.version_recorded", primary_finding.id, primary_finding.version
            )
            advisor_finding_event = unique_event_index(
                "finding.version_recorded", advisor_finding.id, advisor_finding.version
            )
            signed_url_finding_event = unique_event_index(
                "finding.version_recorded", signed_url_finding.id, signed_url_finding.version
            )
            advisor_event_index = next(
                index
                for index, item in enumerate(source_events)
                if item["type"] == "advisor_call.metadata"
                and item["record"]["id"] == advisor_observation_id
            )
            first_opinion_event = unique_event_index(
                "opinion.version_recorded", approved_opinion.id, 1
            )
            assert (
                primary_finding_event
                < advisor_finding_event
                < advisor_event_index
                < signed_url_finding_event
                < first_opinion_event
            )
            assert credential_marker not in (audit_dir / "RUN_TRACE.md").read_text()
            assert signed_url_marker not in (audit_dir / "RUN_TRACE.md").read_text()
            assert transport.calls[0]["external_llm_allowed"] is True
            assert len(transport.calls) == 1
            assert provider.calls == 1
    finally:
        await engine.dispose()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]
