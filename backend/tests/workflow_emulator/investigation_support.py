"""No-network PostgreSQL helpers shared by investigation workflow proofs."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from cti_app.api.analyst_opinion import router as opinion_router
from cti_app.api.investigation import router as investigation_router
from cti_app.application.bridge_investigator import ScriptedTurnTransport, TurnSubmission
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_composition import (
    InvestigationCompositionFactory,
    OptionalInvestigationToolPorts,
)
from cti_app.application.investigation_jobs import make_investigation_runner
from cti_app.application.jobs import JobExecutor, SynchronousJobDispatcher, create_job_registry
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.static_analysis import StaticAnalysisService
from cti_app.application.virustotal import VirusTotalCapabilities
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.editions import Edition
from cti_app.domain.entities import (
    Sample,
    SampleState,
    Subject,
)
from cti_app.domain.entities import (
    SampleOrigin as EntitySampleOrigin,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
)
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    SampleAcquisitionAttempt,
    SampleAcquisitionOutcome,
    SampleAcquisitionReason,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSourceV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from tests.integration.edition_codes import reserve_edition_code
from tests.shodan_support import FakeShodanPort
from tests.test_investigation_vt_tools import (
    FakeVirusTotalPort,
    _file_report,
    _search_result,
)
from tests.test_publication_document_v6 import _base_v5

DEFAULT_SEED_HASH = "a" * 64
DEFAULT_IOC = "203.0.113.10"
DEFAULT_SAMPLE_BYTES = b"MZ\x00\x00synthetic harmless second-stage sample\x00fixture-only"
DEFAULT_SAMPLE_HASH = hashlib.sha256(DEFAULT_SAMPLE_BYTES).hexdigest()


@dataclass(frozen=True, slots=True)
class SyntheticSampleMaterial:
    """Unique inert bytes and identifiers for one PostgreSQL workflow scenario."""

    seed_hash: str
    sample_bytes: bytes
    sample_hash: str
    ip: str = DEFAULT_IOC

    @classmethod
    def unique(cls) -> SyntheticSampleMaterial:
        token = uuid4().bytes
        seed_hash = hashlib.sha256(b"article-seed:" + token).hexdigest()
        sample_bytes = (
            b"MZ\x00\x00AUTOWORK-SYNTHETIC-LOADER-PAYLOAD-" + token + b"\x00NO-EXECUTION\x00"
        )
        return cls(
            seed_hash=seed_hash,
            sample_bytes=sample_bytes,
            sample_hash=hashlib.sha256(sample_bytes).hexdigest(),
            ip=f"198.51.100.{(token[0] % 200) + 1}",
        )


class HashCheckedVirusTotal(FakeVirusTotalPort):
    """No-network VT fake whose acquired bytes match the returned SHA-256."""

    def __init__(
        self,
        *,
        material: SyntheticSampleMaterial | None = None,
        include_candidate: bool = True,
        relationships: list[Any] | None = None,
        extra_samples: Mapping[str, bytes] | None = None,
    ) -> None:
        super().__init__()
        self.material = material or SyntheticSampleMaterial(
            seed_hash=DEFAULT_SEED_HASH,
            sample_bytes=DEFAULT_SAMPLE_BYTES,
            sample_hash=DEFAULT_SAMPLE_HASH,
        )
        self.report = _file_report(self.material.seed_hash)
        self.download_calls = 0
        self.downloaded_hashes: list[str] = []
        self.relationship_call_args: list[tuple[str, str, str]] = []
        self.relationship_pages = list(relationships or ())
        self._samples = {
            self.material.sample_hash: self.material.sample_bytes,
            **(extra_samples or {}),
        }
        self.search_pages = (
            [
                _search_result(
                    tuple(
                        {
                            "id": sample_hash,
                            "type": "file",
                            "attributes": {
                                "meaningful_name": "synthetic-stage-two.bin",
                                "type_description": "Win32 EXE",
                                "size": len(sample_bytes),
                            },
                        }
                        for sample_hash, sample_bytes in self._samples.items()
                    )
                )
            ]
            if include_candidate
            else []
        )

    async def object_relationship(
        self, object_type: Any, object_id: str, relation: str, **kwargs: Any
    ):
        self.relationship_call_args.append((str(object_type), object_id, relation))
        return await super().object_relationship(object_type, object_id, relation, **kwargs)

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        sample_bytes = self._samples.get(file_hash)
        assert sample_bytes is not None
        assert hashlib.sha256(sample_bytes).hexdigest() == file_hash
        self.download_calls += 1
        self.downloaded_hashes.append(file_hash)
        sink.write(sample_bytes)


FinalResponseBuilder = Callable[
    [tuple[EvidenceObservation, ...]],
    Awaitable[Mapping[str, Any] | TurnSubmission],
]


class ScriptedInvestigationTransport(ScriptedTurnTransport):
    """Scripted Bridge with optional DB-aware analysis and final-evidence turns."""

    def __init__(
        self,
        responses: Sequence[str | Mapping[str, Any] | TurnSubmission],
        *,
        uow_factory: Any,
        investigation_id: UUID,
        material: SyntheticSampleMaterial,
        inject_analysis_at_turn: int | None = 3,
        analyze_pending_samples: bool = False,
        final_response_builder: FinalResponseBuilder | None = None,
        include_external_pivot: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._investigation_id = investigation_id
        self._material = material
        self._inject_analysis_at_turn = inject_analysis_at_turn
        self._analyze_pending_samples = analyze_pending_samples
        self._injected: set[int] = set()
        self._final_response_builder = final_response_builder
        self._include_external_pivot = include_external_pivot
        super().__init__(responses)

    def enqueue(self, *responses: Mapping[str, Any]) -> None:
        self._responses.extend(responses)

    async def _pending_analysis(self) -> Mapping[str, Any] | None:
        async with self._uow_factory() as uow:
            associations = await uow.sample_associations.list_for_investigation(
                self._investigation_id
            )
            invocations = await uow.tool_invocations.list_for_investigation(self._investigation_id)
            observations = await uow.evidence_observations.list_for_investigation(
                self._investigation_id
            )
        analyzed = {
            str(item.normalized_parameters.get("association_id"))
            for item in invocations
            if item.tool_name == "sample.analyze"
        }
        association = next((item for item in associations if str(item.id) not in analyzed), None)
        if association is None:
            return None
        evidence_ref = next(
            (item.id for item in observations if item.object_id == association.sample_sha256),
            None,
        )
        if evidence_ref is None:
            evidence_ref = next(
                (
                    item.id
                    for item in observations
                    if item.object_type == "sha256" and item.object_id == self._material.seed_hash
                ),
                None,
            )
        if evidence_ref is None:
            raise AssertionError("Synthetic sample analysis has no persisted evidence reference")
        return _decision(
            "Analyze the quarantined synthetic sample locally.",
            tool_call={
                "tool_name": "sample.analyze",
                "schema_version": "1",
                "purpose": "Run bounded static analysis on the quarantined sample.",
                "arguments": {
                    "association_id": str(association.id),
                    "purpose": "Inspect the inert synthetic sample locally.",
                    "evidence_refs": [str(evidence_ref)],
                },
            },
        )

    async def submit(self, **kwargs: Any):
        turn_index = int(kwargs["turn_index"])
        if self._analyze_pending_samples:
            pending = await self._pending_analysis()
            if pending is not None:
                self._responses.appendleft(pending)
        elif self._inject_analysis_at_turn == turn_index and turn_index not in self._injected:
            pending = await self._pending_analysis()
            if pending is not None:
                self._responses.appendleft(pending)
                self._injected.add(turn_index)
        if not self._responses and self._final_response_builder is not None:
            async with self._uow_factory() as uow:
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(self._investigation_id)
                )
            self._responses.appendleft(await self._final_response_builder(observations))
            self._injected.add(-1)
        elif not self._responses and self._include_external_pivot and -1 not in self._injected:
            async with self._uow_factory() as uow:
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(self._investigation_id)
                )
            self._responses.appendleft(
                await vertical_final_findings(observations, material=self._material)
            )
            self._injected.add(-1)
        return await super().submit(**kwargs)


async def vertical_final_findings(
    observations: tuple[EvidenceObservation, ...],
    *,
    material: SyntheticSampleMaterial,
) -> Mapping[str, Any]:
    candidate_refs = tuple(
        item.id for item in observations if item.object_id == material.sample_hash
    )
    counter_refs = tuple(item.id for item in observations if item.source.casefold() == "shodan")
    assert candidate_refs and counter_refs
    return {
        "reasoning_summary": "The article-relative sample and dated network evidence are cited.",
        "goal": "Record the candidate and qualify the article's network claim.",
        "finding_proposals": [
            {
                "kind": FindingKind.NEW_SAMPLE.value,
                "assessment": FindingAssessment.SUPPORTED.value,
                "statement": "The VT pivot returned the synthetic sample candidate.",
                "observation_refs": [str(item) for item in candidate_refs],
                "subject_ref": material.sample_hash,
                "claim_refs": [],
                "novelty": NoveltyClass.NEW_TO_ARTICLE.value,
                "scope": "Compared only with the verified article snapshot.",
                "limitations": [
                    "Its absence from this article does not establish worldwide novelty."
                ],
            },
            {
                "kind": FindingKind.CLAIM_VERIFICATION.value,
                "assessment": FindingAssessment.REFUTED.value,
                "statement": "Shodan observed indexed services on the article IOC.",
                "observation_refs": [str(item) for item in counter_refs],
                "claim_refs": ["L1.1"],
                "novelty": None,
                "scope": "Passive indexed observation for the article IOC.",
                "limitations": ["The observation date does not prove incident-time activity."],
            },
        ],
        "next_action": "finish",
    }


@dataclass(frozen=True, slots=True)
class SeededInvestigation:
    edition: Edition
    subject: Subject
    run: ProductionRun
    synthesis: ProductionArtifact
    publication: ProductionArtifact
    publication_bytes: bytes
    investigation: AnalystInvestigation
    seed_observation: EvidenceObservation | None


async def seed_verified_article(
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    *,
    suffix: str,
    material: SyntheticSampleMaterial | None = None,
    include_seed: bool = True,
    article_claim: str | None = None,
) -> SeededInvestigation:
    """Persist a verified article, its independent run, and optional seed evidence."""
    samples = material or SyntheticSampleMaterial(
        seed_hash=DEFAULT_SEED_HASH,
        sample_bytes=DEFAULT_SAMPLE_BYTES,
        sample_hash=DEFAULT_SAMPLE_HASH,
    )
    if not include_seed:
        article_claim = article_claim or f"No exposed service is confirmed for {samples.ip}."
    else:
        article_claim = article_claim or (
            f"The seed sample is {samples.seed_hash}; the reported IOC is {samples.ip}."
        )
    edition = Edition(
        country=f"Investigation vertical {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Vertical investigation fixture",
        slug=f"investigation-vertical-{suffix}",
        tlp=TLP.CLEAR,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis_payload = {
        "title": "A verified synthesis for a bounded investigation",
        "subject_id": str(subject.id),
        "lead": [{"text": article_claim}],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
        "indicators": ([{"kind": "sha256", "value": samples.seed_hash}] if include_seed else []),
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="investigation-vertical-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        canonical_blob_id=synthesis_blob_id,
    )
    base_document = _base_v5()
    base_document = replace(
        base_document,
        document=replace(base_document.document, subject_id=subject.id),
    )
    publication_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(base_document)
    )
    publication_blob_id = await artifact_store.put_bytes(
        publication_bytes,
        bucket="investigation-vertical-publication-v5",
        mime_type="application/json",
    )
    publication = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(publication_bytes).hexdigest(),
        canonical_blob_id=publication_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(max_cycles=12, max_vt_read_units=24, max_hits_acquired=1),
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [],
        "file_indicators": ([{"value": samples.seed_hash}] if include_seed else []),
        "policy": {
            "tlp": "clear",
            "do_not_submit": False,
            "external_llm_allowed": True,
        },
    }
    input_pack_blob_id, input_pack_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="analyst-input-packs"
    )
    investigation.input_pack_blob_id = input_pack_blob_id
    investigation.input_sha256 = input_pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_pack_blob_id,
        sha256=input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    seed_observation: EvidenceObservation | None = None
    if include_seed:
        seed_bytes = b"Synthetic seed evidence; no real provider request is made." + uuid4().bytes
        seed_blob_id = await artifact_store.put_bytes(
            seed_bytes,
            bucket="investigation-vertical-seed-evidence",
            mime_type="application/octet-stream",
        )
        seed_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=seed_blob_id,
            sha256=hashlib.sha256(seed_bytes).hexdigest(),
            source="synthetic-article-seed",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="sha256",
            object_id=samples.seed_hash,
            limitations=("Synthetic fixture evidence only.",),
        )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.production_artifacts.append(publication)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        if seed_observation is not None:
            await uow.evidence_observations.append_if_absent(seed_observation)
        await uow.commit()
    return SeededInvestigation(
        edition,
        subject,
        run,
        synthesis,
        publication,
        publication_bytes,
        investigation,
        seed_observation,
    )


async def seed_investigation_for_existing_article(
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    *,
    edition: Edition,
    subject: Subject,
    run: ProductionRun,
    publication: ProductionArtifact,
    publication_bytes: bytes,
    material: SyntheticSampleMaterial,
    article_claim: str,
    include_seed: bool = True,
) -> SeededInvestigation:
    """Attach an investigation to a run independently seeded in a multibatch edition."""
    synthesis_payload = {
        "title": f"Verified synthesis for {subject.title}",
        "subject_id": str(subject.id),
        "lead": [{"text": article_claim}],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
        "indicators": ([{"kind": "sha256", "value": material.seed_hash}] if include_seed else []),
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="workflow-investigation-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        canonical_blob_id=synthesis_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(max_cycles=12, max_vt_read_units=32, max_hits_acquired=2),
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [],
        "file_indicators": ([{"value": material.seed_hash}] if include_seed else []),
        "policy": {
            "tlp": "clear",
            "do_not_submit": False,
            "external_llm_allowed": True,
        },
    }
    pack_blob_id, pack_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="analyst-input-packs"
    )
    investigation.input_pack_blob_id = pack_blob_id
    investigation.input_sha256 = pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=pack_blob_id,
        sha256=pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    seed_observation = None
    if include_seed:
        seed_bytes = b"Synthetic article seed observation:" + uuid4().bytes
        seed_blob_id = await artifact_store.put_bytes(
            seed_bytes,
            bucket="workflow-article-seed-evidence",
            mime_type="application/octet-stream",
        )
        seed_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=seed_blob_id,
            sha256=hashlib.sha256(seed_bytes).hexdigest(),
            source="synthetic-article-seed",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="sha256",
            object_id=material.seed_hash,
            limitations=("Synthetic fixture evidence only.",),
        )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(synthesis)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        if seed_observation is not None:
            await uow.evidence_observations.append_if_absent(seed_observation)
        await uow.commit()
    return SeededInvestigation(
        edition,
        subject,
        run,
        synthesis,
        publication,
        publication_bytes,
        investigation,
        seed_observation,
    )


def article_document_v5(
    subject_id: UUID, label: str, *, version: str = "workflow"
) -> PublicationDocumentV5:
    """Build a contentful V5 document for real edition rendering."""
    title = f"Article {label}"
    lead = f"Corps article {label} {version}. " + (
        "This deterministic local fixture describes one independently published article. " * 24
    )
    source_document_id = uuid4()
    evidence = PublicationEvidenceRefV1(
        source_document_id=source_document_id,
        kind=PublicationEvidenceKind.FACT,
        evidence_key=hashlib.sha256(uuid4().bytes).hexdigest(),
    )
    base = PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=subject_id,
        publication_language="fr",
        title=title,
        lead=(PublicationParagraphV1(text=lead, evidence_refs=(evidence,)),),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(
            PublicationSourceV1(
                source_document_id=source_document_id,
                canonical_url=f"https://example.test/{version}/{uuid4().hex}",
                title="Local workflow fixture",
                publisher="AutoWork test harness",
                published_at=None,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
            ),
        ),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )
    return PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
            paragraphs=(
                SemanticParagraphV1(
                    anchor="title",
                    spans=(SemanticTextSpanV1(SemanticRole.TEXT, title),),
                ),
                SemanticParagraphV1(
                    anchor="lead:0001",
                    spans=(SemanticTextSpanV1(SemanticRole.TEXT, lead),),
                ),
            ),
        ),
    )


def decision(
    reasoning: str,
    *,
    tool_call: Mapping[str, Any] | None = None,
    next_action: str = "continue",
    finding_proposals: list[Mapping[str, Any]] | None = None,
    hypothesis_refs: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reasoning_summary": reasoning,
        "goal": "Use the next bounded evidence step for this verified article.",
        "proposed_tool_calls": [dict(tool_call)] if tool_call is not None else [],
        "finding_proposals": [dict(item) for item in finding_proposals or []],
        "next_action": next_action,
    }
    if hypothesis_refs:
        payload["hypothesis_refs"] = list(hypothesis_refs)
    return payload


def investigation_settings(
    *,
    providers: bool,
    local_analysis: bool = False,
    relationships: bool = False,
    rules: bool = False,
) -> Settings:
    if not providers:
        return Settings.model_construct(
            investigation_max_cycles=12,
            rule_workbench_enabled=rules,
            local_analysis_enabled=local_analysis,
        )
    return Settings.model_construct(
        investigation_max_cycles=12,
        investigation_max_vt_read_units=24,
        investigation_max_shodan_credits=4,
        investigation_max_hits_acquired=1,
        virustotal_file_report_enabled=True,
        virustotal_object_relationships_enabled=relationships,
        virustotal_intelligence_search_enabled=True,
        virustotal_file_download_enabled=True,
        shodan_api_key=SecretStr("synthetic-no-network-key"),
        shodan_host_enabled=True,
        local_analysis_enabled=local_analysis,
        rule_workbench_enabled=rules,
    )


def composition_factory(
    *,
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    settings: Settings,
    transport: ScriptedTurnTransport,
    virustotal: HashCheckedVirusTotal | None = None,
    shodan: FakeShodanPort | None = None,
    include_local_analysis: bool = False,
    rule_corpus_bytes: Any | None = None,
    yara_engine: Any | None = None,
    additional_ports: OptionalInvestigationToolPorts | None = None,
) -> InvestigationCompositionFactory:
    ports = OptionalInvestigationToolPorts()
    if virustotal is not None:
        blob_catalog = artifact_store._catalog
        vt_capabilities = VirusTotalCapabilities(
            file_report=True,
            file_relationships=settings.virustotal_object_relationships_enabled,
            intelligence_search=True,
            file_download=True,
        )
        ports = replace(
            ports,
            virustotal=virustotal,
            acquisition=VirusTotalSampleAcquisitionService(
                virustotal,
                vt_capabilities,
                blob_catalog,
                uow_factory,
                LocalIdentityProvider("service:synthetic-acquisition"),
            ),
            virustotal_observations=VirusTotalObservationService(blob_catalog, uow_factory),
            vt_capabilities=vt_capabilities,
        )
    if shodan is not None:
        from cti_app.application.shodan import ShodanBudgetEstimate, ShodanCapabilities

        ports = replace(
            ports,
            shodan=shodan,
            shodan_capabilities=ShodanCapabilities(host=True),
            shodan_budget_estimate=ShodanBudgetEstimate(host=1),
        )
    if include_local_analysis:
        ports = replace(
            ports,
            sample_static_analysis=StaticAnalysisService(
                artifact_store._catalog,
                uow_factory,
                max_sample_bytes=1024 * 1024,
                string_min_length=4,
            ),
        )
    if rule_corpus_bytes is not None or yara_engine is not None:
        ports = replace(
            ports,
            rule_corpus_bytes=rule_corpus_bytes,
            yara_engine=yara_engine,
        )
    if additional_ports is not None:
        overrides = {
            item.name: getattr(additional_ports, item.name)
            for item in fields(OptionalInvestigationToolPorts)
            if getattr(additional_ports, item.name) is not None
        }
        ports = replace(ports, **overrides)
    return InvestigationCompositionFactory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_conversations=SimpleNamespace(),  # type: ignore[arg-type]
        settings=settings,
        optional_tool_ports=ports,
        turn_transport=transport,
    )


async def persist_sample_association_fixture(
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    *,
    investigation_id: UUID,
    subject_id: UUID,
    sample_bytes: bytes | None = None,
    sample_sha256: str | None = None,
    origin: SampleOrigin = SampleOrigin.MANUAL,
    logical_name: str = "synthetic-fixture.bin",
) -> SampleAssociation:
    """Persist an exact sample association and optional quarantined fixture bytes."""
    if sample_bytes is not None:
        actual_sha256 = hashlib.sha256(sample_bytes).hexdigest()
        if sample_sha256 is not None and sample_sha256 != actual_sha256:
            raise ValueError("Fixture bytes do not match the declared sample SHA-256")
        sample_sha256 = actual_sha256
    if sample_sha256 is None:
        raise ValueError("A sample SHA-256 or fixture byte payload is required")

    sample: Sample | None = None
    if sample_bytes is not None:
        blob_id = await artifact_store.put_bytes(
            sample_bytes,
            bucket="workflow-quarantined-samples",
            mime_type="application/octet-stream",
        )
        sample = Sample(
            subject_id=subject_id,
            blob_id=blob_id,
            original_name=logical_name,
            origin="workflow emulator synthetic fixture",
            acquired_at=datetime.now(UTC),
            license_restriction=None,
            tlp=TLP.CLEAR,
            do_not_submit=True,
            external_llm_allowed=False,
            origin_kind=EntitySampleOrigin.MANUAL,
            state=SampleState.QUARANTINED,
            source_service="workflow-emulator",
            source_object_id=sample_sha256,
            expected_hash=sample_sha256,
        )

    association = SampleAssociation(
        investigation_id=investigation_id,
        sample_sha256=sample_sha256,
        origin=origin,
    )
    async with uow_factory() as uow:
        if sample is not None:
            await uow.samples.add(sample)
            await uow.sample_acquisition_attempts.append(
                SampleAcquisitionAttempt(
                    investigation_id=investigation_id,
                    requested_hash=sample_sha256,
                    hash_family="sha256",
                    reason=SampleAcquisitionReason.SEED,
                    outcome=SampleAcquisitionOutcome.SUCCESS,
                    sample_id=sample.id,
                )
            )
        persisted: SampleAssociation = await uow.sample_associations.append_version(
            association, expected_version=0
        )
        await uow.commit()
    return persisted


class DeferredDispatcher:
    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.job_ids.append(job_id)


def build_investigation_app(
    *,
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
    composition_factory: InvestigationCompositionFactory,
    include_detection: bool = False,
) -> tuple[FastAPI, Any, DeferredDispatcher]:
    runner = make_investigation_runner(uow_factory, composition_factory)
    registry = create_job_registry(investigation_runner=runner)
    dispatcher = DeferredDispatcher()
    app = FastAPI()
    app.include_router(investigation_router)
    app.include_router(opinion_router)
    if include_detection:
        from cti_app.api.detection import router as detection_router

        app.include_router(detection_router)
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = artifact_store
    app.state.investigation_composition_factory = composition_factory
    app.state.job_service = SimpleNamespace(submit=lambda **_kwargs: None)
    app.state.job_registry = registry
    app.state.job_dispatcher = dispatcher
    app.state.identity_provider = LocalIdentityProvider("editor:workflow-emulator")
    _install_job_service(app, uow_factory, registry)
    return app, registry, dispatcher


def _install_job_service(app: FastAPI, uow_factory: Any, registry: Any) -> None:
    from cti_app.application.jobs import JobService

    app.state.job_service = JobService(uow_factory, registry)


async def run_http_job(
    app: FastAPI,
    uow_factory: Any,
    registry: Any,
    dispatcher: DeferredDispatcher,
    job_id: UUID,
) -> None:
    await SynchronousJobDispatcher(JobExecutor(uow_factory, registry)).dispatch(job_id)


async def start_investigation_http(
    client: httpx.AsyncClient, seeded: SeededInvestigation
) -> dict[str, Any]:
    response = await client.post(
        f"/api/subjects/{seeded.subject.id}/investigations",
        json={
            "production_run_id": str(seeded.run.id),
            "synthesis_artifact_id": str(seeded.synthesis.id),
            "synthesis_sha256": seeded.synthesis.input_hash,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["investigation"]["envelope_approved"] is False
    return cast(dict[str, Any], response.json())


async def approve_envelope_http(
    client: httpx.AsyncClient,
    investigation_id: UUID,
    *,
    providers: tuple[str, ...] = ("vt", "shodan"),
    budget_limits: Mapping[str, int] | None = None,
    allow_acquisition: bool = True,
    max_acquire_volume: int = 1,
    do_not_submit: bool = False,
) -> httpx.Response:
    limits = dict(
        budget_limits or {"bridge_turns": 12, "vt_units": 24, "shodan_credits": 4, "downloads": 1}
    )
    return await client.post(
        f"/api/investigations/{investigation_id}/envelope/approve",
        json={
            "allowed_providers": list(providers),
            "budget_limits": limits,
            "acquire_hits_allowed": allow_acquisition,
            "max_acquire_volume": max_acquire_volume if allow_acquisition else 0,
            "disclosure": {
                "tlp": "clear",
                "do_not_submit": do_not_submit,
                "external_llm_allowed": True,
            },
            "reason": "Approve the bounded synthetic workflow investigation.",
            "correlation_id": f"workflow-g2:{uuid4()}",
        },
        headers={"Idempotency-Key": f"workflow-g2-{uuid4()}"},
    )


async def compose_opinion_http(
    client: httpx.AsyncClient,
    investigation_id: UUID,
    *,
    selection: Mapping[str, Any] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/api/investigations/{investigation_id}/opinion/compose",
        json={"selection": dict(selection or {})},
        headers={"Idempotency-Key": f"workflow-opinion-compose-{uuid4()}"},
    )


async def qualify_sample_http(
    client: httpx.AsyncClient,
    investigation_id: UUID,
    association_id: UUID,
    *,
    expected_version: int,
    qualification: SampleQualification = SampleQualification.CONFIRMED,
    evidence_refs: tuple[UUID, ...],
) -> httpx.Response:
    return await client.post(
        f"/api/investigations/{investigation_id}/samples/{association_id}/qualify",
        json={
            "expected_version": expected_version,
            "qualification": qualification.value,
            "reason": "Human review of the cited synthetic sample evidence.",
            "evidence_refs": [str(item) for item in evidence_refs],
            "correlation_id": f"workflow-g3:{uuid4()}",
        },
        headers={"Idempotency-Key": f"workflow-g3-{uuid4()}"},
    )


async def approve_opinion_http(
    client: httpx.AsyncClient, opinion_id: UUID, *, version: int, sha256: str
) -> httpx.Response:
    return await client.post(
        f"/api/opinions/{opinion_id}/approve",
        json={
            "version": version,
            "expected_sha256": sha256,
            "reason": "Approve the reviewed synthetic opinion version.",
        },
        headers={"Idempotency-Key": f"workflow-opinion-approve-{opinion_id}-{version}"},
    )


async def revise_opinion_http(
    client: httpx.AsyncClient,
    opinion_id: UUID,
    *,
    expected_version: int,
    blocks: tuple[Mapping[str, Any], ...],
    unresolved_points: tuple[str, ...] = (),
) -> httpx.Response:
    return await client.post(
        f"/api/opinions/{opinion_id}/revise",
        json={
            "expected_version": expected_version,
            "blocks": [dict(item) for item in blocks],
            "unresolved_points": list(unresolved_points),
        },
        headers={"Idempotency-Key": f"workflow-opinion-revise-{opinion_id}-{uuid4()}"},
    )


async def publish_opinion_http(
    client: httpx.AsyncClient,
    opinion_id: UUID,
    *,
    version: int,
    base_publication_sha256: str,
) -> httpx.Response:
    return await client.post(
        f"/api/opinions/{opinion_id}/publish",
        json={
            "version": version,
            "expected_base_publication_sha256": base_publication_sha256,
        },
        headers={"Idempotency-Key": f"workflow-opinion-publish-{opinion_id}-{version}"},
    )


async def request_analysis_http(
    client: httpx.AsyncClient, investigation_id: UUID, *, text: str
) -> httpx.Response:
    return await client.post(
        f"/api/investigations/{investigation_id}/requests",
        json={"text": text},
        headers={"Idempotency-Key": f"workflow-request-{investigation_id}-{uuid4()}"},
    )


async def read_composition(uow_factory: Any, edition_id: UUID) -> Any:
    async with uow_factory() as uow:
        return await uow.edition_compositions.get_for_edition(edition_id)


async def read_manifest(uow_factory: Any, edition_id: UUID) -> Any:
    async with uow_factory() as uow:
        return await uow.publication_manifests.get_latest_manifest_for_edition(edition_id)


def _decision(
    reasoning: str,
    *,
    tool_call: Mapping[str, Any] | None = None,
    next_action: str = "continue",
    finding_proposals: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    return decision(
        reasoning,
        tool_call=tool_call,
        next_action=next_action,
        finding_proposals=finding_proposals,
    )


__all__ = [
    "DEFAULT_IOC",
    "DEFAULT_SAMPLE_BYTES",
    "DEFAULT_SAMPLE_HASH",
    "DEFAULT_SEED_HASH",
    "DeferredDispatcher",
    "FakeShodanPort",
    "HashCheckedVirusTotal",
    "ScriptedInvestigationTransport",
    "SeededInvestigation",
    "SyntheticSampleMaterial",
    "approve_envelope_http",
    "approve_opinion_http",
    "article_document_v5",
    "build_investigation_app",
    "compose_opinion_http",
    "composition_factory",
    "decision",
    "investigation_settings",
    "publish_opinion_http",
    "qualify_sample_http",
    "read_composition",
    "read_manifest",
    "request_analysis_http",
    "revise_opinion_http",
    "run_http_job",
    "seed_investigation_for_existing_article",
    "seed_verified_article",
    "start_investigation_http",
    "vertical_final_findings",
]
