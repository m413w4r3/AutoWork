"""Adversarial PostgreSQL checks for the Mission 05 Phase 3 journeys."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import (
    ModelConversationTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
    make_investigation_conversation_id,
)
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_audit_export import write_audit_bundle
from cti_app.application.investigation_composition import InvestigationCompositionFactory
from cti_app.application.investigation_governance import (
    InvestigationActor,
    SampleQualificationService,
)
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelCapabilities,
    ModelGateway,
    ModelRouter,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SampleState
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.investigation import (
    BudgetDimension,
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
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import StopReason
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_conversations import ConversationStatus, ConversationTurnStatus
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelSubmissionState,
    ModelTransport,
    ModelUsage,
)
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationTimelineEntryV1,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.investigation_audit_reader import (
    read_investigation_audit_snapshot,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from cti_app.integrations.models import BlobModelOutputStore
from tests.integration.test_investigation_e2e_no_sample_postgres import (
    _REPOSITORY_ROOT,
    _advisor_raw,
    _invoke_advisor,
    _ReservationTransport,
)
from tests.workflow_emulator.investigation_support import (
    approve_envelope_http,
    approve_opinion_http,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    investigation_settings,
    publish_opinion_http,
    revise_opinion_http,
    run_http_job,
    seed_verified_article,
    start_investigation_http,
)

pytestmark = pytest.mark.integration


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture
def block_non_loopback_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject all non-loopback socket and DNS access while allowing test PG."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_sendmsg = socket.socket.sendmsg
    original_getaddrinfo = socket.getaddrinfo
    original_loop_getaddrinfo = asyncio.BaseEventLoop.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_gethostbyaddr = socket.gethostbyaddr

    def allow_address(address: object) -> bool:
        return isinstance(address, tuple) and bool(address) and _is_loopback(address[0])

    def checked_connect(sock: socket.socket, address: object) -> None:
        if not allow_address(address):
            raise AssertionError(f"blocked non-loopback socket: {address!r}")
        original_connect(sock, address)  # type: ignore[arg-type]

    def checked_connect_ex(sock: socket.socket, address: object) -> int:
        if not allow_address(address):
            raise AssertionError(f"blocked non-loopback socket: {address!r}")
        return original_connect_ex(sock, address)  # type: ignore[arg-type]

    def checked_sendto(sock: socket.socket, data: bytes, *args: object) -> int:
        address = args[-1] if args else None
        if not allow_address(address):
            raise AssertionError(f"blocked non-loopback socket: {address!r}")
        sendto = cast(Callable[..., int], original_sendto)
        return sendto(sock, data, *args)

    def checked_sendmsg(
        sock: socket.socket,
        buffers: object,
        ancdata: object = (),
        flags: int = 0,
        address: object = None,
    ) -> int:
        destination = address
        if destination is None:
            try:
                destination = sock.getpeername()
            except OSError as exc:
                raise AssertionError("blocked sendmsg without a loopback destination") from exc
        if not allow_address(destination):
            raise AssertionError(f"blocked non-loopback socket: {destination!r}")
        sendmsg = cast(Callable[..., int], original_sendmsg)
        if address is None:
            return sendmsg(sock, buffers, ancdata, flags)
        return sendmsg(sock, buffers, ancdata, flags, address)

    def checked_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host is not None and not _is_loopback(host):
            raise AssertionError(f"blocked non-loopback DNS lookup: {host!r}")
        return original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    async def checked_loop_getaddrinfo(
        loop: asyncio.BaseEventLoop, host: object, *args: object, **kwargs: object
    ) -> object:
        if host is not None and not _is_loopback(host):
            raise AssertionError(f"blocked non-loopback DNS lookup: {host!r}")
        return await original_loop_getaddrinfo(loop, host, *args, **kwargs)  # type: ignore[arg-type]

    def checked_gethostbyname(host: str) -> str:
        if not _is_loopback(host):
            raise AssertionError(f"blocked non-loopback DNS lookup: {host!r}")
        return original_gethostbyname(host)

    def checked_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
        if not _is_loopback(host):
            raise AssertionError(f"blocked non-loopback DNS lookup: {host!r}")
        return original_gethostbyname_ex(host)

    def checked_gethostbyaddr(host: str) -> tuple[str, list[str], list[str]]:
        if not _is_loopback(host):
            raise AssertionError(f"blocked non-loopback DNS lookup: {host!r}")
        return original_gethostbyaddr(host)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", checked_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", checked_sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", checked_sendmsg)
    monkeypatch.setattr(socket, "getaddrinfo", checked_getaddrinfo)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", checked_loop_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", checked_gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", checked_gethostbyname_ex)
    monkeypatch.setattr(socket, "gethostbyaddr", checked_gethostbyaddr)


class _OperatorClientIdentityProvider:
    mode = "local"
    verified = False

    def __init__(self) -> None:
        self.actor_id = "human:m05d-adversarial"

    async def current(self) -> Identity:
        return Identity(
            actor_id=self.actor_id,
            scopes=frozenset({IdentityScope.HUMAN_OPERATOR, IdentityScope.OPERATOR_CLIENT}),
        )


class _HumanIdentityProvider:
    mode = "local"
    verified = False

    def __init__(self) -> None:
        self.actor_id = "human:m05d-adversarial"

    async def current(self) -> Identity:
        return Identity(
            actor_id=self.actor_id,
            scopes=frozenset({IdentityScope.HUMAN_OPERATOR}),
        )


class _UncertainModelAdapter:
    """Persist one uncertain provider outcome through the real model gateway."""

    provider = ModelProvider.OPENAI
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    requested_model = "m05d-synthetic-uncertain-bridge"
    is_external = True
    is_configured = True
    capabilities = ModelCapabilities(conversation=True)

    def __init__(self) -> None:
        self.fake_sends = 0

    async def invoke(
        self,
        request: Any,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del request, role, output_schema
        self.fake_sends += 1
        return AdapterResult(
            status=AdapterResultStatus.NEEDS_REVIEW,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(),
            metadata={
                "reason": "synthetic_timeout_after_submission",
                "submission_state": ModelSubmissionState.EXTERNAL_STATE_UNKNOWN.value,
                "failure_stage": "submit",
                "failure_reason": "synthetic_timeout_after_submission",
                "diagnostic_code": "synthetic_bridge_timeout",
            },
        )

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("The synthetic uncertain adapter is never resumable.")


async def _append_observation(
    uow_factory,
    artifact_store: ProductionArtifactStore,
    investigation_id: UUID,
    *,
    observation_id: UUID,
    observed_at: datetime,
    record_text: str | None = None,
) -> EvidenceObservation:
    claim_id = f"C{observation_id.int}"
    payload = ProductionArtifactStore.canonical_json_bytes(
        {
            "schema": "investigation-tool-observation.v1",
            "tool_name": "synthetic.article_record",
            "schema_version": "1",
            "operation_key": f"m05d-source-{observation_id}",
            "result": {
                "claim_id": claim_id,
                "text": record_text
                or f"Synthetic article record for {claim_id}; no external source was consulted.",
            },
        }
    )
    blob_id = await artifact_store.put_bytes(
        payload,
        bucket="m05d-adversarial-observations",
        mime_type="application/json",
    )
    observation = EvidenceObservation(
        id=observation_id,
        investigation_id=investigation_id,
        kind=EvidenceKind.CLAIM_SOURCE,
        blob_id=blob_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        source="synthetic-article-record",
        source_version="1",
        observed_at=observed_at,
        object_type="article_claim",
        object_id=claim_id,
        limitations=("Synthetic fixture only; no external source was consulted.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    return observation


async def _persist_sample_policy_fixture(
    uow_factory,
    artifact_store: ProductionArtifactStore,
    *,
    investigation_id: UUID,
    subject_id: UUID,
    label: str,
    tlp: TLP = TLP.CLEAR,
    do_not_submit: bool = False,
    external_llm_allowed: bool = True,
    license_restriction: str | None = None,
) -> tuple[Sample, SampleAssociation, EvidenceObservation]:
    payload = f"AUTOWORK M05D inert sample policy fixture {label}; do not execute.\n".encode()
    sample_sha256 = hashlib.sha256(payload).hexdigest()
    sample_blob_id = await artifact_store.put_bytes(
        payload,
        bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    sample = Sample(
        subject_id=subject_id,
        blob_id=sample_blob_id,
        original_name=f"m05d-inert-sample-policy-{label}.txt",
        origin=f"M05-D inert sample policy fixture {label}",
        acquired_at=datetime.now(UTC),
        license_restriction=license_restriction,
        tlp=tlp,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_llm_allowed,
        origin_kind=EntitySampleOrigin.MANUAL,
        state=SampleState.QUARANTINED,
        source_service="m05d-sample-policy-fixture",
        source_object_id=sample_sha256,
        expected_hash=sample_sha256,
    )
    association = SampleAssociation(
        investigation_id=investigation_id,
        sample_sha256=sample_sha256,
        origin=SampleOrigin.MANUAL,
    )
    observation_payload = ProductionArtifactStore.canonical_json_bytes(
        {
            "schema": "m05d.sample-policy-observation.v1",
            "sample_sha256": sample_sha256,
            "byte_length": len(payload),
            "mime_type": "application/octet-stream",
            "measurement": f"Inert local fixture {label}; no sample execution occurred.",
        }
    )
    observation_blob_id = await artifact_store.put_bytes(
        observation_payload,
        bucket="m05d-sample-policy-observations",
        mime_type="application/json",
    )
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=observation_blob_id,
        sha256=hashlib.sha256(observation_payload).hexdigest(),
        source=f"sample.m05d_policy_fixture.{label}",
        source_version="m05d-sample-policy-observation.v1",
        observed_at=datetime.now(UTC),
        object_type="sample_observation",
        object_id=sample_sha256,
        limitations=("Synthetic inert sample policy fixture only.",),
    )
    async with uow_factory() as uow:
        await uow.samples.add(sample)
        await uow.sample_associations.append_version(association, expected_version=0)
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    await SampleQualificationService(uow_factory).qualify(
        association.id,
        SampleQualification.CONFIRMED,
        InvestigationActor("editor:m05d-reviewer", QualificationActorKind.HUMAN),
        f"Confirm the inert {label} policy fixture association for refusal coverage.",
        (observation.id,),
        correlation_id=f"m05d-sample-policy:{label}:{association.id}",
    )
    return sample, association, observation


async def _add_timeline_to_seeded_publication(
    uow_factory, artifact_store: ProductionArtifactStore, seeded
):
    parsed = publication_document_from_json(json.loads(seeded.publication_bytes))
    assert isinstance(parsed, PublicationDocumentV5)
    assert parsed.document.sources
    source_id = parsed.document.sources[0].source_document_id
    first_ref = PublicationEvidenceRefV1(
        source_document_id=source_id,
        kind=PublicationEvidenceKind.EVENT,
        evidence_key=hashlib.sha256(b"m05d-chronology-first").hexdigest(),
    )
    second_ref = PublicationEvidenceRefV1(
        source_document_id=source_id,
        kind=PublicationEvidenceKind.EVENT,
        evidence_key=hashlib.sha256(b"m05d-chronology-second").hexdigest(),
    )
    timeline = (
        PublicationTimelineEntryV1(
            event_date=date(2026, 10, 1),
            date_text=None,
            text="M05D-CHRONOLOGY-ALPHA synthetic event.",
            evidence_refs=(first_ref,),
        ),
        PublicationTimelineEntryV1(
            event_date=date(2026, 10, 2),
            date_text=None,
            text="M05D-CHRONOLOGY-BETA synthetic event.",
            evidence_refs=(second_ref,),
        ),
    )
    document = replace(
        parsed,
        document=replace(
            parsed.document,
            timeline=timeline,
        ),
        semantic_text=SemanticTextV1(
            schema_version=parsed.semantic_text.schema_version,
            policy_version=parsed.semantic_text.policy_version,
            paragraphs=(
                *(
                    paragraph
                    for paragraph in parsed.semantic_text.paragraphs
                    if not paragraph.anchor.startswith("timeline:")
                ),
                *(
                    SemanticParagraphV1(
                        anchor=f"timeline:{index:04d}",
                        spans=(SemanticTextSpanV1(role=SemanticRole.TEXT, text=item.text),),
                    )
                    for index, item in enumerate(timeline, start=1)
                ),
            ),
        ),
    )
    publication_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(document)
    )
    publication_sha256 = hashlib.sha256(publication_bytes).hexdigest()
    blob_id = await artifact_store.put_bytes(
        publication_bytes,
        bucket="m05d-adversarial-publication-v5",
        mime_type="application/json",
    )
    publication = replace(
        seeded.publication,
        id=uuid4(),
        version=seeded.publication.version + 1,
        input_hash=publication_sha256,
        canonical_blob_id=blob_id,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(publication)
        current = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.PUBLICATION.value
        )
        assert current == publication
        await uow.commit()
    return replace(seeded, publication=publication, publication_bytes=publication_bytes)


@pytest.mark.asyncio
async def test_operator_context_cas_and_advisor_egress_guard_postgres(
    uow_factory, tmp_path: Path, block_non_loopback_sockets
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "m05d-context-blobs"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
        include_seed=False,
    )
    seeded = await _add_timeline_to_seeded_publication(uow_factory, artifact_store, seeded)
    investigation_id = seeded.investigation.id
    transport = _ReservationTransport(
        uow_factory,
        investigation_id,
        [
            TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=_advisor_raw("succeeded", "The synthetic evidence is bounded."),
            )
        ],
    )
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=investigation_settings(providers=True),
        transport=transport,
    )
    app, _, _ = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    identity_provider = _OperatorClientIdentityProvider()
    app.state.identity_provider = identity_provider
    base_url = "http://test"

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=base_url) as client:
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
        async with uow_factory() as uow:
            assert await uow.sample_associations.list_for_investigation(investigation_id) == ()
        previous_provider = app.state.identity_provider
        app.state.identity_provider = _HumanIdentityProvider()
        approved = await approve_envelope_http(
            client,
            investigation_id,
            providers=("bridge",),
            budget_limits={
                "bridge_turns": 4,
                "vt_units": 0,
                "shodan_credits": 0,
                "downloads": 0,
                "tool_seconds": 20_000,
            },
            allow_acquisition=False,
        )
        app.state.identity_provider = previous_provider
        assert approved.status_code == 200, approved.text

        timestamp = datetime(2026, 10, 2, tzinfo=UTC)
        first_id = UUID("f00dbabe-0000-4000-8000-000000000001")
        second_id = UUID("f00dbabe-0000-4000-8000-000000000002")
        first = await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=first_id,
            observed_at=timestamp,
        )
        await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=second_id,
            observed_at=timestamp,
        )
        finding_v1 = InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.CLAIM_VERIFICATION,
            assessment=FindingAssessment.QUALIFIED,
            statement="Version one synthetic claim assessment.",
            observation_refs=(first.id,),
            claim_refs=(str(first.object_id),),
            scope="Synthetic source record only.",
            limitations=("No external source was checked.",),
            version=1,
        )
        finding_v2 = replace(
            finding_v1,
            id=uuid4(),
            version=2,
            supersedes=finding_v1.id,
            assessment=FindingAssessment.SUPPORTED,
            statement="Version two synthetic claim assessment.",
        )
        async with uow_factory() as uow:
            await uow.investigation_findings.append_version(finding_v1, expected_previous_id=None)
            await uow.investigation_findings.append_version(
                finding_v2, expected_previous_id=finding_v1.id
            )
            await uow.commit()
            state = await uow.investigation_states.get(investigation_id)
        assert state is not None

        expected = {
            "expected_version": state.version,
            "expected_sha256": state.sha256,
        }
        full = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={"view": "full", **expected},
        )
        assert full.status_code == 200, full.text
        full_payload = full.json()
        full_evidence = full_payload["context"]["investigation"]["evidence_index"]
        # NULL timestamp seeks remain covered in test_operator_context_summary_postgres.py by
        # test_postgres_timestamp_keyset_predicate_handles_null_pivots.
        assert [item["evidence_id"] for item in full_evidence] == [str(first_id), str(second_id)]
        current_findings = full_payload["context"]["investigation"]["findings"]
        assert [item["finding_id"] for item in current_findings] == [str(finding_v2.id)]

        page_path = (
            f"/api/investigations/{investigation_id}/operator/context/section/evidence_index"
        )
        page_one = await client.get(page_path, params={**expected, "limit": 1})
        assert page_one.status_code == 200, page_one.text
        first_cursor = page_one.json()["next_cursor"]
        assert first_cursor
        paged_items = list(page_one.json()["items"])
        assert page_one.json()["context_sha256"] == full_payload["context_sha256"]
        while first_cursor:
            next_page = await client.get(
                page_path,
                params={**expected, "limit": 1, "cursor": first_cursor},
            )
            assert next_page.status_code == 200, next_page.text
            assert next_page.json()["context_sha256"] == full_payload["context_sha256"]
            paged_items.extend(next_page.json()["items"])
            first_cursor = next_page.json()["next_cursor"]
        assert paged_items == full_evidence

        history = await client.get(
            f"/api/investigations/{investigation_id}/operator/context/section/finding_versions",
            params={**expected, "limit": 20},
        )
        assert history.status_code == 200, history.text
        assert {item["finding_id"] for item in history.json()["items"]} == {
            str(finding_v1.id),
            str(finding_v2.id),
        }
        invalid_legacy_offset = await client.get(
            page_path,
            params={**expected, "offset": 1, "limit": 1},
        )
        assert invalid_legacy_offset.status_code == 422, invalid_legacy_offset.text

        await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=UUID("f00dbabe-0000-4000-8000-000000000003"),
            observed_at=datetime(2026, 10, 3, tzinfo=UTC),
        )
        async with uow_factory() as uow:
            current_state = await uow.investigation_states.get(investigation_id)
        assert current_state is not None
        stale_cursor = await client.get(
            page_path,
            params={
                "expected_version": current_state.version,
                "expected_sha256": current_state.sha256,
                "limit": 1,
                "cursor": page_one.json()["next_cursor"],
            },
        )
        assert stale_cursor.status_code == 409, stale_cursor.text

        timestamp_first_id = UUID("f00dbabe-0000-4000-8000-000000000008")
        uuid_first_id = UUID("f00dbabe-0000-4000-8000-000000000007")
        assert timestamp_first_id.int > uuid_first_id.int
        await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=timestamp_first_id,
            observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=uuid_first_id,
            observed_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        async with uow_factory() as uow:
            ordered_observations = await uow.evidence_observations.list_for_investigation(
                investigation_id
            )
        opposed_pair = {timestamp_first_id, uuid_first_id}
        assert [item.id for item in ordered_observations if item.id in opposed_pair] == [
            timestamp_first_id,
            uuid_first_id,
        ]
        # NULLS LAST remains covered by test_operator_context_summary_postgres.py's
        # test_postgres_repositories_page_nullable_evidence_and_finding_versions.

        declared_hypothesis = await client.post(
            f"/api/investigations/{investigation_id}/operator/tools/hypothesis.declare",
            json={
                "schema_version": "operator-hypothesis-declare.v1",
                "arguments": {
                    "hypothesis_id": "H1",
                    "statement": "The synthetic source record supports the bounded claim.",
                },
                "purpose": "Record a bounded hypothesis from the synthetic source record.",
                "expected_version": state.version,
            },
            headers={"Idempotency-Key": f"m05d-hypothesis-{investigation_id}"},
        )
        assert declared_hypothesis.status_code == 200, declared_hypothesis.text
        async with uow_factory() as uow:
            state = await uow.investigation_states.get(investigation_id)
        assert state is not None

        credential_marker = "synthetic-m05d-credential-sentinel"
        signed_url_marker = "synthetic-m05d-signature-sentinel"
        credential_observation = await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=UUID("f00dbabe-0000-4000-8000-000000000004"),
            observed_at=datetime(2026, 10, 4, tzinfo=UTC),
            record_text=f"Synthetic fixture marker only: OPERATOR_API_TOKEN={credential_marker}",
        )
        signed_url = (
            "https://storage.invalid/synthetic?X-Amz-Credential=fixture&X-Amz-Signature="
            + signed_url_marker
        )
        signed_url_observation = await _append_observation(
            uow_factory,
            artifact_store,
            investigation_id,
            observation_id=UUID("f00dbabe-0000-4000-8000-000000000005"),
            observed_at=datetime(2026, 10, 5, tzinfo=UTC),
            record_text=f"Synthetic fixture marker only: {signed_url}",
        )

        async with uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(investigation_id)
            composition = factory.build(uow, investigation_id, operator_mode=True)
            assert envelope is not None and composition.advisor_tool is not None
            for label, policy in (
                ("external-disabled", {"external_llm_allowed": False}),
                ("do-not-submit", {"do_not_submit": True}),
                ("tlp-red", {"tlp": TLPLevel.RED}),
            ):
                blocked_budget = InvestigationBudget(limits=envelope.budget.limits)
                blocked_key = f"m05d-egress-refusal-{label}-{investigation_id}"
                blocked_budget.reserve(
                    blocked_key,
                    {
                        BudgetDimension.BRIDGE_TURNS: 1,
                        BudgetDimension.TOOL_SECONDS: 3_600,
                        BudgetDimension.CONTEXT_BYTES: 0,
                        BudgetDimension.CONTEXT_TOKENS: 0,
                    },
                )
                refusal = await composition.advisor_tool.execute(
                    {
                        "consultation_type": "plan_investigation",
                        "question": "This synthetic request must be refused before transport.",
                        "evidence_refs": [str(first.id)],
                    },
                    ToolExecutionContext(
                        investigation_id=investigation_id,
                        envelope=replace(
                            envelope,
                            budget=blocked_budget,
                            disclosure=replace(envelope.disclosure, **policy),
                        ),
                        budget=blocked_budget,
                        idempotency_key=blocked_key,
                        available_capabilities=frozenset({"bridge.advisor"}),
                    ),
                )
                assert refusal.status is ToolInvocationStatus.POLICY_DENIED, label
            for label, observation, marker in (
                ("credential", credential_observation, credential_marker),
                ("signed-url", signed_url_observation, signed_url_marker),
            ):
                sensitive_budget = InvestigationBudget(limits=envelope.budget.limits)
                sensitive_key = f"m05d-sensitive-refusal-{label}-{investigation_id}"
                sensitive_budget.reserve(
                    sensitive_key,
                    {
                        BudgetDimension.BRIDGE_TURNS: 1,
                        BudgetDimension.TOOL_SECONDS: 3_600,
                        BudgetDimension.CONTEXT_BYTES: 0,
                        BudgetDimension.CONTEXT_TOKENS: 0,
                    },
                )
                refusal = await composition.advisor_tool.execute(
                    {
                        "consultation_type": "plan_investigation",
                        "question": "Review this synthetic no-sample observation.",
                        "evidence_refs": [str(observation.id)],
                    },
                    ToolExecutionContext(
                        investigation_id=investigation_id,
                        envelope=replace(envelope, budget=sensitive_budget),
                        budget=sensitive_budget,
                        idempotency_key=sensitive_key,
                        available_capabilities=frozenset({"bridge.advisor"}),
                    ),
                )
                assert refusal.status is ToolInvocationStatus.FAILED, label
                assert refusal.error_code == "advisor_secret_field_remaining", label
                assert refusal.output["refusal_code"] == (
                    "autowork_credential_or_signed_url_detected"
                )
                assert refusal.output["confirmed_non_execution"] is True
                secret_findings = refusal.output["secret_findings"]
                assert secret_findings, label
                assert refusal.output["redacted_secret_count"] == len(secret_findings), label
                assert all(
                    set(finding) == {"location", "type"}
                    and finding["location"].startswith("$")
                    and finding["type"] in {"configured_credential", "signed_url"}
                    and marker not in finding["location"]
                    for finding in secret_findings
                ), label
                assert marker not in str(refusal.output)
        assert transport.calls == []
        assert transport.real_network_calls == 0

        with pytest.raises(AssertionError, match="blocked non-loopback"):
            async with httpx.AsyncClient(trust_env=False, timeout=0.2) as external_client:
                await external_client.get("http://example.invalid")
        with pytest.raises(AssertionError, match="blocked non-loopback"):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
                udp_socket.sendto(b"synthetic-egress-probe", ("203.0.113.9", 9))
        with pytest.raises(AssertionError, match="blocked non-loopback"):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
                udp_socket.sendmsg(
                    [b"synthetic-sendmsg-egress-probe"],
                    [],
                    0,
                    ("203.0.113.9", 9),
                )

        async with uow_factory() as uow:
            latest_state = await uow.investigation_states.get(investigation_id)
        assert latest_state is not None
        missing_ref = await client.post(
            f"/api/investigations/{investigation_id}/operator/tools/advisor.consult",
            json={
                "schema_version": "advisor-consult.v1",
                "arguments": {
                    "consultation_type": "plan_investigation",
                    "question": "Check evidence completeness before local Advisor dispatch.",
                    "evidence_refs": [str(first.id), str(uuid4())],
                },
                "purpose": "Check one valid synthetic reference and one absent reference.",
                "expected_version": latest_state.version,
            },
            headers={"Idempotency-Key": f"m05d-missing-ref-{investigation_id}"},
        )
        assert missing_ref.status_code == 200, missing_ref.text
        missing_payload = missing_ref.json()
        # Typed pre-transport proof (confirmed non-execution) => FAILED, not NEEDS_REVIEW.
        assert missing_payload["invocation"]["status"] == ToolInvocationStatus.FAILED.value
        assert missing_payload["invocation"]["error_code"] == "tool_failed"
        assert missing_payload["advisor_output"]["advisor_status"] == "invalid_input"
        assert missing_payload["advisor_output"]["confirmed_non_execution"] is True
        assert missing_payload["advisor_output"]["provider_usage"] == {"status": "not_submitted"}
        assert transport.calls == []
        async with uow_factory() as uow:
            latest_state = await uow.investigation_states.get(investigation_id)
        assert latest_state is not None

        allowed = await _invoke_advisor(
            client,
            investigation_id,
            latest_state.version,
            idempotency_key=f"m05d-allowed-fake-send-{investigation_id}",
            evidence_ref=first.id,
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["invocation"]["status"] == ToolInvocationStatus.SUCCEEDED.value
        assert len(transport.calls) == 1
        assert transport.real_network_calls == 0

        composed = await compose_opinion_http(client, investigation_id)
        assert composed.status_code == 200, composed.text
        opinion_service = AnalystOpinionService(
            uow_factory=uow_factory, artifact_store=artifact_store
        )
        draft = await opinion_service.get_draft(investigation_id)
        assert draft is not None and draft.opinion is not None
        revised = await revise_opinion_http(
            client,
            draft.opinion.id,
            expected_version=draft.opinion.version,
            blocks=(
                {
                    "block_type": OpinionBlockType.KEY_FINDINGS.value,
                    "text": "The bounded synthetic claim is supported by its cited observation.",
                    "finding_refs": [str(finding_v2.id)],
                    "evidence_refs": [str(first.id)],
                    "qualification": OpinionClaimQualification.FACT.value,
                    "certainty": 0.95,
                },
            ),
        )
        assert revised.status_code == 200, revised.text
        revised_draft = revised.json()["draft"]
        identity_provider.actor_id = "reviewer:m05d-human"
        approved_opinion = await approve_opinion_http(
            client,
            draft.opinion.id,
            version=revised_draft["version"],
            sha256=revised_draft["content_sha256"],
        )
        assert approved_opinion.status_code == 200, approved_opinion.text
        identity_provider.actor_id = "approver:m05d-human"
        published = await publish_opinion_http(
            client,
            draft.opinion.id,
            version=approved_opinion.json()["approved_version"],
            base_publication_sha256=seeded.publication.input_hash,
        )
        assert published.status_code == 200, published.text
        augmented_id = UUID(published.json()["publication"]["artifact_id"])

        render_service = PublicationRenderService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            media_asset_store=MediaAssetStore(artifact_store, uow_factory),
            renderer=TypstRenderer(),
            compiler=TypstSubprocessCompiler(binary=os.environ.get("TYPST_BINARY", "typst")),
            chp_typst_root=_REPOSITORY_ROOT.parent / "chpTypst",
            font_bundle_root=Path(
                os.environ.get("FONT_BUNDLE_ROOT", str(_REPOSITORY_ROOT.parent / "chpTypst"))
            ),
            typst_fonts_lock_path=_REPOSITORY_ROOT.parent / "infra" / "typst-fonts.lock",
        )
        base_render = await render_service.render_pdf(seeded.publication.id)
        v6_render = await render_service.render_pdf(augmented_id)
        base_lineage = await render_service.lineage_manifest_for_render(base_render)
        v6_lineage = await render_service.lineage_manifest_for_render(v6_render)
        assert base_lineage["nature"] == "base_publication"
        assert v6_lineage["actors"]["reviewer"]["actor_id"] == "reviewer:m05d-human"
        assert v6_lineage["actors"]["approver"]["actor_id"] == "approver:m05d-human"
        pdftotext = shutil.which("pdftotext")
        assert pdftotext is not None
        for label, render in (("base", base_render), ("v6", v6_render)):
            assert render.output_blob_id is not None
            pdf_path = tmp_path / f"m05d-{label}.pdf"
            pdf_bytes = await artifact_store.read_bytes(render.output_blob_id)
            assert pdf_bytes.startswith(b"%PDF-")
            pdf_path.write_bytes(pdf_bytes)
            extracted = await asyncio.to_thread(
                subprocess.run,
                (pdftotext, "-layout", str(pdf_path), "-"),
                capture_output=True,
                check=True,
                timeout=30,
            )
            pdf_text = extracted.stdout.decode("utf-8", errors="replace")
            assert "reviewer:m05d-human" not in pdf_text
            assert "approver:m05d-human" not in pdf_text
            if label == "v6":
                canonical_ref = str(first.id)
                assert pdf_text.count(canonical_ref) == 1
                reference_lines = [line for line in pdf_text.splitlines() if canonical_ref in line]
                assert len(reference_lines) == 1
                assert "[E1]" in reference_lines[0]
                chronology_alpha = "M05D-CHRONOLOGY-ALPHA"
                chronology_beta = "M05D-CHRONOLOGY-BETA"
                assert pdf_text.count(chronology_alpha) == 1
                assert pdf_text.count(chronology_beta) == 1
                assert pdf_text.index(chronology_alpha) < pdf_text.index(chronology_beta)

        async with uow_factory() as uow:
            current_envelope = await uow.investigation_envelopes.get(investigation_id)
            latest_state = await uow.investigation_states.get(investigation_id)
        assert current_envelope is not None and latest_state is not None
        previous_provider = app.state.identity_provider
        app.state.identity_provider = _HumanIdentityProvider()
        revoked = await client.post(
            f"/api/investigations/{investigation_id}/envelope/revoke",
            json={
                "expected_version": current_envelope.version,
                "reason": "Synthetic no-sample test completes the authorized consultation.",
                "correlation_id": f"m05d-envelope-revoke:{investigation_id}",
            },
            headers={"Idempotency-Key": f"m05d-envelope-revoke-{investigation_id}"},
        )
        app.state.identity_provider = previous_provider
        assert revoked.status_code == 200, revoked.text
        revoked_attempt = await _invoke_advisor(
            client,
            investigation_id,
            latest_state.version,
            idempotency_key=f"m05d-revoked-advisor-{investigation_id}",
            evidence_ref=first.id,
        )
        assert revoked_attempt.status_code == 200, revoked_attempt.text
        assert (
            revoked_attempt.json()["invocation"]["status"] == ToolInvocationStatus.UNAVAILABLE.value
        )
        assert revoked_attempt.json()["invocation"]["error_code"] == "tool_unavailable"
        assert (
            revoked_attempt.json()["output"]["error"]["reason"] == "external_llm_disclosure_denied"
        )
        assert len(transport.calls) == 1
        assert transport.real_network_calls == 0


@pytest.mark.asyncio
async def test_sample_disclosure_veto_matrix_refuses_before_fake_send_postgres(
    uow_factory, tmp_path: Path, block_non_loopback_sockets
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "m05d-owner-restriction"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
        include_seed=False,
    )
    investigation_id = seeded.investigation.id
    transport = _ReservationTransport(uow_factory, investigation_id, [])
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=investigation_settings(providers=True),
        transport=transport,
    )
    app, _, _ = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    app.state.identity_provider = _OperatorClientIdentityProvider()

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
        previous_provider = app.state.identity_provider
        app.state.identity_provider = _HumanIdentityProvider()
        approved = await approve_envelope_http(
            client,
            investigation_id,
            providers=("bridge",),
            budget_limits={"bridge_turns": 20, "tool_seconds": 72_000},
            allow_acquisition=False,
        )
        app.state.identity_provider = previous_provider
        assert approved.status_code == 200, approved.text
        veto_cases = (
            ("tlp-red", {"tlp": TLP.RED}, "tlp_red"),
            (
                "owner-restriction",
                {"license_restriction": "Synthetic owner/source restriction fixture."},
                "license_restriction",
            ),
        )
        for label, policy, expected_source in veto_cases:
            sample, association, observation = await _persist_sample_policy_fixture(
                uow_factory,
                artifact_store,
                investigation_id=investigation_id,
                subject_id=seeded.subject.id,
                label=label,
                **policy,
            )
            async with uow_factory() as uow:
                state = await uow.investigation_states.get(investigation_id)
                stored_samples = await uow.samples.list_for_subject(seeded.subject.id)
                stored_sample = next(
                    item
                    for item in stored_samples
                    if item.expected_hash == association.sample_sha256
                )
            assert state is not None
            assert stored_sample == sample
            assert stored_sample.expected_hash == association.sample_sha256

            refused = await client.post(
                f"/api/investigations/{investigation_id}/operator/tools/advisor.consult",
                headers={"Idempotency-Key": f"m05d-sample-veto:{label}:{association.id}"},
                json={
                    "schema_version": "advisor-consult.v1",
                    "arguments": {
                        "consultation_type": "draft_text",
                        "question": "Review this persisted inert sample policy fixture.",
                        "evidence_refs": [str(observation.id)],
                    },
                    "purpose": "Verify Advisor refuses this inert restricted sample association.",
                    "expected_version": state.version,
                },
            )
            assert refused.status_code == 200, refused.text
            body = refused.json()
            assert body["invocation"]["status"] == ToolInvocationStatus.FAILED.value
            assert body["invocation"]["error_code"] == "tool_failed"
            advisor_output = body["advisor_output"]
            assert advisor_output["advisor_status"] == "disclosure_restricted"
            assert advisor_output["preflight_status"] == "POLICY_BLOCKED"
            assert advisor_output["failure_stage"] == "sample_disclosure_restricted"
            assert advisor_output["failure_reason"] == "explicit_sample_restriction"
            trace = next(
                item
                for item in advisor_output["disclosure_restricted_sample_associations"]
                if item["association_id"] == str(association.id)
            )
            assert trace["restriction_sources"] == [expected_source]
            assert trace["linked_observation_refs"] == [str(observation.id)]
            assert transport.calls == []
            assert transport.real_network_calls == 0


@pytest.mark.asyncio
async def test_uncertain_external_turn_job_replay_preserves_audit_postgres(
    uow_factory, migrated_postgres_url: str, tmp_path: Path, block_non_loopback_sockets
) -> None:
    """Terminal redelivery is proven; in-flight crash recovery is NOT_DEMONSTRATED."""
    blob_store = FilesystemBlobStore(tmp_path / "m05d-uncertain-replay")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
    )
    seeded = await _add_timeline_to_seeded_publication(uow_factory, artifact_store, seeded)
    adapter = _UncertainModelAdapter()
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        openai_drafting=adapter,
        openai_critic=adapter,
        qwen=adapter,
        fake=adapter,
    )
    gateway = ModelGateway(
        router,
        uow_factory,
        BlobModelOutputStore(BlobCatalogService(blob_store, uow_factory)),
    )
    model_conversations = ModelConversationService(
        uow_factory,
        gateway,
        blob_store,
    )
    factory = InvestigationCompositionFactory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_conversations=model_conversations,
        settings=investigation_settings(providers=False),
        turn_transport=ModelConversationTurnTransport(model_conversations),
    )
    app, registry, dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    investigation_id = seeded.investigation.id
    job_id: UUID

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await start_investigation_http(client, seeded)
        job_id = UUID(started["job_id"])
        approved = await approve_envelope_http(
            client,
            investigation_id,
            providers=(),
            budget_limits={
                "bridge_turns": 4,
                "vt_units": 0,
                "shodan_credits": 0,
                "downloads": 0,
                "tool_seconds": 20_000,
            },
            allow_acquisition=False,
        )
        assert approved.status_code == 200, approved.text
        async with uow_factory() as uow:
            current_publication = await uow.production_artifacts.get_current(
                seeded.run.id, ProductionArtifactStage.PUBLICATION.value
            )
        assert current_publication is not None
        assert current_publication.canonical_blob_id is not None
        chronology_before = await artifact_store.read_bytes(current_publication.canonical_blob_id)

        await run_http_job(app, uow_factory, registry, dispatcher, job_id)

    engine = create_postgres_engine(migrated_postgres_url)
    try:
        session_factory = create_session_factory(engine)

        async def read_model_snapshot():
            async with uow_factory() as uow:
                conversation = await uow.model_conversations.get(
                    make_investigation_conversation_id(investigation_id)
                )
                assert conversation is not None
                turns = await uow.model_conversation_turns.list_for_conversation(conversation.id)
                runs = []
                for turn in turns:
                    run = await uow.model_runs.get(turn.model_run_id)
                    if run is not None:
                        runs.append(run)
                return conversation, tuple(turns), tuple(runs)

        audit_before = await read_investigation_audit_snapshot(
            session_factory, investigation_id, artifact_store
        )
        model_snapshot_before = await read_model_snapshot()
        async with uow_factory() as uow:
            state_before = await uow.investigation_states.get(investigation_id)
            job_before = await uow.jobs.get(job_id)
        assert state_before is not None and job_before is not None
        assert state_before.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
        assert job_before.status is JobStatus.SUCCEEDED
        assert adapter.fake_sends == 1
        assert audit_before.envelope is not None
        bridge_reservations = tuple(
            reservation
            for reservation in audit_before.envelope.budget.reservations
            if reservation.amounts.get(BudgetDimension.BRIDGE_TURNS, 0) > 0
        )
        assert len(bridge_reservations) == 1
        reservation_before = bridge_reservations[0]
        assert reservation_before.amounts[BudgetDimension.BRIDGE_TURNS] == 1
        assert reservation_before.committed_amounts[BudgetDimension.BRIDGE_TURNS] == 1
        assert audit_before.envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1
        assert audit_before.hypotheses_state == state_before
        conversation_before, turns_before, model_runs_before = model_snapshot_before
        assert conversation_before.status is ConversationStatus.NEEDS_REVIEW
        assert len(turns_before) == 1
        turn_before = turns_before[0]
        assert turn_before.status is ConversationTurnStatus.NEEDS_REVIEW
        assert turn_before.error_code == "model_submission_reconciliation_required"
        assert len(model_runs_before) == 1
        model_run_before = model_runs_before[0]
        assert model_run_before.status is ModelRunStatus.NEEDS_REVIEW
        assert model_run_before.submission_state is ModelSubmissionState.EXTERNAL_STATE_UNKNOWN
        assert model_run_before.error_details is not None
        assert model_run_before.error_details["reason"] == "synthetic_timeout_after_submission"
        assert model_run_before.error_details["submission_state"] == "external_state_unknown"
        assert model_run_before.error_details["failure_stage"] == "submit"
        assert (
            model_run_before.error_details["failure_reason"] == "synthetic_timeout_after_submission"
        )
        assert model_run_before.error_details["diagnostic_code"] == "synthetic_bridge_timeout"
        assert len(turn_before.input_sha256) == 64
        assert len(model_run_before.authorized_input_hash) == 64
        assert len(model_run_before.evidence_pack_hash) == 64

        audit_dir = tmp_path / "m05d-uncertain-audit"
        first_bundle = write_audit_bundle(audit_before, audit_dir, str(investigation_id))
        resumed_bundle = write_audit_bundle(None, audit_dir, str(investigation_id), resume=True)
        assert resumed_bundle.resumed_from_events is True
        assert resumed_bundle.events == first_bundle.events

        # The investigation is stopped at NEEDS_REVIEW; redelivering its terminal
        # durable job must not repeat the fake external operation or mutate state.
        await run_http_job(app, uow_factory, registry, dispatcher, job_id)
        audit_after = await read_investigation_audit_snapshot(
            session_factory, investigation_id, artifact_store
        )
        model_snapshot_after = await read_model_snapshot()
        async with uow_factory() as uow:
            state_after = await uow.investigation_states.get(investigation_id)
            job_after = await uow.jobs.get(job_id)
            current_publication_after = await uow.production_artifacts.get_current(
                seeded.run.id, ProductionArtifactStage.PUBLICATION.value
            )
        assert state_after is not None and job_after is not None
        assert state_after == state_before
        assert state_after.version == state_before.version
        assert state_after.sha256 == state_before.sha256
        assert state_after.pending_context_sha256 == state_before.pending_context_sha256
        assert state_after.recent_actions == state_before.recent_actions
        assert job_after == job_before
        assert audit_after == audit_before
        assert audit_after.envelope is not None
        assert audit_after.envelope.budget.reservations == audit_before.envelope.budget.reservations
        assert audit_after.envelope.budget.consumed == audit_before.envelope.budget.consumed
        assert model_snapshot_after == model_snapshot_before
        assert adapter.fake_sends == 1
        assert current_publication_after is not None
        assert current_publication_after.canonical_blob_id == current_publication.canonical_blob_id
        assert current_publication_after.canonical_blob_id is not None
        chronology_after = await artifact_store.read_bytes(
            current_publication_after.canonical_blob_id
        )
        assert chronology_after == chronology_before
        rendered_timeline = publication_document_from_json(json.loads(chronology_after))
        assert isinstance(rendered_timeline, PublicationDocumentV5)
        timeline_text = [entry.text for entry in rendered_timeline.document.timeline]
        assert timeline_text.count("M05D-CHRONOLOGY-ALPHA synthetic event.") == 1
        assert timeline_text.count("M05D-CHRONOLOGY-BETA synthetic event.") == 1
    finally:
        await engine.dispose()
