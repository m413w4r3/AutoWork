"""Continuous no-sample E2E-0 driver.

NO_SAMPLE_INVARIANT: this driver creates no samples and asserts that sample
associations are empty. The real ``ProposalConversationService.propose()`` path
is NOT_DEMONSTRATED because it requires origin Samples; only the complementary
terminal-gate check is present. Offline V4 parse+reparse is demonstrated using
a separate synthetic persisted V4 fixture. Authoritative V4 reparse from the
direct consultation writer remains NOT_DEMONSTRATED because it archives v2/v3;
this driver must not relabel that archive. Everything else in this driver is
executed by the PostgreSQL test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine

from cti_app.application.advisor_semantic_parser import (
    ADVISOR_RESPONSE_SCHEMA_V4,
    ADVISOR_SOURCE_PARSER_VERSION_V4,
    parse_advisor_semantic_response,
)
from cti_app.application.analyst_opinion_service import AnalystOpinionService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import (
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.invariant_proposals import (
    ProposalConversationService,
    _parse_proposal_response_details,
)
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_REPARSE_SCHEMA_VERSION,
    ADVISOR_REPARSE_TOOL_NAME,
)
from cti_app.application.investigation_audit_export import (
    load_audit_events,
    write_audit_bundle,
)
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.research_context_builder import ResearchContextBuilder
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.classification import TLP
from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.invariant_proposals import ProposalOutcome
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationExecutionMode,
    InvestigationFinding,
    ToolInvocationStatus,
)
from cti_app.domain.model_runs import BridgeSubmissionState
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.domain.publication import PublicationTimelineEntryV1
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
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
from cti_app.infrastructure.database.session import create_session_factory
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.edition_codes import reserve_edition_code
from tests.test_publication_document_v6 import _base_v5
from tests.workflow_emulator.investigation_support import (
    approve_envelope_http,
    approve_opinion_http,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    investigation_settings,
    publish_opinion_http,
    revise_opinion_http,
)

pytestmark = pytest.mark.integration
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _Actor:
    def __init__(self) -> None:
        self.actor_id = "operator:m05b-driver"

    async def current(self) -> Identity:
        return Identity(
            actor_id=self.actor_id,
            scopes=frozenset({IdentityScope.HUMAN_OPERATOR, IdentityScope.OPERATOR_CLIENT}),
        )


class _ReservationTransport:
    """Local Bridge fake that records the archived prompt and checks G2 reservations."""

    def __init__(self, uow_factory: Any, investigation_id: UUID, responses: list[TurnSubmission]):
        self._uow_factory = uow_factory
        self._investigation_id = investigation_id
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.real_network_calls = 0

    async def submit(self, **kwargs: Any) -> TurnSubmission:
        self.calls.append(dict(kwargs))
        async with self._uow_factory() as uow:
            invocations = await uow.tool_invocations.list_for_investigation(self._investigation_id)
            invocation = next(
                item
                for item in invocations
                if item.tool_name == "advisor.consult"
                and item.status is ToolInvocationStatus.RUNNING
            )
            envelope = await uow.investigation_envelopes.get(self._investigation_id)
            assert envelope is not None
            reservation = next(
                item
                for item in envelope.budget.reservations
                if item.operation_key == invocation.operation_key
            )
            prompt_bytes = len(kwargs["prompt"].encode("utf-8"))
            prompt_tokens = max(1, (prompt_bytes + 3) // 4)
            assert invocation.reserved_budget[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
            assert invocation.reserved_budget[BudgetDimension.CONTEXT_TOKENS] == prompt_tokens
            assert reservation.amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
            assert reservation.amounts[BudgetDimension.CONTEXT_TOKENS] == prompt_tokens
        return self._responses.pop(0)


async def _seed_no_sample_article(
    uow_factory: Any, artifact_store: ProductionArtifactStore, suffix: str
) -> dict[str, Any]:
    from cti_app.domain.editions import Edition
    from cti_app.domain.entities import Subject

    edition = Edition(
        country=f"M05-B synthetic fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Synthetic no-sample investigation fixture",
        slug=f"m05b-no-sample-{suffix}",
        tlp=TLP.CLEAR,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    source_claim = (
        "Synthetic registry notes service alpha at TEST-NET address 203.0.113.8 on "
        "2026-10-02; this fixture contains no sample bytes or sample associations."
    )
    synthesis_payload = {
        "title": "Synthetic no-sample verified synthesis",
        "subject_id": str(subject.id),
        "lead": [{"text": source_claim}],
        "sections": [],
        "timeline": [],
        "uncertainties": ["Synthetic fixture only; no live provider was consulted."],
        "indicators": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="m05b-no-sample-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        canonical_blob_id=synthesis_blob_id,
    )
    base_v5 = _base_v5()
    citation = base_v5.document.lead[0].evidence_refs[0]
    chronology = (
        PublicationTimelineEntryV1(
            event_date=date(2026, 10, 2),
            date_text="2026-10-02",
            text="M05B-CHRONOLOGY-ONE synthetic registry entry.",
            evidence_refs=(citation,),
        ),
        PublicationTimelineEntryV1(
            event_date=date(2026, 10, 3),
            date_text="2026-10-03",
            text="M05B-CHRONOLOGY-TWO synthetic review entry.",
            evidence_refs=(citation,),
        ),
    )
    base_document = replace(
        base_v5.document,
        subject_id=subject.id,
        timeline=chronology,
    )
    preserved_semantics = tuple(
        paragraph
        for paragraph in base_v5.semantic_text.paragraphs
        if not paragraph.anchor.startswith("timeline:")
    )
    timeline_semantics = tuple(
        SemanticParagraphV1(
            anchor=f"timeline:{index:04d}",
            spans=(SemanticTextSpanV1(role=SemanticRole.TEXT, text=item.text),),
        )
        for index, item in enumerate(chronology, start=1)
    )
    base_v5 = replace(
        base_v5,
        document=base_document,
        semantic_text=SemanticTextV1(
            schema_version=base_v5.semantic_text.schema_version,
            policy_version=base_v5.semantic_text.policy_version,
            paragraphs=(*preserved_semantics, *timeline_semantics),
        ),
    )
    base_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(base_v5)
    )
    base_blob_id = await artifact_store.put_bytes(
        base_bytes,
        bucket="m05b-no-sample-publication-v5",
        mime_type="application/json",
    )
    publication = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(base_bytes).hexdigest(),
        canonical_blob_id=base_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(max_cycles=12, max_vt_read_units=0, max_hits_acquired=0),
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [],
        "file_indicators": [],
        "policy": {"tlp": "clear", "do_not_submit": False, "external_llm_allowed": True},
    }
    input_blob_id, input_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="analyst-input-packs"
    )
    investigation.input_pack_blob_id = input_blob_id
    investigation.input_sha256 = input_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_blob_id,
        sha256=input_sha256,
        schema_version="analyst-input-pack-v1",
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.production_artifacts.append(publication)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.commit()
    return {
        "edition": edition,
        "subject": subject,
        "run": run,
        "synthesis": synthesis,
        "publication": publication,
        "base_bytes": base_bytes,
        "base_document": base_v5,
        "investigation": investigation,
        "source_claim": source_claim,
    }


def _advisor_raw(status: str, opinion: str) -> str:
    return (
        "advisor-wire.v1\nSCHEMA: advisor-response.v4\n"
        "TYPE: plan_investigation\n"
        f"STATUS: {status}\n"
        "SUMMARY: Bounded synthetic review based on persisted evidence.\n"
        "ALT: The source record may be incomplete. | refs=E1\n"
        "EXPERIMENT: Compare the cited record with a second synthetic source. "
        "| signal=matching service and date | refs=E1\n"
        "LEAD: The service timeline remains a bounded lead. "
        "| reason=single source | refs=E1\n"
        "RISK: One synthetic source only.\n"
        "GAP: Independent corroboration is absent.\n"
        f"OPINION: {opinion}\nREF: E1\n"
    )


@pytest.mark.asyncio
async def test_investigation_no_sample_postgres_continuous_journey(
    uow_factory, migrated_postgres_url: str, tmp_path: Path
) -> None:
    blob_catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "blob-storage"), uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await _seed_no_sample_article(uow_factory, artifact_store, uuid4().hex[:10])
    investigation_id = seeded["investigation"].id
    foreign_seeded = await _seed_no_sample_article(
        uow_factory, artifact_store, f"foreign-{uuid4().hex[:10]}"
    )
    foreign_investigation_id = foreign_seeded["investigation"].id
    foreign_observation_bytes = ProductionArtifactStore.canonical_json_bytes(
        {
            "schema": "m05b-foreign-audit-observation.v1",
            "claim": "Synthetic row used to prove investigation audit scoping.",
        }
    )
    foreign_blob_id = await artifact_store.put_bytes(
        foreign_observation_bytes,
        bucket="m05b-foreign-audit-observation",
        mime_type="application/json",
    )
    foreign_observation = EvidenceObservation(
        investigation_id=foreign_investigation_id,
        kind=EvidenceKind.CLAIM_SOURCE,
        blob_id=foreign_blob_id,
        sha256=hashlib.sha256(foreign_observation_bytes).hexdigest(),
        source="synthetic-foreign-audit-row",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="foreign_scope_probe",
        object_id=str(foreign_investigation_id),
        limitations=("Synthetic fixture only.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(foreign_observation)
        await uow.commit()
    actor = _Actor()
    transport = _ReservationTransport(
        uow_factory,
        investigation_id,
        [
            TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=_advisor_raw("partial", "A bounded partial review."),
            ),
            TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=_advisor_raw(
                    "succeeded", "The synthetic claim is supported by the cited observation."
                ),
            ),
            TurnSubmission(
                TurnSubmissionStatus.NEEDS_REVIEW,
                error_code="synthetic_timeout",
                submission_state=BridgeSubmissionState.EXTERNAL_STATE_UNKNOWN,
                failure_stage="submit",
                failure_reason="synthetic timeout after submission",
            ),
        ],
    )
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=investigation_settings(providers=True),
        transport=transport,
    )
    app, _registry, _dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    app.state.identity_provider = actor
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            f"/api/subjects/{seeded['subject'].id}/investigations",
            json={
                "production_run_id": str(seeded["run"].id),
                "synthesis_artifact_id": str(seeded["synthesis"].id),
                "synthesis_sha256": seeded["synthesis"].input_hash,
                "execution_mode": InvestigationExecutionMode.OPERATOR_LED.value,
            },
        )
        assert started.status_code == 200, started.text
        async with uow_factory() as uow:
            state = await uow.investigation_states.get(investigation_id)
        assert state is not None

        approved = await approve_envelope_http(
            client,
            investigation_id,
            providers=("bridge",),
            budget_limits={
                "bridge_turns": 6,
                "vt_units": 0,
                "shodan_credits": 0,
                "downloads": 0,
                "tool_seconds": 20000,
            },
            allow_acquisition=False,
            max_acquire_volume=0,
        )
        assert approved.status_code == 200, approved.text
        async with uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(investigation_id)
            snapshot = await factory.article_snapshots.resolve(seeded["synthesis"].id)
            composition = factory.build(uow, investigation_id, operator_mode=True)
            assert envelope is not None and composition.advisor_tool is not None
            assert envelope.disclosure.external_llm_allowed
            assert snapshot.external_llm_allowed and not snapshot.do_not_submit
            from cti_app.application.investigation_tools import ToolExecutionContext

            blocked_budget = InvestigationBudget(limits=envelope.budget.limits)
            blocked_key = f"m05b-policy-block-{investigation_id}"
            blocked_budget.reserve(
                blocked_key,
                {
                    BudgetDimension.BRIDGE_TURNS: 1,
                    BudgetDimension.TOOL_SECONDS: 3600,
                    BudgetDimension.CONTEXT_BYTES: 0,
                    BudgetDimension.CONTEXT_TOKENS: 0,
                },
            )
            policy_block = await composition.advisor_tool.execute(
                {
                    "consultation_type": "plan_investigation",
                    "question": "Policy preflight must stop before transport.",
                    "evidence_refs": [],
                },
                ToolExecutionContext(
                    investigation_id=investigation_id,
                    envelope=replace(
                        envelope,
                        budget=blocked_budget,
                        disclosure=replace(envelope.disclosure, external_llm_allowed=False),
                    ),
                    budget=blocked_budget,
                    idempotency_key=blocked_key,
                    available_capabilities=frozenset({"bridge.advisor"}),
                ),
            )
            assert policy_block.status is ToolInvocationStatus.POLICY_DENIED
        assert transport.calls == []

        observation_payload = {
            "schema": "investigation-tool-observation.v1",
            "tool_name": "synthetic.article_record",
            "schema_version": "1",
            "operation_key": f"m05b-source-{investigation_id}",
            "result": {
                "claim_id": "C1",
                "text": (
                    "Synthetic article record: service alpha was listed at "
                    "203.0.113.8 on 2026-10-02."
                ),
            },
        }
        observation_bytes = ProductionArtifactStore.canonical_json_bytes(observation_payload)
        observation_blob_id = await artifact_store.put_bytes(
            observation_bytes,
            bucket="m05b-synthetic-observations",
            mime_type="application/json",
        )
        observation = EvidenceObservation(
            investigation_id=investigation_id,
            kind=EvidenceKind.CLAIM_SOURCE,
            blob_id=observation_blob_id,
            sha256=hashlib.sha256(observation_bytes).hexdigest(),
            source="synthetic-article-record",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="article_claim",
            object_id="C1",
            limitations=("Synthetic fixture only; no live provider was consulted.",),
        )
        finding_v1 = InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.CLAIM_VERIFICATION,
            assessment=FindingAssessment.QUALIFIED,
            statement="The synthetic article record qualifies the service timeline claim.",
            observation_refs=(observation.id,),
            claim_refs=("C1",),
            scope="Synthetic source record only.",
            limitations=("No external source was checked.",),
        )
        finding_v2 = replace(
            finding_v1,
            id=uuid4(),
            version=2,
            supersedes=finding_v1.id,
            assessment=FindingAssessment.SUPPORTED,
            statement="The synthetic record supports the bounded service timeline claim.",
        )
        async with uow_factory() as uow:
            await uow.evidence_observations.append_if_absent(observation)
            await uow.investigation_findings.append_version(finding_v1, expected_previous_id=None)
            await uow.investigation_findings.append_version(
                finding_v2, expected_previous_id=finding_v1.id
            )
            await uow.commit()

        state = await _read_state(uow_factory, investigation_id)
        declared = await client.post(
            f"/api/investigations/{investigation_id}/operator/tools/hypothesis.declare",
            json={
                "schema_version": "operator-hypothesis-declare.v1",
                "arguments": {
                    "hypothesis_id": "H1",
                    "statement": (
                        "The synthetic source record documents service alpha on the stated date."
                    ),
                },
                "purpose": "Record the bounded hypothesis supported by synthetic evidence.",
                "expected_version": state.version,
            },
            headers={"Idempotency-Key": f"m05b-hypothesis-{investigation_id}"},
        )
        assert declared.status_code == 200, declared.text

        state = await _read_state(uow_factory, investigation_id)
        async with uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(investigation_id)
            assert envelope is not None
            builder = ResearchContextBuilder(
                article_snapshots=factory.article_snapshots,
                observations=uow.evidence_observations,
                findings=uow.investigation_findings,
                invocations=uow.tool_invocations,
                opinions=uow.analyst_opinions,
            )
            from cti_app.application.investigation_tools import ToolExecutionContext

            tool_context = ToolExecutionContext(
                investigation_id=investigation_id,
                envelope=envelope,
                budget=envelope.budget,
                idempotency_key="m05b-context-read",
            )
            full_context = await builder.build(
                investigation_id=investigation_id,
                synthesis_artifact_id=seeded["synthesis"].id,
                state=state,
                tool_context=tool_context,
                budget_remaining={},
            )
            full_context_bytes = full_context.canonical_bytes()
            context_sha256 = hashlib.sha256(full_context_bytes).hexdigest()
            assert hashlib.sha256(full_context_bytes).hexdigest() == context_sha256
            total, full_items = await builder.read_context_section(
                section="evidence_index",
                investigation_id=investigation_id,
                state=state,
                offset=0,
                limit=20,
            )
            pages = []
            cursor = None
            while True:
                page = await builder.read_context_section_page(
                    section="evidence_index",
                    investigation_id=investigation_id,
                    state=state,
                    cursor=cursor,
                    limit=1,
                    context_sha256=context_sha256,
                )
                pages.extend(page.items)
                assert page.context_sha256 == context_sha256
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert total == len(full_items) == len(pages)
            assert tuple(pages) == tuple(full_items)

        partial = await _invoke_advisor(
            client,
            investigation_id,
            state.version,
            idempotency_key=f"m05b-advisor-partial-{investigation_id}",
            evidence_ref=observation.id,
        )
        assert partial.status_code == 200, partial.text
        assert partial.json()["invocation"]["status"] in {
            ToolInvocationStatus.PARTIAL.value,
            ToolInvocationStatus.SUCCEEDED.value,
        }, partial.text
        partial_state = partial.json()["working_state_version"]
        succeeded = await _invoke_advisor(
            client,
            investigation_id,
            partial_state,
            idempotency_key=f"m05b-advisor-success-{investigation_id}",
            evidence_ref=observation.id,
        )
        assert succeeded.status_code == 200, succeeded.text
        assert succeeded.json()["invocation"]["status"] == ToolInvocationStatus.SUCCEEDED.value, (
            succeeded.text
        )
        assert len(transport.calls) == 2
        assert transport.real_network_calls == 0

        async with uow_factory() as uow:
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
            advisor_observations = tuple(
                sorted(
                    (
                        item
                        for item in observations
                        if item.source == "advisor.consult" and item.object_type == "advisor_call"
                    ),
                    key=lambda item: (item.observed_at, item.id),
                )
            )
            assert len(advisor_observations) == 2
            for advisor_observation, captured_call in zip(
                advisor_observations, transport.calls, strict=True
            ):
                archived = json.loads(await artifact_store.read_bytes(advisor_observation.blob_id))
                prompt_bytes = await artifact_store.read_bytes(UUID(archived["prompt_blob_id"]))
                assert prompt_bytes == captured_call["prompt"].encode("utf-8")
                assert hashlib.sha256(prompt_bytes).hexdigest() == archived["prompt_sha256"]
            invocations = await uow.tool_invocations.list_for_investigation(investigation_id)
            assert len([item for item in invocations if item.tool_name == "advisor.consult"]) == 2
            saved_samples = await uow.sample_associations.list_for_investigation(investigation_id)
            assert saved_samples == ()
        advisor_observation = advisor_observations[-1]

        # The current consultation writer records v2/v3 parser metadata, so the
        # operator reparse adapter correctly refuses to label those bytes as V4.
        # This is the required M05-A seam gap: do not relabel the persisted record.
        successful_archive = json.loads(
            await artifact_store.read_bytes(advisor_observation.blob_id)
        )
        assert successful_archive["advisor_response_schema"] != "advisor-response.v4"
        assert (
            successful_archive["output"].get("parser_version") != ADVISOR_SOURCE_PARSER_VERSION_V4
        )
        assert len(transport.calls) == 2

        # Exercise the actual V4 parser and registered advisor.reparse adapter
        # using an independently persisted synthetic fixture. The authoritative
        # advisor.consult archive above remains v2/v3 and is never relabeled.
        v4_original, v4_parse, v4_output_sha256 = await _persist_v4_reparse_fixture(
            uow_factory,
            artifact_store,
            investigation_id,
            observation,
        )
        assert v4_parse["status"] == "succeeded"
        assert v4_parse["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
        assert v4_parse["raw_output_sha256"] == v4_output_sha256
        reparse_state = await _read_state(uow_factory, investigation_id)
        sends_before_reparse = len(transport.calls)
        reparsed = await _invoke_advisor_reparse(
            client,
            investigation_id,
            reparse_state.version,
            observation_id=v4_original.id,
            idempotency_key=f"m05b-advisor-v4-reparse-{investigation_id}",
        )
        assert reparsed.status_code == 200, reparsed.text
        reparsed_payload = reparsed.json()
        assert reparsed_payload["invocation"]["status"] == ToolInvocationStatus.SUCCEEDED.value
        assert reparsed_payload["output"]["kind"] == "advisor_reparse"
        assert reparsed_payload["output"]["status"] == "succeeded"
        reparsed_refs = reparsed_payload["output"]["data"]["observations"]
        assert {item["observation_id"] for item in reparsed_refs} >= {str(v4_original.id)}
        reparsed_ref = next(
            item for item in reparsed_refs if item["observation_id"] != str(v4_original.id)
        )
        assert len(transport.calls) == sends_before_reparse == 2
        assert transport.real_network_calls == 0
        reparsed_observation_id = UUID(reparsed_ref["observation_id"])
        async with uow_factory() as uow:
            reparsed_observation = await uow.evidence_observations.get(reparsed_observation_id)
            assert reparsed_observation is not None
            assert reparsed_observation.source_version == ADVISOR_SOURCE_PARSER_VERSION_V4
            reparsed_archive_bytes = await artifact_store.read_bytes(reparsed_observation.blob_id)
        assert hashlib.sha256(reparsed_archive_bytes).hexdigest() == reparsed_observation.sha256
        reparsed_archive = json.loads(reparsed_archive_bytes)
        assert reparsed_archive["reparsed_from_observation_id"] == str(v4_original.id)
        assert reparsed_archive["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
        assert reparsed_archive["bridge_turns_consumed"] == 0
        assert reparsed_archive["output"]["output_sha256"] == v4_output_sha256
        assert reparsed_archive["output"]["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
        assert reparsed_archive["output"]["status"] == "succeeded"

        refused_wire = (
            "advisor-wire.v1\nTYPE: p10_proposal\nSTATUS: refused\n"
            "REFUSAL_CODE: insufficient_evidence\n"
            "REFUSAL_REASON: Insufficient synthetic support.\n"
            "CANDIDATE: literal_string | c2_indicator | exact | CreateMutexW | "
            "Synthetic unsupported candidate. | refs=unknown\n"
        )
        # Complementary parser/P09 terminal-gate proof only. The real P10
        # propose() path requires retained origin Samples, which this no-sample
        # investigation intentionally does not create.
        parsed_refusal = _parse_proposal_response_details(refused_wire)
        assert parsed_refusal.outcome is ProposalOutcome.REFUSED

        class _P09Registry:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def propose(self, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                raise AssertionError("refused P10 response must not reach P09")

        p09 = _P09Registry()
        proposal_service = ProposalConversationService(None, None, p09)  # type: ignore[arg-type]
        terminal_results = await proposal_service._process_response_for_p09(
            outcome=parsed_refusal.outcome,
            response=parsed_refusal.response,
            investigation_id=investigation_id,
            cycle_number=1,
            sample_ids=(),
            provenance_by_ref={},
        )
        assert terminal_results == ()
        assert p09.calls == []

        # Human opinion revision and publication augmentation remain on this same investigation.
        composed = await compose_opinion_http(client, investigation_id)
        assert composed.status_code == 200, composed.text
        opinion_service = AnalystOpinionService(
            uow_factory=uow_factory, artifact_store=artifact_store
        )
        draft = await opinion_service.get_draft(investigation_id)
        assert draft is not None
        assert draft.opinion is not None
        opinion_id = draft.opinion.id
        blocks = tuple(
            {
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "text": (
                    f"The supported synthetic claim is linked to evidence SHA-256 "
                    f"{observation.sha256}."
                ),
                "finding_refs": [str(finding_v2.id)],
                "evidence_refs": [str(observation.id)],
                "qualification": OpinionClaimQualification.FACT.value,
                "certainty": 0.95,
            }
            for _ in (0,)
        )
        revised = await revise_opinion_http(
            client,
            opinion_id,
            expected_version=draft.opinion.version,
            blocks=blocks,
        )
        assert revised.status_code == 200, revised.text
        revised_payload = revised.json()["draft"]
        actor.actor_id = "reviewer:m05b-human"
        approved_opinion = await approve_opinion_http(
            client,
            opinion_id,
            version=revised_payload["version"],
            sha256=revised_payload["content_sha256"],
        )
        assert approved_opinion.status_code == 200, approved_opinion.text
        approved_version = approved_opinion.json()["approved_version"]
        actor.actor_id = "approver:m05b-human"
        published = await publish_opinion_http(
            client,
            opinion_id,
            version=approved_version,
            base_publication_sha256=seeded["publication"].input_hash,
        )
        assert published.status_code == 200, published.text
        augmented_id = UUID(published.json()["publication"]["artifact_id"])
        async with uow_factory() as uow:
            base_artifact = await uow.production_artifacts.get(seeded["publication"].id)
            augmented = await uow.production_artifacts.get(augmented_id)
            opinion = await uow.analyst_opinions.get(opinion_id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            assert base_artifact == seeded["publication"]
            assert augmented is not None
            assert augmented.metadata["previous_publication_artifact_id"] == str(base_artifact.id)
            assert opinion is not None
            reviewer_decision = next(
                item
                for item in decisions
                if item.decision_type is AnalystDecisionType.NOTE_APPROVE
                and item.target_type is AnalystDecisionTargetType.NOTE
                and item.target_id == opinion_id
            )
            assert reviewer_decision.actor_id == "reviewer:m05b-human"
        v6_payload = json.loads(await artifact_store.read_bytes(augmented.canonical_blob_id))
        v6 = publication_document_from_json(v6_payload)
        assert isinstance(v6, PublicationDocumentV6)
        assert v6.base_publication == seeded["base_document"]
        assert v6.analyst_opinion is not None
        assert any(
            observation.id in block.evidence_refs and finding_v2.id in block.finding_refs
            for block in v6.analyst_opinion.blocks
        )

        # One deterministic audit snapshot, then resume from its checkpoint.
        engine = create_async_engine(migrated_postgres_url)
        try:
            session_factory = create_session_factory(engine)
            async with uow_factory() as uow:
                foreign_rows = await uow.evidence_observations.list_for_investigation(
                    foreign_investigation_id
                )
            assert foreign_observation in foreign_rows
            audit_snapshot = await read_investigation_audit_snapshot(
                session_factory, investigation_id, artifact_store
            )
            assert audit_snapshot.investigation_id == str(investigation_id)
            for field_name in (
                "observations",
                "findings",
                "invocations",
                "experiments",
                "opinions",
                "detection_candidates",
                "sample_associations",
                "analyst_decisions",
            ):
                assert all(
                    item.investigation_id == investigation_id
                    for item in getattr(audit_snapshot, field_name)
                )
            assert foreign_observation.id not in {item.id for item in audit_snapshot.observations}
            audit_dir = tmp_path / "audit-export"
            first_bundle = write_audit_bundle(audit_snapshot, audit_dir, str(investigation_id))
            resumed_bundle = write_audit_bundle(None, audit_dir, str(investigation_id), resume=True)
            assert not first_bundle.resumed_from_events
            assert resumed_bundle.resumed_from_events
            assert first_bundle.events == resumed_bundle.events
            target_events = load_audit_events(
                audit_dir / "RUN_EVENTS.jsonl.gz", str(investigation_id)
            )
            assert target_events
            assert all(
                item["correlation"]["investigation_id"] == str(investigation_id)
                for item in target_events
            )
            assert all(
                item["record"]["id"] != str(foreign_observation.id) for item in target_events
            )
            assert str(foreign_observation.id) not in json.dumps(target_events, sort_keys=True)
            assert any(
                item["type"] == "evidence_observation.recorded"
                and item["record"]["id"] == str(observation.id)
                for item in target_events
            )
        finally:
            await engine.dispose()

        # Compile both canonical publication versions and check chronology and lineage.
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
        base_render = await render_service.render_pdf(seeded["publication"].id)
        v6_render = await render_service.render_pdf(augmented.id)
        base_lineage = await render_service.lineage_manifest_for_render(base_render)
        v6_lineage = await render_service.lineage_manifest_for_render(v6_render)
        assert base_lineage["nature"] == "base_publication"
        assert v6_lineage["actors"]["reviewer"]["actor_id"] == "reviewer:m05b-human"
        assert v6_lineage["actors"]["approver"]["actor_id"] == "approver:m05b-human"
        pdftotext = shutil.which("pdftotext")
        assert pdftotext is not None
        for label, render in (("base", base_render), ("v6", v6_render)):
            assert render.output_blob_id is not None
            pdf_path = tmp_path / f"{label}.pdf"
            pdf_path.write_bytes(await artifact_store.read_bytes(render.output_blob_id))
            extracted = await asyncio.to_thread(
                subprocess.run,
                (pdftotext, "-layout", str(pdf_path), "-"),
                capture_output=True,
                check=True,
                timeout=30,
            )
            text = extracted.stdout.decode("utf-8", errors="replace")
            assert text.count("M05B-CHRONOLOGY-ONE") == 1
            assert text.count("M05B-CHRONOLOGY-TWO") == 1
            assert text.index("M05B-CHRONOLOGY-ONE") < text.index("M05B-CHRONOLOGY-TWO")
            assert "reviewer:m05b-human" not in text
            assert "approver:m05b-human" not in text
            if label == "v6":
                assert text.count(str(observation.id)) == 1

        # A fresh composition replays the uncertain operation key without another submission.
        uncertain_state = await _read_state(uow_factory, investigation_id)
        uncertain_key = f"m05b-advisor-uncertain-{investigation_id}"
        uncertain = await _invoke_advisor(
            client,
            investigation_id,
            uncertain_state.version,
            idempotency_key=uncertain_key,
            evidence_ref=observation.id,
        )
        assert uncertain.status_code == 200, uncertain.text
        assert uncertain.json()["invocation"]["status"] == ToolInvocationStatus.NEEDS_REVIEW.value
        assert len(transport.calls) == 3
        uncertain_invocation_id = UUID(uncertain.json()["invocation"]["id"])
        before_replay = await _read_invocation_replay_snapshot(
            migrated_postgres_url,
            artifact_store,
            investigation_id,
            uncertain_invocation_id,
        )
        invocation_before = before_replay["invocation"]
        reservation_before = before_replay["reservation"]
        archive_before = before_replay["advisor_archive"]
        assert invocation_before.status is ToolInvocationStatus.NEEDS_REVIEW
        assert reservation_before.operation_key == invocation_before.operation_key
        assert reservation_before.amounts == invocation_before.reserved_budget
        assert archive_before["advisor_status"] == "needs_review"
        assert archive_before["submission_status"] == TurnSubmissionStatus.NEEDS_REVIEW.value
        assert (
            archive_before["submission_state"] == BridgeSubmissionState.EXTERNAL_STATE_UNKNOWN.value
        )
        assert archive_before["failure_stage"] == "submit"
        assert archive_before["failure_reason"] == "synthetic timeout after submission"
        for hash_key in ("prompt_sha256", "dossier_sha256"):
            assert isinstance(archive_before[hash_key], str)
            assert len(archive_before[hash_key]) == 64
        replay_transport = _ReservationTransport(uow_factory, investigation_id, [])
        replay_factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            settings=investigation_settings(providers=True),
            transport=replay_transport,
        )
        replay_app, _, _ = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            composition_factory=replay_factory,
        )
        replay_app.state.identity_provider = actor
        async with httpx.AsyncClient(
            transport=ASGITransport(app=replay_app), base_url="http://test"
        ) as replay_client:
            replay = await _invoke_advisor(
                replay_client,
                investigation_id,
                uncertain_state.version,
                idempotency_key=uncertain_key,
                evidence_ref=observation.id,
            )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["invocation"]["id"] == uncertain.json()["invocation"]["id"]
        after_replay = await _read_invocation_replay_snapshot(
            migrated_postgres_url,
            artifact_store,
            investigation_id,
            uncertain_invocation_id,
        )
        invocation_after = after_replay["invocation"]
        reservation_after = after_replay["reservation"]
        archive_after = after_replay["advisor_archive"]
        assert after_replay == before_replay
        assert invocation_after.id == invocation_before.id == uncertain_invocation_id
        assert invocation_after.status is ToolInvocationStatus.NEEDS_REVIEW
        assert invocation_after.version == invocation_before.version
        assert invocation_after.schema_version == invocation_before.schema_version
        assert invocation_after.reserved_budget == invocation_before.reserved_budget
        assert invocation_after.consumed_budget == invocation_before.consumed_budget
        assert reservation_after.amounts == reservation_before.amounts
        assert reservation_after.committed_amounts == reservation_before.committed_amounts
        assert reservation_after.status == reservation_before.status
        assert archive_after["failure_stage"] == archive_before["failure_stage"]
        assert archive_after["failure_reason"] == archive_before["failure_reason"]
        for hash_key in ("prompt_sha256", "dossier_sha256", "output_sha256"):
            assert archive_after.get(hash_key) == archive_before.get(hash_key)
        assert after_replay["audit"].conversation == before_replay["audit"].conversation
        assert after_replay["audit"].conversation_turns == before_replay["audit"].conversation_turns
        assert after_replay["audit"].model_runs == before_replay["audit"].model_runs
        assert after_replay["audit"].source_versions == before_replay["audit"].source_versions
        assert replay_transport.calls == []
        assert len(transport.calls) == 3
        assert transport.real_network_calls == replay_transport.real_network_calls == 0


async def _read_state(uow_factory: Any, investigation_id: UUID) -> Any:
    async with uow_factory() as uow:
        state = await uow.investigation_states.get(investigation_id)
    assert state is not None
    return state


async def _read_invocation_replay_snapshot(
    migrated_postgres_url: str,
    artifact_store: ProductionArtifactStore,
    investigation_id: UUID,
    invocation_id: UUID,
) -> dict[str, Any]:
    engine = create_async_engine(migrated_postgres_url)
    try:
        session_factory = create_session_factory(engine)
        audit = await read_investigation_audit_snapshot(
            session_factory, investigation_id, artifact_store
        )
    finally:
        await engine.dispose()
    invocation = next(item for item in audit.invocations if item.id == invocation_id)
    assert audit.envelope is not None
    reservation = next(
        item
        for item in audit.envelope.budget.reservations
        if item.operation_key == invocation.operation_key
    )
    advisor_observations = tuple(
        item
        for item in audit.observations
        if item.id in invocation.response_refs
        and item.source == "advisor.consult"
        and item.object_type == "advisor_call"
    )
    assert len(advisor_observations) == 1
    advisor_observation = advisor_observations[0]
    archive_bytes = await artifact_store.read_bytes(advisor_observation.blob_id)
    assert hashlib.sha256(archive_bytes).hexdigest() == advisor_observation.sha256
    advisor_archive = json.loads(archive_bytes)
    return {
        "audit": audit,
        "invocation": invocation,
        "reservation": reservation,
        "advisor_archive_bytes": archive_bytes,
        "advisor_archive": advisor_archive,
    }


async def _persist_v4_reparse_fixture(
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    investigation_id: UUID,
    source_observation: EvidenceObservation,
) -> tuple[EvidenceObservation, dict[str, Any], str]:
    reference_id = str(source_observation.id)
    input_refs = [
        {
            "kind": "observation",
            "id": reference_id,
            "sha256": source_observation.sha256,
        }
    ]
    prompt_data = {
        "consultation_type": "draft_text",
        "question": "Review the synthetic no-sample source observation.",
        "transmitted_refs": input_refs,
        "evidence": [
            {
                "evidence_id": reference_id,
                "sha256": source_observation.sha256,
                "object_type": "observation",
                "content_excerpts": [
                    {
                        "kind": "json",
                        "title": "Synthetic no-sample source observation",
                        "text": await artifact_store.read_text(source_observation.blob_id),
                    }
                ],
            }
        ],
    }
    raw_text = json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V4,
            "consultation_type": "draft_text",
            "status": "succeeded",
            "opinions": [
                {
                    "unit_id": "U1",
                    "text": "Review the synthetic source observation.",
                    "source_aliases": ["E1"],
                }
            ],
            "incomplete_units": [],
        },
        ensure_ascii=False,
    )
    parsed = parse_advisor_semantic_response(
        raw_text,
        consultation_type="draft_text",
        prompt_data=prompt_data,
    )
    raw_bytes = raw_text.encode("utf-8")
    output_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    await artifact_store.put_bytes(
        raw_bytes,
        bucket="model-outputs",
        mime_type="text/plain; charset=utf-8",
    )
    input_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(prompt_data)
    ).hexdigest()
    bridge_invocation_id = str(uuid4())
    bridge_provenance = {
        "bridge_invocation_id": bridge_invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "input_sha256": input_sha256,
        "input_refs": input_refs,
        "output_sha256": output_sha256,
        "provider_usage": {"status": "synthetic_fixture"},
    }
    output = {
        **parsed,
        "output_sha256": output_sha256,
        "bridge_provenance": bridge_provenance,
    }
    original_payload = {
        "schema": "operator-advisor-call.v1",
        "advisor_response_schema": ADVISOR_RESPONSE_SCHEMA_V4,
        "consultation_type": "draft_text",
        "advisor_status": parsed["status"],
        "bridge_invocation_id": bridge_invocation_id,
        "bridge_invocation_id_kind": "conversation_turn",
        "idempotency_key": f"m05b-v4-reparse-fixture-{uuid4()}",
        "input_sha256": input_sha256,
        "input_refs": input_refs,
        "input_observation_refs": [reference_id],
        "input_source_refs": [],
        "output_sha256": output_sha256,
        "provider_usage": {"status": "synthetic_fixture"},
        "output": output,
        "reparse_context": prompt_data,
    }
    archive_bytes = ProductionArtifactStore.canonical_json_bytes(original_payload)
    archive_blob_id = await artifact_store.put_bytes(
        archive_bytes,
        bucket="m05b-v4-reparse-original",
        mime_type="application/json",
    )
    original_observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=archive_blob_id,
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        source="advisor.consult",
        source_version=ADVISOR_SOURCE_PARSER_VERSION_V4,
        observed_at=datetime.now(UTC),
        object_type="advisor_call",
        object_id="draft_text",
        limitations=("Synthetic V4 parser fixture; no transport call was made.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(original_observation)
        await uow.commit()
    return original_observation, parsed, output_sha256


async def _invoke_advisor(
    client: httpx.AsyncClient,
    investigation_id: UUID,
    expected_version: int,
    *,
    idempotency_key: str,
    evidence_ref: UUID | None,
) -> httpx.Response:
    arguments: dict[str, Any] = {
        "consultation_type": "plan_investigation",
        "question": "Review the synthetic article claim and state its support and limits.",
    }
    if evidence_ref is not None:
        arguments["evidence_refs"] = [str(evidence_ref)]
    return await client.post(
        f"/api/investigations/{investigation_id}/operator/tools/advisor.consult",
        json={
            "schema_version": "advisor-consult.v1",
            "arguments": arguments,
            "purpose": "Review one synthetic, no-sample article claim.",
            "expected_version": expected_version,
        },
        headers={"Idempotency-Key": idempotency_key},
    )


async def _invoke_advisor_reparse(
    client: httpx.AsyncClient,
    investigation_id: UUID,
    expected_version: int,
    *,
    observation_id: UUID,
    idempotency_key: str,
) -> httpx.Response:
    return await client.post(
        f"/api/investigations/{investigation_id}/operator/tools/{ADVISOR_REPARSE_TOOL_NAME}",
        json={
            "schema_version": ADVISOR_REPARSE_SCHEMA_VERSION,
            "arguments": {
                "observation_id": str(observation_id),
                "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
            },
            "purpose": "Locally reparse a separately persisted synthetic V4 fixture.",
            "expected_version": expected_version,
        },
        headers={"Idempotency-Key": idempotency_key},
    )
