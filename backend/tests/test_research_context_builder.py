import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.investigation_article_snapshot import (
    ProductionArticleSnapshotAdapter,
    _extract_narrative_hashes,
)
from cti_app.application.investigation_contracts import ArticleClaimV1, ArticleContextV1
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemorySampleAssociationRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import (
    ArticleSnapshot,
    RepositoryInvestigationGraphReadPort,
    ResearchContextBuilder,
)
from cti_app.domain.investigation import (
    AgendaItem,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    SampleAssociation,
    SampleOrigin,
    TLPLevel,
    thaw_json,
    tool_operation_key,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    InvestigationWorkingState,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)


class _SnapshotPort:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def resolve(self, synthesis_artifact_id):
        return self.snapshot

    async def get_claims(self, investigation_id, claim_ids=()):
        if not claim_ids:
            return self.snapshot.article.claims
        wanted = set(claim_ids)
        return tuple(item for item in self.snapshot.article.claims if item.id in wanted)


def test_article_snapshot_extracts_bounded_hash_families_from_claims_and_sources() -> None:
    md5 = "A1" * 16
    sha1 = "B2" * 20
    sha256 = "C3" * 32

    extracted = _extract_narrative_hashes(
        {
            "narrative": ["Sample MD5: " + md5],
            "claims": [{"statement": "SHA-1=" + sha1}],
            "sources": [{"quote": "SHA-256=" + sha256}],
            "other": "not a hash: " + "D4" * 18,
        }
    )

    assert extracted == {md5.lower(), sha1.lower(), sha256.lower()}
    assert len(_extract_narrative_hashes({"value": md5 + " " + sha1}, limit=1)) == 1


class _ReadPort:
    async def get_evidence(self, observation_id):
        return {
            "summary": "safe evidence",
            "api_key": "secret-value",
            "signed": "https://example.invalid/data?X-Amz-Signature=very-secret",
        }

    async def get_claims(self, investigation_id, claim_ids):
        return ({"id": "C1", "statement": "Claim text", "source_refs": []},)

    async def get_feature_pack(self, sample_sha256):
        return {"sample_sha256": sample_sha256, "summary": "persisted features"}

    async def graph_neighbors(self, object_type, object_id):
        return ({"target": "10.0.0.1", "relationship": "contacted"},)


def _state(
    investigation_id,
    *,
    turn_index=0,
    evidence_ids=(),
    finding_ids=(),
    hypotheses=(),
    agenda=None,
):
    return InvestigationWorkingState(
        investigation_id=investigation_id,
        agenda=agenda
        or InvestigationAgenda(
            (
                AgendaItem(question="Check the infection chain", potential=1.0, rank=1),
                AgendaItem(question="Verify the network claim", potential=0.8, rank=2),
            )
        ),
        turn_index=turn_index,
        hypotheses=tuple(hypotheses),
        last_seen_evidence_ids=tuple(evidence_ids),
        last_seen_finding_ids=tuple(finding_ids),
    )


@pytest.mark.asyncio
async def test_context_builds_full_article_and_bounded_delta_with_contradictions_first() -> None:
    investigation_id = uuid4()
    source_id = uuid4()
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.SHODAN_OBSERVATION,
        blob_id=uuid4(),
        sha256="b" * 64,
        source="shodan.host",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="ip",
        object_id="203.0.113.7",
    )
    observations = InMemoryEvidenceObservationRepository()
    await observations.append_if_absent(observation)
    findings = InMemoryFindingRepository()
    refuted = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.REFUTED,
        statement="The observed service predates the reported campaign.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    supported = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.NETWORK_BEHAVIOUR,
        assessment=FindingAssessment.SUPPORTED,
        statement="The sandbox observed a connection to the address.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    await findings.append_version(refuted, expected_previous_id=None)
    await findings.append_version(supported, expected_previous_id=None)
    snapshot = ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="Campaign narrative https://example.invalid/?X-Amz-Signature=secret",
            claims=(
                ArticleClaimV1(
                    id="C1",
                    statement="The article attributes 203.0.113.7 to the campaign.",
                    source_refs=(f"{source_id}:fact:{'c' * 64}",),
                ),
            ),
            documented_samples=("d" * 64,),
            known_chain=({"stage": "loader", "api_key": "secret-value"},),
            open_questions=("Is the address attribution supported?",),
        ),
    )
    registry = InMemoryToolRegistry()
    builder = ResearchContextBuilder(
        article_snapshots=_SnapshotPort(snapshot),
        observations=observations,
        findings=findings,
        registry=registry,
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.GREEN),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    tool_context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="context-1",
    )

    full = await builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=snapshot.synthesis_artifact_id,
        state=_state(investigation_id),
        tool_context=tool_context,
        budget_remaining={},
    )
    delta = await builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=snapshot.synthesis_artifact_id,
        state=_state(investigation_id, turn_index=1),
        tool_context=tool_context,
        budget_remaining={},
    )

    assert full.view == "full"
    assert full.article is not None
    assert full.article.claims[0].source_refs == (f"{source_id}:fact:{'c' * 64}",)
    assert "very-secret" not in full.canonical_bytes().decode()
    assert "secret-value" not in full.canonical_bytes().decode()
    assert "UNTRUSTED" in full.article.short_summary
    assert delta.view == "delta"
    assert delta.article is None
    assert delta.delta is not None
    assert delta.delta.changes[0].kind == "finding:refuted"
    assert len(delta.canonical_bytes()) < 262_144


@pytest.mark.asyncio
async def test_delta_includes_bounded_unresolved_hypotheses_with_local_evidence_only() -> None:
    investigation_id = uuid4()
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.SHODAN_OBSERVATION,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="shodan.host",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="ip",
        object_id="203.0.113.11",
    )
    observations = InMemoryEvidenceObservationRepository()
    await observations.append_if_absent(observation)
    findings = InMemoryFindingRepository()
    linked_finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.REFUTED,
        statement="The observed evidence contradicts the attribution.",
        observation_refs=(observation.id,),
    )
    await findings.append_version(linked_finding, expected_previous_id=None)
    snapshot = ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="f" * 64,
        article=ArticleContextV1(
            short_summary="Article summary.",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
    )
    foreign_observation_id = uuid4()
    invalid_observation_id = uuid4()
    invalid_finding_id = uuid4()
    hypotheses = (
        Hypothesis(
            id="z-open",
            statement="An open question.",
            evidence_refs=(observation.id, foreign_observation_id, invalid_observation_id),
            finding_refs=(linked_finding.id, invalid_finding_id),
        ),
        Hypothesis(
            id="a-refuted",
            statement="A previously refuted question remains unresolved.",
            status=HypothesisStatus.REFUTED,
            evidence_refs=(observation.id,),
            finding_refs=(linked_finding.id,),
        ),
        Hypothesis(
            id="m-supported",
            statement="A supported hypothesis is settled.",
            status=HypothesisStatus.SUPPORTED,
            evidence_refs=(observation.id,),
            finding_refs=(linked_finding.id,),
        ),
        *(
            Hypothesis(id=f"h-{index:02}", statement="Another open question.")
            for index in range(14)
        ),
    )
    state = _state(investigation_id, turn_index=1, hypotheses=hypotheses)
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    builder = ResearchContextBuilder(
        article_snapshots=_SnapshotPort(snapshot),
        observations=observations,
        findings=findings,
    )

    context = await builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=snapshot.synthesis_artifact_id,
        state=state,
        tool_context=ToolExecutionContext(
            investigation_id=investigation_id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="unresolved-hypotheses",
        ),
        budget_remaining={},
    )

    assert context.delta is not None
    unresolved = context.delta.stable_hypotheses
    assert len(unresolved) == 16
    assert [item["id"] for item in unresolved] == sorted(item["id"] for item in unresolved)
    by_id = {item["id"]: item for item in unresolved}
    assert "m-supported" not in by_id
    assert by_id["a-refuted"]["evidence_refs"] == [str(observation.id)]
    assert by_id["a-refuted"]["finding_refs"] == [str(linked_finding.id)]
    assert by_id["z-open"]["evidence_refs"] == [str(observation.id)]
    assert by_id["z-open"]["finding_refs"] == [str(linked_finding.id)]


@pytest.mark.asyncio
async def test_large_full_context_is_deterministically_bounded_and_keeps_iocs_first() -> None:
    investigation_id = uuid4()
    citation = "citation-" + "x" * 240
    claims = tuple(
        ArticleClaimV1(
            id=f"C{index}",
            statement=("Claim narrative " * 200) + " api_key=claim-secret",
            source_refs=(citation,) * 64,
        )
        for index in range(128)
    )
    rows = (
        *({"kind": "infection_chain", "statement": "chain " * 400} for _ in range(255)),
        {"kind": "documented_ioc", "value": "198.51.100.42"},
    )
    snapshot = ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="Narrative " * 1_000,
            claims=claims,
            documented_samples=("b" * 64,) * 1_000,
            known_chain=rows,
            open_questions=("Question " * 200,) * 128,
        ),
    )
    findings = InMemoryFindingRepository()
    for index in range(40):
        await findings.append_version(
            InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.CLAIM_VERIFICATION,
                assessment=(
                    FindingAssessment.REFUTED if index == 39 else FindingAssessment.INCONCLUSIVE
                ),
                statement=f"Finding {index} " + "detail " * 500,
                observation_refs=(),
                claim_refs=(),
            ),
            expected_previous_id=None,
        )
    state = _state(
        investigation_id,
        hypotheses=tuple(
            Hypothesis(
                id=f"h{index}",
                statement=("Hypothesis " * 20)
                + (" api_key=hypothesis-secret" if index == 0 else ""),
            )
            for index in range(128)
        ),
        agenda=InvestigationAgenda(
            (
                AgendaItem(question="Question api_key=agenda-secret", potential=1.0, rank=1),
                AgendaItem(question="Check the evidence", potential=0.8, rank=2),
            )
        ),
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    builder = ResearchContextBuilder(
        article_snapshots=_SnapshotPort(snapshot),
        observations=InMemoryEvidenceObservationRepository(),
        findings=findings,
    )

    context = await builder.build(
        investigation_id=investigation_id,
        synthesis_artifact_id=snapshot.synthesis_artifact_id,
        state=state,
        tool_context=ToolExecutionContext(
            investigation_id=investigation_id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="large-context",
        ),
        budget_remaining={},
    )

    assert len(context.canonical_bytes()) < 262_144
    canonical_context = context.canonical_bytes().decode()
    assert "claim-secret" not in canonical_context
    assert "hypothesis-secret" not in canonical_context
    assert "agenda-secret" not in canonical_context
    assert context.article is not None
    assert len(context.article.claims) == 16
    assert len(context.article.documented_samples) == 128
    assert len(context.article.open_questions) == 16
    assert len(context.investigation.hypotheses) == 16
    assert "198.51.100.42" in context.article.known_chain[0]["summary"]
    assert context.investigation.findings[0].assessment is FindingAssessment.REFUTED


@pytest.mark.asyncio
async def test_local_context_tools_read_persisted_data_and_mark_it_untrusted() -> None:
    investigation_id = uuid4()
    builder = ResearchContextBuilder(
        article_snapshots=_SnapshotPort(
            ArticleSnapshot(
                subject_id=uuid4(),
                production_run_id=uuid4(),
                synthesis_artifact_id=uuid4(),
                synthesis_sha256="a" * 64,
                article=ArticleContextV1(
                    short_summary="Article",
                    claims=(),
                    documented_samples=(),
                    known_chain=(),
                    open_questions=(),
                ),
            )
        ),
        observations=InMemoryEvidenceObservationRepository(),
        findings=InMemoryFindingRepository(),
    )
    registry = InMemoryToolRegistry()
    builder.register_local_tools(registry, _ReadPort())
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="evidence-read",
        available_capabilities=frozenset({"investigation.local_read"}),
    )
    evidence_id = uuid4()
    operation_key = tool_operation_key(
        investigation_id=investigation_id,
        tool_name="evidence.get",
        schema_version="1",
        normalized_parameters={"observation_id": str(evidence_id)},
        purpose="inspect persisted evidence",
    )
    dispatcher = RegistryToolDispatcher(registry)

    result = await dispatcher.dispatch(
        context,
        tool_name="evidence.get",
        schema_version="1",
        arguments={"observation_id": str(evidence_id)},
        purpose="inspect persisted evidence",
        operation_key=operation_key,
    )

    assert result.status.value == "succeeded"
    text = json.dumps(thaw_json(result.output))
    assert "UNTRUSTED" in text
    assert "secret-value" not in text
    assert "very-secret" not in text
    assert {definition.name for definition in registry.list_available(context)} == {
        "evidence.get",
        "article.get_claims",
        "sample.get_feature_pack",
        "graph.neighbors",
    }
    for tool_name, arguments in (
        ("article.get_claims", {"claim_ids": ["C1"]}),
        ("sample.get_feature_pack", {"sample_sha256": "a" * 64}),
        ("graph.neighbors", {"object_type": "ip", "object_id": "203.0.113.7"}),
    ):
        operation = tool_operation_key(
            investigation_id=investigation_id,
            tool_name=tool_name,
            schema_version="1",
            normalized_parameters=arguments,
            purpose="read persisted context",
        )
        local_result = await dispatcher.dispatch(
            context,
            tool_name=tool_name,
            schema_version="1",
            arguments=arguments,
            purpose="read persisted context",
            operation_key=operation,
        )
        assert local_result.status.value == "succeeded"


@pytest.mark.asyncio
async def test_repository_graph_reads_edges_from_findings_and_sample_associations() -> None:
    investigation_id = uuid4()
    findings = InMemoryFindingRepository()
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.NETWORK_BEHAVIOUR,
        assessment=FindingAssessment.INCONCLUSIVE,
        statement="The article associates the service with claim C1.",
        observation_refs=(uuid4(),),
        claim_refs=("C1",),
    )
    await findings.append_version(finding, expected_previous_id=None)
    associations = InMemorySampleAssociationRepository()
    association = SampleAssociation(
        investigation_id=investigation_id,
        sample_sha256="a" * 64,
        origin=SampleOrigin.SEED,
        related_sample_sha256=("b" * 64,),
    )
    await associations.append_version(association, expected_version=0)
    graph = RepositoryInvestigationGraphReadPort(
        investigation_id=investigation_id,
        findings=findings,
        associations=associations,
    )

    sample_edges = await graph.neighbors("sample", "a" * 64)
    finding_edges = await graph.neighbors("finding", str(finding.id))
    claim_edges = await graph.neighbors("claim", "C1")

    assert sample_edges[0]["target_id"] == "b" * 64
    assert {item["target_type"] for item in finding_edges} == {"evidence", "claim"}
    assert claim_edges[0]["source_id"] == str(finding.id)


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _UowFactory:
    def __init__(self, uow):
        self.uow = uow

    def __call__(self):
        return _AsyncContext(self.uow)


class _ArtifactStore:
    canonical_json_bytes = staticmethod(ProductionArtifactStore.canonical_json_bytes)

    def __init__(self, records):
        self.records = records

    async def read_json(self, blob_id):
        return self.records[blob_id]


@pytest.mark.asyncio
async def test_verified_article_adapter_resolves_synthesis_and_input_pack_by_identity() -> None:
    run_id, subject_id, investigation_id = uuid4(), uuid4(), uuid4()
    synthesis_id, extraction_id, publication_id = uuid4(), uuid4(), uuid4()
    synthesis_blob, extraction_blob, publication_blob, pack_blob = (uuid4() for _ in range(4))
    synthesis = ProductionArtifact(
        id=synthesis_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="1" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob,
    )
    extraction = ProductionArtifact(
        id=extraction_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=2,
        input_hash="2" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob,
    )
    publication = ProductionArtifact(
        id=publication_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=3,
        input_hash="3" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=publication_blob,
    )
    synthesis_payload = {
        "schema_version": 1,
        "subject_id": str(subject_id),
        "title": "Threat overview",
        "lead": [
            {
                "text": "The actor used a loader.",
                "evidence_refs": [
                    {"source_document_id": str(uuid4()), "kind": "fact", "evidence_key": "c" * 64}
                ],
            }
        ],
        "sections": [
            {
                "kind": "infection_chain",
                "heading": "Infection chain",
                "paragraphs": [{"text": "The loader retrieves a payload.", "evidence_refs": []}],
            }
        ],
        "timeline": [],
        "uncertainties": [{"text": "The initial vector is unknown."}],
    }
    extraction_payload = {
        "sources": [
            {
                "indicators": [
                    {
                        "value": "198.51.100.9",
                        "artifact_type": "ip",
                        "indicator_status": "confirmed_ioc",
                        "context": "contacted by the loader",
                        "evidence_quote": "198.51.100.9",
                        "source_document_ids": [str(uuid4())],
                    }
                ]
            }
        ]
    }
    pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation_id)},
        "synthesis_artifact": {"id": str(synthesis_id), "canonical_blob_id": str(synthesis_blob)},
        "retained_extraction_artifacts": [
            {
                "artifact_id": str(extraction_id),
                "canonical_blob_id": str(extraction_blob),
                "input_hash": extraction.input_hash,
                "version": extraction.version,
            }
        ],
        "file_indicators": [
            {
                "value": "d" * 64,
                "provenance": {"source_ids": ["source-1"]},
            }
        ],
        "policy": {"tlp": "GREEN", "do_not_submit": False, "external_llm_allowed": True},
    }
    artifact_store = _ArtifactStore(
        {
            synthesis_blob: synthesis_payload,
            extraction_blob: extraction_payload,
            publication_blob: {"schema_version": "publication.v5"},
            pack_blob: pack_payload,
        }
    )
    pack_sha256 = hashlib.sha256(artifact_store.canonical_json_bytes(pack_payload)).hexdigest()
    uow = SimpleNamespace(
        production_artifacts=SimpleNamespace(
            get=lambda artifact_id: _async_value(
                {synthesis_id: synthesis, extraction_id: extraction}.get(artifact_id)
            ),
            list_for_run=lambda run: _async_value((synthesis, extraction, publication)),
        ),
        analyst_investigations=SimpleNamespace(
            get=lambda investigation: _async_value(
                SimpleNamespace(
                    id=investigation_id,
                    synthesis_artifact_id=synthesis_id,
                    input_pack_blob_id=pack_blob,
                    input_sha256=pack_sha256,
                )
            ),
            get_for_run=lambda run: _async_value(
                SimpleNamespace(
                    id=investigation_id,
                    synthesis_artifact_id=synthesis_id,
                    input_pack_blob_id=pack_blob,
                    input_sha256=pack_sha256,
                )
            ),
        ),
        analyst_input_packs=SimpleNamespace(
            get_for_investigation=lambda investigation: _async_value(
                SimpleNamespace(
                    blob_id=pack_blob,
                    sha256=pack_sha256,
                    schema_version="analyst-input-pack-v1",
                )
            )
        ),
    )
    adapter = ProductionArticleSnapshotAdapter(_UowFactory(uow), artifact_store)

    snapshot = await adapter.resolve(synthesis_id)

    assert snapshot.synthesis_artifact_id == synthesis_id
    assert snapshot.article.claims[0].statement == "The actor used a loader."
    assert snapshot.article.claims[0].source_refs[0].endswith(f":{'c' * 64}")
    assert "198.51.100.9" in json.dumps(snapshot.article.known_chain)
    assert "d" * 64 in snapshot.article.documented_samples
    assert snapshot.base_publication_artifact_id == publication_id
    assert snapshot.external_llm_allowed is True
    assert (await adapter.get_claims(investigation_id, ("L1.1",)))[0].id == "L1.1"


async def _async_value(value):
    return value
