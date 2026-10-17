"""PostgreSQL proofs for L10 agent pivots and evidence-bounded opinions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.detection_review_service import DetectionReviewService
from cti_app.application.investigation_composition import OptionalInvestigationToolPorts
from cti_app.application.sample_similarity import SampleSimilarityService
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.behavior_evidence import BehaviorEvidenceSource
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.investigation import (
    AgentNextAction,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import Hypothesis, HypothesisStatus
from cti_app.domain.production import ProductionRun, ProductionRunStatus, ProductionStage
from cti_app.domain.retrohunt import (
    RetrohuntHit,
    RetrohuntProviderJob,
    RetrohuntProviderJobState,
    RetrohuntProviderResults,
    RetrohuntSubmitRequest,
)
from cti_app.domain.rule_workbench import (
    RuleCorpusInventoryItem,
    RuleSampleProvenance,
    RuleTestCorpusRole,
)
from cti_app.domain.sample_similarity import (
    CorpusMember,
    CorpusSnapshot,
    SimilarityPolicy,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    approve_opinion_http,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    decision,
    investigation_settings,
    persist_sample_association_fixture,
    qualify_sample_http,
    revise_opinion_http,
    run_http_job,
    seed_investigation_for_existing_article,
    seed_verified_article,
    start_investigation_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


async def _string_observation(
    driver: WorkflowDriver,
    observations: Sequence[EvidenceObservation],
    *,
    object_id: str,
    marker: str,
) -> EvidenceObservation:
    for item in observations:
        if item.kind is not EvidenceKind.BINARY_STRING or item.object_id != object_id:
            continue
        rendered = json.dumps(
            await driver.artifact_store.read_json(item.blob_id), ensure_ascii=True
        )
        if marker in rendered:
            return item
    raise AssertionError(f"no BINARY_STRING observation for {object_id} carries {marker!r}")


def _call(name: str, arguments: dict[str, Any], *, purpose: str | None = None) -> dict[str, Any]:
    return {
        "tool_name": name,
        "schema_version": "1",
        "arguments": arguments,
        # The purpose is part of the operation key: a retest after the corpus changed
        # must state its own purpose, otherwise it is an idempotent replay.
        "purpose": purpose or f"Run the bounded synthetic {name} operation.",
    }


class _StrictFeaturePacks:
    def __init__(self, packs: Mapping[str, Mapping[str, object]]) -> None:
        self._packs = dict(packs)

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, object] | None:
        if sample_sha256 not in self._packs:
            raise AssertionError(f"similarity requested an unseeded feature pack: {sample_sha256}")
        return self._packs[sample_sha256]


class _DatabaseCorpusSnapshots:
    def __init__(self, uow_factory: Any, *, baseline_id: UUID) -> None:
        self._uow_factory = uow_factory
        self._baseline_id = baseline_id

    async def get_snapshot(self, investigation_id: UUID) -> CorpusSnapshot:
        async with self._uow_factory() as uow:
            associations = tuple(
                await uow.sample_associations.list_for_investigation(investigation_id)
            )
        return CorpusSnapshot(
            investigation_id=investigation_id,
            members=tuple(
                CorpusMember(
                    sample_sha256=item.sample_sha256,
                    qualification=item.qualification.value,
                    origin=item.origin.value,
                    association_version=item.version,
                )
                for item in associations
            ),
            goodware_baseline_id=self._baseline_id,
            goodware_baseline_sha256="a" * 64,
            goodware_index_sha256="b" * 64,
        )


class _PreparedGoodware:
    baseline_fingerprint_sha256 = "a" * 64
    index_descriptor = BlobDescriptor(
        sha256="b" * 64,
        size=1,
        mime_type="application/vnd.sqlite3",
        logical_bucket="goodware-indexes",
    )

    async def lookup_batch(
        self, features: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], int]:
        return {feature: 0 for feature in features}


class _StrictGoodware:
    def __init__(self, baseline_id: UUID) -> None:
        self._baseline_id = baseline_id

    async def prepare(self, baseline_ref: UUID | str) -> _PreparedGoodware:
        assert baseline_ref == self._baseline_id
        return _PreparedGoodware()


class _ControlledRuleCorpus:
    """Exact local bytes plus explicit goodware and independent holdout controls."""

    def __init__(
        self,
        samples: Mapping[str, bytes],
        *,
        goodware: tuple[str, ...],
        holdout: tuple[str, ...],
    ) -> None:
        self._samples = dict(samples)
        self._goodware = goodware
        self._holdout = holdout

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        payload = self._samples.get(sample_sha256)
        if payload is None:
            raise AssertionError(f"YARA requested an unseeded corpus item: {sample_sha256}")
        assert hashlib.sha256(payload).hexdigest() == sample_sha256
        return payload

    async def manual_rule_corpus_imports(
        self, _investigation_id: UUID
    ) -> tuple[RuleCorpusInventoryItem, ...]:
        return tuple(
            RuleCorpusInventoryItem(
                sample_sha256=sample_sha256,
                role=RuleTestCorpusRole.GOODWARE,
                provenance=RuleSampleProvenance.SYNTHETIC,
                size_bytes=len(self._samples[sample_sha256]),
                source_description="Synthetic benign-like W14 fixture for local YARA testing.",
            )
            for sample_sha256 in self._goodware
        )

    async def holdout_sample_sha256s(self, _investigation_id: UUID) -> tuple[str, ...]:
        return self._holdout


class _StrictRetrohuntProxy:
    """In-memory provider with a call ledger; no VT HTTP client is present."""

    available = True

    def __init__(self) -> None:
        self.requests: list[RetrohuntSubmitRequest] = []

    async def submit_retrohunt(self, request: RetrohuntSubmitRequest) -> RetrohuntProviderJob:
        self.requests.append(request)
        return RetrohuntProviderJob(
            job_id=f"w14-retro-{request.reservation_id}",
            state=RetrohuntProviderJobState.RUNNING,
        )

    async def retrohunt_status(self, job_id: str) -> RetrohuntProviderJob:
        return RetrohuntProviderJob(job_id=job_id, state=RetrohuntProviderJobState.COMPLETED)

    async def retrohunt_results(self, job_id: str) -> RetrohuntProviderResults:
        reservation_id = job_id.removeprefix("w14-retro-")
        hit = hashlib.sha256(f"retrohunt-hit:{reservation_id}".encode()).hexdigest()
        return RetrohuntProviderResults(
            job_id=job_id,
            hits=(RetrohuntHit(sha256=hit),),
        )


def _feature_pack(
    sample_sha256: str,
    *,
    strings: tuple[tuple[str, int], ...],
) -> dict[str, object]:
    return {
        "sample_sha256": sample_sha256,
        "extractor_version": "synthetic-static-v1",
        "parameters_sha256": "c" * 64,
        "features": {
            "sha256": sample_sha256,
            "format": "PE",
            "strings": [
                {"value": value, "encoding": "ascii", "offset": offset} for value, offset in strings
            ],
        },
    }


def _family_similarity(
    *,
    uow_factory: Any,
    artifact_store: Any,
    investigation_id: UUID,
    feature_packs: Mapping[str, Mapping[str, object]],
    baseline_id: UUID,
) -> SampleSimilarityService:
    del investigation_id

    class _ArtifactAdapter:
        async def ingest(self, source, *, logical_bucket: str, mime_type: str):
            return await artifact_store._catalog.ingest(
                source,
                logical_bucket=logical_bucket,
                mime_type=mime_type,
            )

    return SampleSimilarityService(
        feature_packs=_StrictFeaturePacks(feature_packs),  # type: ignore[arg-type]
        corpus_snapshots=_DatabaseCorpusSnapshots(uow_factory, baseline_id=baseline_id),
        artifacts=_ArtifactAdapter(),  # type: ignore[arg-type]
        goodware=_StrictGoodware(baseline_id),  # type: ignore[arg-type]
        policy=SimilarityPolicy(),
    )


async def _start_and_approve(
    driver: WorkflowDriver,
    client: httpx.AsyncClient,
    *,
    seeded,
    providers: tuple[str, ...] = (),
    downloads: int = 0,
    acquire: bool = False,
) -> dict[str, Any]:
    started = await start_investigation_http(client, seeded)
    approved = await approve_envelope_http(
        client,
        seeded.investigation.id,
        providers=providers,
        budget_limits={
            "bridge_turns": 30,
            "vt_units": 24,
            "shodan_credits": 0,
            "downloads": downloads,
        },
        allow_acquisition=acquire,
        max_acquire_volume=downloads,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved"] is True
    driver.edition_id = seeded.edition.id
    return started


async def _approve_opinion(
    client: httpx.AsyncClient,
    *,
    investigation_id: UUID,
    blocks: tuple[Mapping[str, Any], ...],
    unresolved: tuple[str, ...] = (),
) -> dict[str, Any]:
    composed = await compose_opinion_http(client, investigation_id)
    assert composed.status_code == 200, composed.text
    draft = composed.json()["draft"]
    revised = await revise_opinion_http(
        client,
        UUID(draft["id"]),
        expected_version=draft["version"],
        blocks=blocks,
        unresolved_points=unresolved,
    )
    assert revised.status_code == 200, revised.text
    opinion = revised.json()["draft"]
    approved = await approve_opinion_http(
        client,
        UUID(opinion["id"]),
        version=opinion["version"],
        sha256=opinion["content_sha256"],
    )
    assert approved.status_code == 200, approved.text
    return dict(approved.json()["approved"])


def _assert_observations_reach_following_bridge_turns(
    transport: ScriptedInvestigationTransport,
    invocations: Sequence[Any],
    *,
    immediate: bool = True,
) -> None:
    """Every produced proof ID must reach a Bridge prompt following its first producer.

    Observations are content-addressed: several tools may return the same ID, which is
    presented once, right after the first invocation that produced it. ``immediate``
    requires delivery in the very next turn; ``immediate=False`` accepts any later turn
    (useful when one turn produces more evidence than the bounded delta window).
    """
    delivered_for: set[Any] = set()
    for index, invocation in enumerate(invocations):
        fresh = [ref for ref in invocation.response_refs if ref not in delivered_for]
        delivered_for.update(invocation.response_refs)
        if not fresh:
            continue
        assert index + 1 < len(transport.prompts), (
            invocation.tool_name,
            "no following Bridge turn consumed the tool observation",
        )
        following = (
            [transport.prompts[index + 1]] if immediate else list(transport.prompts[index + 1 :])
        )
        for reference in fresh:
            assert any(str(reference) in prompt for prompt in following), (
                invocation.tool_name,
                reference,
                "no following Bridge turn carried the observation ID",
            )


async def _persist_observation(
    uow_factory: Any,
    artifact_store: Any,
    *,
    investigation_id: UUID,
    kind: EvidenceKind,
    payload: Mapping[str, Any],
    source: str,
    object_type: str,
    object_id: str,
) -> EvidenceObservation:
    blob_id, sha256 = await artifact_store.put_canonical_json(
        dict(payload), bucket="w14-synthetic-evidence"
    )
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=kind,
        blob_id=blob_id,
        sha256=sha256,
        source=source,
        source_version="w14-v1",
        observed_at=datetime.now(UTC),
        object_type=object_type,
        object_id=object_id,
        limitations=("Synthetic inert workflow fixture.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    return observation


async def _seed_hypotheses(
    uow_factory: Any,
    investigation_id: UUID,
    hypotheses: tuple[Hypothesis, ...],
) -> None:
    async with uow_factory() as uow:
        state = await uow.investigation_states.get(investigation_id)
        assert state is not None
        updated = replace(state, hypotheses=hypotheses, version=state.version + 1)
        await uow.investigation_states.save_if_version(updated, expected_version=state.version)
        await uow.commit()


@pytest.mark.asyncio
async def test_w14_a_similarity_rule_revision_and_new_article_sample(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W14-A-similarity-rule-novel-sample")
    async with driver:
        marker_common = f"W14SharedLoaderHandshake{uuid4().hex[:12]}"
        marker_c = f"W14DistinctStageC{uuid4().hex[:12]}"
        bytes_a = b"MZ\x00W14-sample-A:" + marker_common.encode() + b":relocated-A"
        bytes_b = b"MZ\x00W14-sample-B:" + marker_common.encode() + b":relocated-B"
        bytes_c = b"MZ\x00W14-sample-C:" + marker_c.encode() + b":new-stage"
        goodware_bytes = b"synthetic-goodware-W14-no-family-marker"
        holdout_bytes = b"synthetic-holdout-W14-no-family-marker"
        sha_a = hashlib.sha256(bytes_a).hexdigest()
        sha_b = hashlib.sha256(bytes_b).hexdigest()
        sha_c = hashlib.sha256(bytes_c).hexdigest()
        goodware_sha = hashlib.sha256(goodware_bytes).hexdigest()
        holdout_sha = hashlib.sha256(holdout_bytes).hexdigest()
        base = SyntheticSampleMaterial.unique()
        material = replace(base, seed_hash=sha_a, sample_bytes=bytes_c, sample_hash=sha_c)
        seeded = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w14-a-{uuid4().hex[:8]}",
            material=material,
            article_claim=f"The reference article documents seed SHA-256 {sha_a}.",
        )
        await persist_sample_association_fixture(
            uow_factory,
            driver.artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=seeded.subject.id,
            sample_bytes=bytes_a,
            origin=SampleOrigin.SEED,
            logical_name="sample-a-inert.bin",
        )
        association_b = await persist_sample_association_fixture(
            uow_factory,
            driver.artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=seeded.subject.id,
            sample_bytes=bytes_b,
            origin=SampleOrigin.MANUAL,
            logical_name="sample-b-inert.bin",
        )
        vt = HashCheckedVirusTotal(material=material)
        rule_candidate_id = uuid4()
        baseline_id = uuid4()
        similarity = _family_similarity(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            investigation_id=seeded.investigation.id,
            feature_packs={
                sha_a: _feature_pack(sha_a, strings=((marker_common, 0x240),)),
                sha_b: _feature_pack(sha_b, strings=((marker_common, 0x318),)),
                sha_c: _feature_pack(sha_c, strings=((marker_c, 0x420),)),
            },
            baseline_id=baseline_id,
        )
        rule_corpus = _ControlledRuleCorpus(
            {
                sha_a: bytes_a,
                sha_b: bytes_b,
                sha_c: bytes_c,
                goodware_sha: goodware_bytes,
                holdout_sha: holdout_bytes,
            },
            goodware=(goodware_sha,),
            holdout=(holdout_sha,),
        )

        async def plan(observations: tuple[EvidenceObservation, ...]) -> Mapping[str, Any]:
            async with uow_factory() as uow:
                associations = tuple(
                    await uow.sample_associations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                versions = tuple(
                    await uow.detection_candidates.list_for_candidate(rule_candidate_id)
                )
            associated_hashes = {item.sample_sha256 for item in associations}
            similarity_calls = [item for item in invocations if item.tool_name == "samples.compare"]
            if not similarity_calls:
                return decision(
                    "Compare the exact article seed and the independently retained variant.",
                    tool_call=_call("samples.compare", {"sample_sha256s": [sha_a, sha_b]}),
                )
            common_calls = [item for item in invocations if item.tool_name == "features.common"]
            if not common_calls:
                return decision(
                    "Inspect measured shared features after the persisted comparison.",
                    tool_call=_call(
                        "features.common",
                        {"sample_sha256s": [sha_a, sha_b], "limit": 24},
                    ),
                )
            candidate_calls = [
                item for item in invocations if item.tool_name == "signatures.candidates"
            ]
            if not candidate_calls:
                return decision(
                    "Ask for signature candidates from the measured common-feature table.",
                    tool_call=_call(
                        "signatures.candidates",
                        {"sample_sha256s": [sha_a, sha_b], "limit": 16},
                    ),
                )
            if not versions:
                observation = await _string_observation(
                    driver, observations, object_id=sha_a, marker=marker_common
                )
                return decision(
                    "Propose a measured common loader motif as YARA v1.",
                    tool_call=_call(
                        "rule.propose",
                        {
                            "candidate_id": str(rule_candidate_id),
                            "condition": "any",
                            "motifs": [
                                {
                                    "kind": "string",
                                    "value": marker_common,
                                    "provenance_kind": "p09_invariant",
                                    "provenance_id": str(observation.id),
                                    "observation_refs": [str(observation.id)],
                                }
                            ],
                        },
                    ),
                )
            rule_tests = [item for item in invocations if item.tool_name == "rule.test"]
            if not any(item.normalized_parameters.get("version") == 1 for item in rule_tests):
                return decision(
                    "Compile and measure v1 against the two samples, synthetic benign-like "
                    "fixture, and holdout.",
                    tool_call=_call(
                        "rule.test", {"candidate_id": str(rule_candidate_id), "version": 1}
                    ),
                )
            if sha_c not in associated_hashes:
                return decision(
                    "Acquire the newly discovered stage C for an explicit v1 boundary test.",
                    tool_call=_call(
                        "vt.download_sample",
                        {
                            "sha256": sha_c,
                            "purpose": "Acquire one inert synthetic stage C for the rule holdout.",
                            "evidence_refs": [str(seeded.seed_observation.id)],
                        },
                    ),
                )
            if not any(
                item.tool_name == "samples.compare"
                and list(item.normalized_parameters.get("sample_sha256s", []))
                == [sha_a, sha_b, sha_c]
                for item in invocations
            ):
                return decision(
                    "Recompare after C arrives and retain its measured divergence.",
                    tool_call=_call(
                        "samples.compare",
                        {"sample_sha256s": [sha_a, sha_b, sha_c]},
                    ),
                )
            if len(rule_tests) < 2:
                return decision(
                    "Retest v1 on C; a miss keeps C a candidate and informs the revision.",
                    tool_call=_call(
                        "rule.test",
                        {"candidate_id": str(rule_candidate_id), "version": 1},
                        purpose="Retest immutable v1 now that stage C joined the corpus.",
                    ),
                )
            if len(versions) == 1:
                observation = await _string_observation(
                    driver, observations, object_id=sha_c, marker=marker_c
                )
                first_observation = await _string_observation(
                    driver, observations, object_id=sha_a, marker=marker_common
                )
                return decision(
                    "Revise v1 with C's new measured motif while retaining the A/B invariant.",
                    tool_call=_call(
                        "rule.revise",
                        {
                            "candidate_id": str(rule_candidate_id),
                            "parent_version": 1,
                            "condition": "any",
                            "motifs": [
                                {
                                    "kind": "string",
                                    "value": marker_common,
                                    "provenance_kind": "p09_invariant",
                                    "provenance_id": str(first_observation.id),
                                    "observation_refs": [str(first_observation.id)],
                                },
                                {
                                    "kind": "string",
                                    "value": marker_c,
                                    "provenance_kind": "p09_invariant",
                                    "provenance_id": str(observation.id),
                                    "observation_refs": [str(observation.id)],
                                },
                            ],
                        },
                    ),
                )
            if not any(item.normalized_parameters.get("version") == 2 for item in rule_tests):
                return decision(
                    "Compile v2 against A, B, C, the synthetic benign-like fixture, and the "
                    "independent holdout.",
                    tool_call=_call(
                        "rule.test", {"candidate_id": str(rule_candidate_id), "version": 2}
                    ),
                )
            if not any(item.tool_name == "rule.compare" for item in invocations):
                return decision(
                    "Compare immutable v1 and v2 match sets for the newly arrived sample.",
                    tool_call=_call(
                        "rule.compare",
                        {"candidate_id": str(rule_candidate_id), "version_a": 1, "version_b": 2},
                    ),
                )

            c_observations = tuple(
                item.id
                for item in observations
                if item.object_id == sha_c
                and item.kind in {EvidenceKind.BINARY_ANALYSIS, EvidenceKind.BINARY_STRING}
            )
            return decision(
                "Finish with C explicitly candidate and the shared measured feature cited.",
                next_action=AgentNextAction.FINISH.value,
                finding_proposals=[
                    {
                        "kind": FindingKind.NEW_SAMPLE.value,
                        "assessment": FindingAssessment.SUPPORTED.value,
                        "statement": (
                            f"Sample {sha_c} is new relative to the source article; it remains "
                            "a candidate after v1 misses it and v2 is measured."
                        ),
                        "subject_ref": sha_c,
                        "observation_refs": [str(item) for item in c_observations],
                        "claim_refs": [],
                        "novelty": NoveltyClass.NEW_TO_ARTICLE.value,
                        "scope": "One exact quarantined stage C and local YARA measurements.",
                        "limitations": [
                            "C is not confirmed as a member of the article's family.",
                            "Synthetic local corpus results do not estimate global prevalence.",
                        ],
                    }
                ],
            )

        transport = ScriptedInvestigationTransport(
            [],
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
            analyze_pending_samples=True,
            final_response_builder=plan,
        )
        settings = investigation_settings(providers=True, local_analysis=True, rules=True)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=transport,
            virustotal=vt,
            include_local_analysis=True,
            rule_corpus_bytes=rule_corpus,
            yara_engine=IsolatedYaraRunner(),
            additional_ports=OptionalInvestigationToolPorts(sample_similarity=similarity),
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
            include_detection=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w14-a"
        ) as client:
            started = await _start_and_approve(
                driver,
                client,
                seeded=seeded,
                providers=("vt",),
                downloads=1,
                acquire=True,
            )
            qualification_evidence = await _persist_observation(
                uow_factory,
                driver.artifact_store,
                investigation_id=seeded.investigation.id,
                kind=EvidenceKind.BINARY_ANALYSIS,
                payload={"sample_sha256": sha_b, "retained_as": "independent-variant-B"},
                source="w14-human-qualification-fixture",
                object_type="sample",
                object_id=sha_b,
            )
            qualified = await qualify_sample_http(
                client,
                seeded.investigation.id,
                association_b.id,
                expected_version=association_b.version,
                qualification=SampleQualification.CONFIRMED,
                evidence_refs=(qualification_evidence.id,),
            )
            assert qualified.status_code == 200, qualified.text
            assert qualified.json()["association"]["qualification"] == "confirmed"
            await driver._perform(
                "run_a_end_to_end",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name=(
                    "compare exact samples, compile and revise YARA with a controlled VT fake"
                ),
                identities={"investigation": seeded.investigation.id},
                assertions=("real-postgres-tool-and-evidence-persistence", "no-network"),
            )

            async with uow_factory() as uow:
                associations = tuple(
                    await uow.sample_associations.list_for_investigation(seeded.investigation.id)
                )
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                versions = tuple(
                    await uow.detection_candidates.list_for_candidate(rule_candidate_id)
                )
                findings = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )

            assert {sha_a, sha_b, sha_c} <= {item.sample_sha256 for item in associations}
            association_c = next(item for item in associations if item.sample_sha256 == sha_c)
            assert association_c.qualification is SampleQualification.CANDIDATE
            # v1 is measured "tested" on A/B; v2 also matches C, an out-of-cluster
            # candidate, which by policy keeps the version a candidate: C is never
            # promoted to a confirmed member automatically.
            assert [(item.version, item.status.value) for item in versions] == [
                (1, "tested"),
                (2, "candidate"),
            ]
            assert {
                "samples.compare",
                "features.common",
                "signatures.candidates",
                "rule.propose",
                "rule.test",
                "rule.revise",
                "rule.compare",
                "vt.download_sample",
                "sample.analyze",
            } <= {item.tool_name for item in invocations}
            assert any(
                item.kind is EvidenceKind.BINARY_ANALYSIS and item.source == "sample_similarity"
                for item in observations
            )
            await _string_observation(driver, observations, object_id=sha_c, marker=marker_c)
            assert len(findings) == 1
            new_sample = findings[0]
            assert new_sample.kind is FindingKind.NEW_SAMPLE
            cited = {item.id: item for item in observations}
            assert new_sample.observation_refs
            assert all(cited[ref].object_id == sha_c for ref in new_sample.observation_refs), (
                "the NEW_SAMPLE finding must cite observations of sample C only"
            )
            assert new_sample.novelty is NoveltyClass.NEW_TO_ARTICLE
            assert new_sample.assessment is FindingAssessment.SUPPORTED

            reports = [
                item
                for item in observations
                if item.kind is EvidenceKind.YARA_MATCH
                and item.object_type == "detection_candidate_version"
            ]
            report_payloads = [
                await driver.artifact_store.read_json(item.blob_id) for item in reports
            ]
            report_payloads = [item for item in report_payloads if item["operation"] == "test"]
            assert len(report_payloads) == 3
            assert all(item["execution_status"] == "ok" for item in report_payloads)
            assert all(item["validation_status"] == "real_partial" for item in report_payloads)
            first_v1 = next(
                item
                for item in report_payloads
                if item["version_id"] == str(versions[0].id)
                and sha_c not in item["corpus_sha256s_by_role"].get("out_of_cluster_candidate", [])
            )
            roles_first_v1 = {item["role"]: item for item in first_v1["roles"]}
            assert {sha_a, sha_b} <= set(roles_first_v1["confirmed_positive"]["matches"])
            assert goodware_sha in roles_first_v1["goodware"]["non_matches"]
            first_goodware_case = roles_first_v1["goodware"]["test_cases"][0]
            assert first_goodware_case["provenance"] == RuleSampleProvenance.SYNTHETIC.value
            assert holdout_sha in roles_first_v1["holdout"]["non_matches"]
            v1_after_c = next(
                item
                for item in report_payloads
                if item["version_id"] == str(versions[0].id)
                and sha_c in item["corpus_sha256s_by_role"].get("out_of_cluster_candidate", [])
            )
            roles_v1 = {item["role"]: item for item in v1_after_c["roles"]}
            assert {sha_a, sha_b} <= set(roles_v1["confirmed_positive"]["matches"])
            assert sha_c in roles_v1["out_of_cluster_candidate"]["non_matches"]
            assert goodware_sha in roles_v1["goodware"]["non_matches"]
            assert holdout_sha in roles_v1["holdout"]["non_matches"]
            v2_report = next(
                item
                for item in report_payloads
                if item["version_id"] == str(versions[1].id) and item["operation"] == "test"
            )
            roles_v2 = {item["role"]: item for item in v2_report["roles"]}
            assert {sha_a, sha_b} <= set(roles_v2["confirmed_positive"]["matches"])
            assert sha_c in roles_v2["out_of_cluster_candidate"]["matches"]
            assert goodware_sha in roles_v2["goodware"]["non_matches"]
            assert holdout_sha in roles_v2["holdout"]["non_matches"]
            assert all(
                item.tool_name != "rule.test" or item.status is ToolInvocationStatus.SUCCEEDED
                for item in invocations
            )
            _assert_observations_reach_following_bridge_turns(transport, invocations)

            similarity_observation = next(
                item
                for item in observations
                if item.source == "sample_similarity" and item.kind is EvidenceKind.BINARY_ANALYSIS
            )
            blocks = (
                {
                    "block_type": OpinionBlockType.ADDITIONAL_SAMPLES.value,
                    "text": (
                        f"Sample C ({sha_c}) is new relative to the source article and remains "
                        f"a candidate. Samples A and B share the measured specificity "
                        f"{marker_common}; v1 matched both and missed C, while v2 measured C."
                    ),
                    "finding_refs": [str(new_sample.id)],
                    "evidence_refs": [
                        str(similarity_observation.id),
                        *[str(x) for x in new_sample.observation_refs],
                    ],
                    "qualification": OpinionClaimQualification.INFERENCE.value,
                    "certainty": 0.86,
                },
            )
            opinion = await _approve_opinion(
                client,
                investigation_id=seeded.investigation.id,
                blocks=blocks,
                unresolved=("C has not been human-qualified as a confirmed family member.",),
            )
            async with uow_factory() as uow:
                persisted = await uow.analyst_opinions.get(UUID(opinion["id"]))
            assert persisted is not None and persisted.content_sha256 == opinion["content_sha256"]
            assert persisted.status.value == "approved"
            assert sha_c in persisted.blocks[0].text
            assert marker_common in persisted.blocks[0].text
            assert "candidate" in persisted.blocks[0].text


@pytest.mark.asyncio
async def test_w14_c_contradictory_report_keeps_one_hypothesis_open(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W14-C-contradiction-open-hypothesis")
    async with driver:
        material = SyntheticSampleMaterial.unique()
        sample_bytes = b"MZ\x00W14-C-inert-sample:" + uuid4().bytes + b":article-beacon-claim"
        sample_sha = hashlib.sha256(sample_bytes).hexdigest()
        material = replace(material, sample_bytes=sample_bytes, sample_hash=sample_sha)
        target_host = "203.0.113.77"
        seeded = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w14-c-{uuid4().hex[:8]}",
            material=material,
            article_claim=f"The report claims that the sample contacted {target_host}.",
        )
        await persist_sample_association_fixture(
            uow_factory,
            driver.artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=seeded.subject.id,
            sample_bytes=sample_bytes,
            origin=SampleOrigin.SEED,
            logical_name="w14-c-inert.bin",
        )
        hypothesis = Hypothesis(
            id="H-C2-1",
            statement=f"The investigated sample contacted {target_host} during the incident.",
        )
        transport = ScriptedInvestigationTransport(
            [],
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
            analyze_pending_samples=True,
            final_response_builder=None,
        )
        settings = investigation_settings(providers=False, local_analysis=True, rules=False)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=transport,
            include_local_analysis=True,
            additional_ports=OptionalInvestigationToolPorts(
                behavior_report_importer=BehaviorReportImporter()
            ),
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w14-c"
        ) as client:
            started = await _start_and_approve(
                driver,
                client,
                seeded=seeded,
                providers=(),
                downloads=0,
                acquire=False,
            )
            await _seed_hypotheses(uow_factory, seeded.investigation.id, (hypothesis,))

            def _report(destination_ip: str, host: str) -> str:
                return json.dumps(
                    {
                        "sample_sha256": sample_sha,
                        "network_sessions": [
                            {
                                "protocol": "tcp",
                                "destination_ip": destination_ip,
                                "destination_port": 443,
                                "host": host,
                            }
                        ],
                    },
                    sort_keys=True,
                )

            report_other = _report("192.0.2.55", "operator-run-a.invalid")
            report_target = _report(target_host, "operator-run-b.invalid")

            async def choose_after_report(
                observations: tuple[EvidenceObservation, ...],
            ) -> Mapping[str, Any]:
                async with uow_factory() as uow:
                    invocations = tuple(
                        await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                    )
                imports = [
                    item for item in invocations if item.tool_name == "sandbox.report.import"
                ]
                if len(imports) < 2:
                    payload = report_other if not imports else report_target
                    return decision(
                        "Import one supplied operator report, scoped to this exact sample.",
                        tool_call=_call(
                            "sandbox.report.import",
                            {
                                "sample_sha256": sample_sha,
                                "source": BehaviorEvidenceSource.OPERATOR.value,
                                "report_json": payload,
                            },
                        ),
                    )
                report_observations = [
                    item
                    for item in observations
                    if item.source == "autowork.sandbox-report.operator"
                    and item.object_id == sample_sha
                ]
                assert len(report_observations) == 2
                # Observation IDs are deterministic per payload: pick them by content.
                target_observation = None
                other_observation = None
                for item in report_observations:
                    rendered = json.dumps(
                        await driver.artifact_store.read_json(item.blob_id), ensure_ascii=True
                    )
                    if target_host in rendered:
                        target_observation = item
                    else:
                        other_observation = item
                assert target_observation is not None and other_observation is not None
                return decision(
                    "Two independent operator runs disagree about the endpoint; keep both linked "
                    "to H-C2-1 so the disagreement leaves the hypothesis open.",
                    next_action=AgentNextAction.FINISH.value,
                    hypothesis_refs=[hypothesis.id],
                    finding_proposals=[
                        {
                            "kind": FindingKind.CLAIM_VERIFICATION.value,
                            "assessment": FindingAssessment.SUPPORTED.value,
                            "statement": (
                                f"An operator run records a session to {target_host}, in line "
                                "with the article assertion."
                            ),
                            "subject_ref": sample_sha,
                            "observation_refs": [str(target_observation.id)],
                            "claim_refs": ["L1.1"],
                            "hypothesis_refs": [hypothesis.id],
                            "novelty": NoveltyClass.CLAIM_CORRECTION.value,
                            "scope": "One supplied sample run and its recorded network events.",
                            "limitations": [
                                "A single supplied run does not establish incident-time traffic."
                            ],
                        },
                        {
                            "kind": FindingKind.CLAIM_VERIFICATION.value,
                            "assessment": FindingAssessment.REFUTED.value,
                            "statement": (
                                "A second operator run records a different endpoint and does "
                                f"not corroborate {target_host} in that run."
                            ),
                            "subject_ref": sample_sha,
                            "observation_refs": [str(other_observation.id)],
                            "claim_refs": ["L1.1"],
                            "hypothesis_refs": [hypothesis.id],
                            "novelty": NoveltyClass.CLAIM_CORRECTION.value,
                            "scope": "One supplied sample run and its recorded network events.",
                            "limitations": [
                                "A missing session in this report does not establish that no "
                                "communication occurred outside this run."
                            ],
                        },
                    ],
                )

            transport._final_response_builder = choose_after_report
            await driver._perform(
                "run_c_contradictory_claim",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name="import one operator report and preserve the contradiction as open",
                identities={"investigation": seeded.investigation.id},
                assertions=("hypothesis-linked-findings", "real-postgres-state", "no-network"),
            )

            async with uow_factory() as uow:
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                findings = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )
                state = await uow.investigation_states.get(seeded.investigation.id)

            assert any(item.source == "autowork.sandbox-report.operator" for item in observations)
            assert sum(item.tool_name == "sandbox.report.import" for item in invocations) == 2
            assert state is not None
            persisted_hypothesis = next(
                item for item in state.hypotheses if item.id == hypothesis.id
            )
            assert persisted_hypothesis.status is HypothesisStatus.OPEN
            linked_ids = {str(item) for item in persisted_hypothesis.finding_refs}
            linked = [item for item in findings if str(item.id) in linked_ids]
            assert len(linked) == 2
            assert {item.assessment for item in linked} == {
                FindingAssessment.SUPPORTED,
                FindingAssessment.REFUTED,
            }
            assert all(len(item.observation_refs) == 1 for item in linked)
            _assert_observations_reach_following_bridge_turns(transport, invocations)

            report_observation = next(
                item for item in observations if item.source == "autowork.sandbox-report.operator"
            )
            opinion = await _approve_opinion(
                client,
                investigation_id=seeded.investigation.id,
                blocks=(
                    {
                        "block_type": OpinionBlockType.CLAIM_VERIFICATION.value,
                        "text": (
                            f"The article's claim about {target_host} remains an open hypothesis. "
                            "The supplied operator report records another endpoint for this one "
                            "sample run; the report's coverage cannot exclude communications "
                            "outside that run."
                        ),
                        "finding_refs": [str(item.id) for item in linked],
                        "evidence_refs": [
                            str(seeded.seed_observation.id),
                            str(report_observation.id),
                        ],
                        "qualification": OpinionClaimQualification.HYPOTHESIS.value,
                        "certainty": 0.45,
                    },
                ),
                unresolved=("Incident-time communication with the claimed host is unresolved.",),
            )
            async with uow_factory() as uow:
                persisted = await uow.analyst_opinions.get(UUID(opinion["id"]))
            assert persisted is not None and persisted.status.value == "approved"
            assert persisted.blocks[0].qualification is OpinionClaimQualification.HYPOTHESIS
            assert "remains an open hypothesis" in persisted.blocks[0].text
            assert persisted.blocks[0].certainty < 0.5


@pytest.mark.asyncio
async def test_w14_d_evidence_bounded_opinion_without_yara(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W14-D-evidence-bounded-no-yara")
    async with driver:
        material = SyntheticSampleMaterial.unique()
        sample_bytes = b"MZ\x00W14-D-static-loader:" + uuid4().bytes + b":inert"
        sample_sha = hashlib.sha256(sample_bytes).hexdigest()
        material = replace(material, sample_bytes=sample_bytes, sample_hash=sample_sha)
        seeded = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w14-d-{uuid4().hex[:8]}",
            material=material,
            article_claim=f"The report describes the quarantined sample {sample_sha}.",
        )
        await persist_sample_association_fixture(
            uow_factory,
            driver.artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=seeded.subject.id,
            sample_bytes=sample_bytes,
            origin=SampleOrigin.SEED,
            logical_name="w14-d-inert.bin",
        )
        transport = ScriptedInvestigationTransport(
            [],
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
            analyze_pending_samples=True,
        )
        settings = investigation_settings(providers=False, local_analysis=True, rules=False)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=transport,
            include_local_analysis=True,
            additional_ports=OptionalInvestigationToolPorts(
                behavior_report_importer=BehaviorReportImporter()
            ),
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w14-d"
        ) as client:
            started = await _start_and_approve(
                driver,
                client,
                seeded=seeded,
                providers=(),
                downloads=0,
                acquire=False,
            )

            async def report_then_reason(
                observations: tuple[EvidenceObservation, ...],
            ) -> Mapping[str, Any]:
                async with uow_factory() as uow:
                    invocations = tuple(
                        await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                    )
                    findings = tuple(
                        await uow.investigation_findings.list_for_investigation(
                            seeded.investigation.id
                        )
                    )
                if not any(item.tool_name == "sandbox.report.import" for item in invocations):
                    report_json = json.dumps(
                        {
                            "sample_sha256": sample_sha,
                            "process_tree": [
                                {"pid": 1, "name": "sample.exe"},
                                {"pid": 2, "ppid": 1, "name": "worker.exe"},
                            ],
                            "network_sessions": [
                                {
                                    "protocol": "tcp",
                                    "destination_ip": "192.0.2.101",
                                    "destination_port": 443,
                                    "host": "telemetry-fixture.invalid",
                                }
                            ],
                        },
                        sort_keys=True,
                    )
                    return decision(
                        "Import a supplied inert report; do not infer behavior from static "
                        "strings alone.",
                        tool_call=_call(
                            "sandbox.report.import",
                            {
                                "sample_sha256": sample_sha,
                                "source": BehaviorEvidenceSource.OPERATOR.value,
                                "report_json": report_json,
                            },
                        ),
                    )
                static_observation = next(
                    item
                    for item in observations
                    if item.source == "autowork.static_analysis" and item.object_id == sample_sha
                )
                behavior_observation = next(
                    item
                    for item in observations
                    if item.source == "autowork.sandbox-report.operator"
                    and item.object_id == sample_sha
                )
                supported_findings = tuple(
                    item
                    for item in findings
                    if item.assessment is FindingAssessment.SUPPORTED and item.observation_refs
                )
                if supported_findings:
                    static_finding = next(
                        item for item in supported_findings if item.kind is FindingKind.CAPABILITY
                    )
                    behavior_finding = next(
                        item
                        for item in supported_findings
                        if item.kind is FindingKind.NETWORK_BEHAVIOUR
                    )
                    response = decision(
                        "Draft fact and inference blocks from the persisted supported findings.",
                        next_action=AgentNextAction.FINISH.value,
                    )
                    response["opinion_delta"] = {
                        "blocks": [
                            {
                                "block_type": OpinionBlockType.NETWORK_AND_INFRASTRUCTURE.value,
                                "text": (
                                    "The supplied report records a TCP session to the fixture "
                                    "endpoint for this sample."
                                ),
                                "finding_refs": [str(behavior_finding.id)],
                                "evidence_refs": [str(behavior_observation.id)],
                                "qualification": OpinionClaimQualification.FACT.value,
                                "certainty": 0.95,
                            },
                            {
                                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                                "text": (
                                    "The process and network details are consistent with a "
                                    "staged worker chain; that relationship is inferred."
                                ),
                                "finding_refs": [
                                    str(static_finding.id),
                                    str(behavior_finding.id),
                                ],
                                "evidence_refs": [
                                    str(static_observation.id),
                                    str(behavior_observation.id),
                                ],
                                "qualification": OpinionClaimQualification.INFERENCE.value,
                                "certainty": 0.61,
                            },
                        ]
                    }
                    return response
                return decision(
                    "Finish with process and network events as observed evidence, and the chain "
                    "interpretation explicitly inferred.",
                    next_action=AgentNextAction.CONTINUE.value,
                    finding_proposals=[
                        {
                            "kind": FindingKind.CAPABILITY.value,
                            "assessment": FindingAssessment.SUPPORTED.value,
                            "statement": (
                                "Static analysis recorded a PE format and its extracted strings."
                            ),
                            "subject_ref": sample_sha,
                            "observation_refs": [str(static_observation.id)],
                            "claim_refs": [],
                            "novelty": NoveltyClass.NOT_CONFIRMED.value,
                            "scope": "Local static analysis of one quarantined sample.",
                            "limitations": ["Static capability evidence does not prove execution."],
                        },
                        {
                            "kind": FindingKind.NETWORK_BEHAVIOUR.value,
                            "assessment": FindingAssessment.SUPPORTED.value,
                            "statement": (
                                "The supplied report records one TCP session to the fixture "
                                "endpoint."
                            ),
                            "subject_ref": sample_sha,
                            "observation_refs": [str(behavior_observation.id)],
                            "claim_refs": [],
                            "novelty": NoveltyClass.NOT_CONFIRMED.value,
                            "scope": "One operator-supplied report for this exact sample.",
                            "limitations": ["Imported report events are untrusted and run-scoped."],
                        },
                    ],
                )

            transport._final_response_builder = report_then_reason
            await driver._perform(
                "run_d_no_yara",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name="import bounded behavior evidence and finish without a rule proposal",
                identities={"investigation": seeded.investigation.id},
                assertions=("static-observed-inferred-distinction", "normal-finish", "no-yara"),
            )

            async with uow_factory() as uow:
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                findings = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )
                state = await uow.investigation_states.get(seeded.investigation.id)
                investigation = await uow.analyst_investigations.get(seeded.investigation.id)

            static_observation = next(
                item for item in observations if item.source == "autowork.static_analysis"
            )
            behavior_observation = next(
                item for item in observations if item.source == "autowork.sandbox-report.operator"
            )
            assert (
                static_observation.sha256
                == hashlib.sha256(
                    await driver.artifact_store.read_bytes(static_observation.blob_id)
                ).hexdigest()
            )
            assert (
                behavior_observation.sha256
                == hashlib.sha256(
                    await driver.artifact_store.read_bytes(behavior_observation.blob_id)
                ).hexdigest()
            )
            assert {item.kind for item in findings} == {
                FindingKind.CAPABILITY,
                FindingKind.NETWORK_BEHAVIOUR,
            }
            assert not any(item.kind is EvidenceKind.YARA_MATCH for item in observations)
            assert not any(item.tool_name.startswith("rule.") for item in invocations)
            assert state is not None and state.stop_reason is not None
            assert state.stop_reason.value == "agent_finished"
            assert investigation is not None and investigation.status.value == "completed"
            _assert_observations_reach_following_bridge_turns(transport, invocations)

            static_finding = next(item for item in findings if item.kind is FindingKind.CAPABILITY)
            behavior_finding = next(
                item for item in findings if item.kind is FindingKind.NETWORK_BEHAVIOUR
            )
            opinion = await _approve_opinion(
                client,
                investigation_id=seeded.investigation.id,
                blocks=(
                    {
                        "block_type": OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES.value,
                        "text": (
                            "Static analysis recorded PE strings and format; this is static "
                            "evidence, not proof of execution."
                        ),
                        "finding_refs": [str(static_finding.id)],
                        "evidence_refs": [str(static_observation.id)],
                        "qualification": OpinionClaimQualification.FACT.value,
                        "certainty": 0.95,
                    },
                    {
                        "block_type": OpinionBlockType.NETWORK_AND_INFRASTRUCTURE.value,
                        "text": (
                            "The supplied operator report records a TCP session to "
                            "telemetry-fixture.invalid during that run."
                        ),
                        "finding_refs": [str(behavior_finding.id)],
                        "evidence_refs": [str(behavior_observation.id)],
                        "qualification": OpinionClaimQualification.FACT.value,
                        "certainty": 0.95,
                    },
                    {
                        "block_type": OpinionBlockType.KEY_FINDINGS.value,
                        "text": (
                            "The process and network details are consistent with a staged worker "
                            "chain, but that relationship is inferred from static and "
                            "operator-supplied evidence."
                        ),
                        "finding_refs": [str(static_finding.id), str(behavior_finding.id)],
                        "evidence_refs": [str(static_observation.id), str(behavior_observation.id)],
                        "qualification": OpinionClaimQualification.INFERENCE.value,
                        "certainty": 0.61,
                    },
                ),
            )
            async with uow_factory() as uow:
                persisted = await uow.analyst_opinions.get(UUID(opinion["id"]))
            assert persisted is not None and persisted.status.value == "approved"
            assert {item.qualification for item in persisted.blocks} == {
                OpinionClaimQualification.FACT,
                OpinionClaimQualification.INFERENCE,
            }
            assert "inferred" in persisted.blocks[-1].text


@pytest.mark.asyncio
async def test_w14_e_retrohunt_subject_slots_replay_and_concurrent_investigations(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W14-E-retrohunt-slots-concurrency")
    async with driver:
        provider = _StrictRetrohuntProxy()
        material_a = SyntheticSampleMaterial.unique()
        marker_a = f"W14RetrohuntRuleA{uuid4().hex[:12]}"
        bytes_a = b"MZ\x00" + marker_a.encode() + b":inert-seed-A"
        sha_a = hashlib.sha256(bytes_a).hexdigest()
        material_a = replace(
            material_a,
            seed_hash=sha_a,
            sample_bytes=bytes_a,
            sample_hash=sha_a,
        )
        first = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w14-e-a-{uuid4().hex[:8]}",
            material=material_a,
            article_claim="Two scoped investigations measure a local candidate before Retrohunt.",
        )

        # The first seeded run is still queued (active); a subject admits a single
        # active run, so the independent second run is a completed one.
        completed_at = datetime.now(UTC)
        second_run = ProductionRun(
            subject_id=first.subject.id,
            edition_id=first.edition.id,
            run_number=2,
            status=ProductionRunStatus.READY,
            current_stage=ProductionStage.SYNTHESIS,
            started_at=completed_at,
            finished_at=completed_at,
        )
        second_publication = replace(
            first.publication,
            id=uuid4(),
            production_run_id=second_run.id,
        )
        async with uow_factory() as uow:
            await uow.production_runs.add(second_run)
            await uow.production_artifacts.append(second_publication)
            await uow.commit()
        material_b = SyntheticSampleMaterial.unique()
        marker_b = f"W14RetrohuntRuleB{uuid4().hex[:12]}"
        bytes_b = b"MZ\x00" + marker_b.encode() + b":inert-seed-B"
        sha_b = hashlib.sha256(bytes_b).hexdigest()
        material_b = replace(
            material_b,
            seed_hash=sha_b,
            sample_bytes=bytes_b,
            sample_hash=sha_b,
        )
        second = await seed_investigation_for_existing_article(
            uow_factory,
            driver.artifact_store,
            edition=first.edition,
            subject=first.subject,
            run=second_run,
            publication=second_publication,
            publication_bytes=first.publication_bytes,
            material=material_b,
            article_claim="A second independent workflow targets the same article subject.",
        )
        for seeded, sample_bytes in ((first, bytes_a), (second, bytes_b)):
            await persist_sample_association_fixture(
                uow_factory,
                driver.artifact_store,
                investigation_id=seeded.investigation.id,
                subject_id=first.subject.id,
                sample_bytes=sample_bytes,
                origin=SampleOrigin.SEED,
                logical_name="w14-retrohunt-inert-seed.bin",
            )

        goodware_bytes = b"MZ\x00W14-retro-goodware-no-marker"
        holdout_bytes = b"MZ\x00W14-retro-holdout-no-marker"
        goodware_sha = hashlib.sha256(goodware_bytes).hexdigest()
        holdout_sha = hashlib.sha256(holdout_bytes).hexdigest()
        corpus_a = _ControlledRuleCorpus(
            {sha_a: bytes_a, goodware_sha: goodware_bytes, holdout_sha: holdout_bytes},
            goodware=(goodware_sha,),
            holdout=(holdout_sha,),
        )
        corpus_b = _ControlledRuleCorpus(
            {sha_b: bytes_b, goodware_sha: goodware_bytes, holdout_sha: holdout_bytes},
            goodware=(goodware_sha,),
            holdout=(holdout_sha,),
        )
        rule_route = VirusTotalOperationRoute(
            primary=VirusTotalRouteStep(
                transport=VirusTotalTransportKind.PROXY,
                variant=VirusTotalEndpointVariant.V3,
            )
        )
        additional_ports = OptionalInvestigationToolPorts(
            retrohunt_provider=provider,
            vt_capabilities=VirusTotalCapabilities(retrohunt=True),
            vt_routing_policy=VirusTotalRoutingPolicy(
                routes={VirusTotalCapability.RETROHUNT: rule_route}
            ),
        )
        settings = investigation_settings(providers=True, local_analysis=True, rules=True)
        corpus_id = "w14-shared-local-test-corpus"
        corpus_sha = hashlib.sha256(corpus_id.encode()).hexdigest()
        parameters = {"window_days": 90, "scope": "bounded-test"}

        def stack(
            seeded,
            material,
            corpus,
            *,
            mode: str,
            candidate_id: UUID | None = None,
        ):
            candidate_id = candidate_id or uuid4()

            async def plan(observations: tuple[EvidenceObservation, ...]) -> Mapping[str, Any]:
                async with uow_factory() as uow:
                    invocations = tuple(
                        await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                    )
                    versions = tuple(
                        await uow.detection_candidates.list_for_candidate(candidate_id)
                    )
                string_observation = next(
                    (
                        item
                        for item in observations
                        if item.kind is EvidenceKind.BINARY_STRING
                        and item.object_id == material.sample_hash
                    ),
                    None,
                )
                if not versions:
                    assert string_observation is not None
                    return decision(
                        "Build one rule from the exact static string observation for this seed.",
                        tool_call=_call(
                            "rule.propose",
                            {
                                "candidate_id": str(candidate_id),
                                "condition": "any",
                                "motifs": [
                                    {
                                        "kind": "string",
                                        "value": marker_a if seeded is first else marker_b,
                                        "provenance_kind": "p09_invariant",
                                        "provenance_id": str(string_observation.id),
                                        "observation_refs": [str(string_observation.id)],
                                    }
                                ],
                            },
                        ),
                    )
                tests = [item for item in invocations if item.tool_name == "rule.test"]
                if not tests:
                    return decision(
                        "Measure the exact candidate, goodware, and independent holdout locally.",
                        tool_call=_call(
                            "rule.test", {"candidate_id": str(candidate_id), "version": 1}
                        ),
                    )
                report = next(
                    item
                    for item in observations
                    if item.kind is EvidenceKind.YARA_MATCH
                    and item.object_type == "detection_candidate_version"
                    and item.object_id == str(versions[0].id)
                )
                if mode == "prepare":
                    return decision(
                        "Stop after the successful local validation for explicit human review.",
                        next_action=AgentNextAction.FINISH.value,
                        finding_proposals=[
                            {
                                "kind": FindingKind.DETECTION.value,
                                "assessment": FindingAssessment.QUALIFIED.value,
                                "statement": (
                                    "A local YARA candidate was compiled and measured against one "
                                    "seed and two controls."
                                ),
                                "subject_ref": str(versions[0].candidate_id),
                                "observation_refs": [str(report.id)],
                                "claim_refs": [],
                                "novelty": NoveltyClass.NOT_CONFIRMED.value,
                                "scope": "One local exact-version test report.",
                                "limitations": [
                                    "Human review is required before any active submission."
                                ],
                            }
                        ],
                    )

                submissions = [
                    item for item in invocations if item.tool_name == "vt.retrohunt.submit"
                ]
                if mode == "replay-third":
                    if len(submissions) == 1:
                        return decision(
                            "Replay the committed submission with identical operation inputs.",
                            tool_call=_call(
                                "vt.retrohunt.submit",
                                {
                                    "version_id": str(versions[0].id),
                                    "source_sha256": versions[0].source_sha256,
                                    "corpus_id": corpus_id,
                                    "corpus_sha256": corpus_sha,
                                    "parameters": parameters,
                                },
                                purpose=(
                                    "Replay the exact committed Retrohunt operation after restart."
                                ),
                            ),
                        )
                    if len(submissions) == 2:
                        overflow_corpus = "w14-third-operation-must-be-refused"
                        return decision(
                            "Try a distinct third subject operation and observe the atomic slot "
                            "refusal.",
                            tool_call=_call(
                                "vt.retrohunt.submit",
                                {
                                    "version_id": str(versions[0].id),
                                    "source_sha256": versions[0].source_sha256,
                                    "corpus_id": overflow_corpus,
                                    "corpus_sha256": hashlib.sha256(
                                        overflow_corpus.encode()
                                    ).hexdigest(),
                                    "parameters": parameters,
                                },
                                purpose=(
                                    "Test the subject-wide third-slot boundary with a new "
                                    "operation key."
                                ),
                            ),
                        )
                    assert len(submissions) == 3
                    result_observation = next(
                        item
                        for item in observations
                        if item.source == "vt.retrohunt.submit"
                        and item.invocation_ref == submissions[-1].id
                    )
                    output = await driver.artifact_store.read_json(result_observation.blob_id)
                    assert output["result"]["reason"] == "subject_slot_limit"
                    assert output["result"]["slots_consumed"] == 2
                    assert output["result"]["slots_remaining"] == 0
                    return decision(
                        "Finish after the durable refusal; the previously recorded hit remains a "
                        "candidate.",
                        next_action=AgentNextAction.FINISH.value,
                        hypothesis_refs=[],
                    )

                if not submissions:
                    return decision(
                        "Submit the exact human-authorized version to the bounded fake proxy.",
                        tool_call=_call(
                            "vt.retrohunt.submit",
                            {
                                "version_id": str(versions[0].id),
                                "source_sha256": versions[0].source_sha256,
                                "corpus_id": corpus_id,
                                "corpus_sha256": corpus_sha,
                                "parameters": parameters,
                            },
                            purpose=(
                                "Submit the exact reviewed rule and corpus through the strict "
                                "fake proxy."
                            ),
                        ),
                    )
                request = next(
                    item
                    for item in provider.requests
                    if item.investigation_id == seeded.investigation.id
                )
                if not any(item.tool_name == "vt.retrohunt.results" for item in invocations):
                    return decision(
                        "Read the submitted reservation's completed result and preserve hits as "
                        "candidates.",
                        tool_call=_call(
                            "vt.retrohunt.results",
                            {"reservation_id": str(request.reservation_id)},
                        ),
                    )
                hit_observation = next(
                    item
                    for item in observations
                    if item.source == "VirusTotal Retrohunt"
                    and item.object_type == "vt_retrohunt_candidate_hit"
                )
                return decision(
                    "Record the exact Retrohunt hit as a candidate, not confirmed membership.",
                    next_action=AgentNextAction.FINISH.value,
                    finding_proposals=[
                        {
                            "kind": FindingKind.NEW_SAMPLE.value,
                            "assessment": FindingAssessment.QUALIFIED.value,
                            "statement": (
                                f"Retrohunt returned {hit_observation.object_id} as a candidate; "
                                "it is not confirmed as a subject member."
                            ),
                            "subject_ref": hit_observation.object_id,
                            "observation_refs": [str(hit_observation.id)],
                            "claim_refs": [],
                            "novelty": NoveltyClass.NOT_CONFIRMED.value,
                            "scope": "One result from the bounded fake Retrohunt provider.",
                            "limitations": [
                                "Provider hits remain candidates pending separate review."
                            ],
                        }
                    ],
                )

            transport = ScriptedInvestigationTransport(
                [],
                uow_factory=uow_factory,
                investigation_id=seeded.investigation.id,
                material=material,
                inject_analysis_at_turn=None,
                analyze_pending_samples=True,
                final_response_builder=plan,
            )
            factory = composition_factory(
                uow_factory=uow_factory,
                artifact_store=driver.artifact_store,
                settings=settings,
                transport=transport,
                include_local_analysis=True,
                rule_corpus_bytes=corpus,
                yara_engine=IsolatedYaraRunner(),
                additional_ports=additional_ports,
            )
            app, registry, dispatcher = build_investigation_app(
                uow_factory=uow_factory,
                artifact_store=driver.artifact_store,
                composition_factory=factory,
                include_detection=True,
            )
            return app, registry, dispatcher, transport, candidate_id, factory

        stacks = {}
        for seeded, material, corpus in (
            (first, material_a, corpus_a),
            (second, material_b, corpus_b),
        ):
            app, registry, dispatcher, transport, candidate_id, factory = stack(
                seeded, material, corpus, mode="prepare"
            )
            stacks[seeded.investigation.id] = {
                "seeded": seeded,
                "material": material,
                "corpus": corpus,
                "app": app,
                "registry": registry,
                "dispatcher": dispatcher,
                "transport": transport,
                "candidate_id": candidate_id,
                "factory": factory,
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=f"http://w14-e-prepare-{len(stacks)}",
            ) as client:
                started = await _start_and_approve(
                    driver,
                    client,
                    seeded=seeded,
                    providers=("vt",),
                    downloads=0,
                    acquire=False,
                )

                async def run_preparation_job(
                    app=app,
                    registry=registry,
                    dispatcher=dispatcher,
                    started=started,
                ):
                    await run_http_job(
                        app,
                        uow_factory,
                        registry,
                        dispatcher,
                        UUID(started["job_id"]),
                    )

                await driver._perform(
                    f"run_e_prepare_{len(stacks)}",
                    run_preparation_job,
                    via="service",
                    action_name=(
                        "compile and test one exact YARA version before human prevalidation"
                    ),
                    identities={
                        "investigation": seeded.investigation.id,
                        "subject": first.subject.id,
                    },
                    assertions=("postgres-test-report", "strict-fake-only"),
                )
            async with uow_factory() as uow:
                versions = tuple(await uow.detection_candidates.list_for_candidate(candidate_id))
                observations = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
            assert len(versions) == 1 and versions[0].version == 1
            test_reports = [
                await driver.artifact_store.read_json(item.blob_id)
                for item in observations
                if item.kind is EvidenceKind.YARA_MATCH
                and item.object_type == "detection_candidate_version"
            ]
            report = next(item for item in test_reports if item["operation"] == "test")
            assert report["execution_status"] == "ok"
            report_roles = {item["role"]: item for item in report["roles"]}
            assert seeded.investigation is not None
            expected_sha = material.sample_hash
            assert expected_sha in report_roles["confirmed_positive"]["matches"]
            assert goodware_sha in report_roles["goodware"]["non_matches"]
            assert holdout_sha in report_roles["holdout"]["non_matches"]
            assert {"rule.propose", "rule.test", "sample.analyze"} <= {
                item.tool_name for item in invocations
            }
            _assert_observations_reach_following_bridge_turns(
                stacks[seeded.investigation.id]["transport"], invocations
            )
            review = DetectionReviewService(uow_factory, driver.artifact_store)
            human_decision = await review.approve(
                versions[0].id,
                versions[0].source_sha256,
                actor="identified-human-reviewer",
                reason="Authorize this exact measured version for bounded Retrohunt.",
                known_limitations="Synthetic convenience corpus; not an attribution claim.",
                correlation_id=f"w14-retro-prevalidation:{versions[0].id}",
                retrohunt_authorized=True,
            )
            assert human_decision.retrohunt_authorization is not None
            assert human_decision.retrohunt_authorization.authorized is True
            assert human_decision.retrohunt_authorization.rule_version_id == versions[0].id
            assert human_decision.retrohunt_authorization.source_sha256 == versions[0].source_sha256
            stacks[seeded.investigation.id]["version"] = versions[0]

        submit_stacks = {}
        for seeded in (first, second):
            old = stacks[seeded.investigation.id]
            app, registry, dispatcher, transport, candidate_id, factory = stack(
                old["seeded"],
                old["material"],
                old["corpus"],
                mode="retrohunt",
                candidate_id=old["candidate_id"],
            )
            submit_stacks[seeded.investigation.id] = {
                **old,
                "app": app,
                "registry": registry,
                "dispatcher": dispatcher,
                "transport": transport,
                "candidate_id": candidate_id,
                "factory": factory,
            }

        async def resume_and_run(item: Mapping[str, Any]) -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=item["app"]),
                base_url=f"http://w14-e-submit-{item['seeded'].investigation.id}",
            ) as client:
                current = await client.get(f"/api/investigations/{item['seeded'].investigation.id}")
                assert current.status_code == 200, current.text
                resumed = await client.post(
                    f"/api/investigations/{item['seeded'].investigation.id}/resume",
                    json={"expected_version": current.json()["version"]},
                    headers={"Idempotency-Key": f"w14-e-resume-{item['seeded'].investigation.id}"},
                )
                assert resumed.status_code == 200, resumed.text
                await run_http_job(
                    item["app"],
                    uow_factory,
                    item["registry"],
                    item["dispatcher"],
                    UUID(resumed.json()["job_id"]),
                )

        await asyncio.gather(
            *(resume_and_run(submit_stacks[item.investigation.id]) for item in (first, second))
        )
        for seeded in (first, second):
            async with uow_factory() as uow:
                submitted_observations = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
            retro_submit = next(
                item for item in submitted_observations if item.source == "vt.retrohunt.submit"
            )
            retro_results = next(
                item
                for item in submitted_observations
                if item.source == "VirusTotal Retrohunt"
                and item.object_type == "vt_retrohunt_candidate_hit"
            )
            prompts = submit_stacks[seeded.investigation.id]["transport"].prompts
            assert len(prompts) >= 3
            assert str(retro_submit.id) in prompts[1]
            assert str(retro_results.id) in prompts[2]
        assert provider.requests and len(provider.requests) == 2
        assert {item.investigation_id for item in provider.requests} == {
            first.investigation.id,
            second.investigation.id,
        }
        assert {item.subject_id for item in provider.requests} == {first.subject.id}
        assert {item.slot_number for item in provider.requests} == {1, 2}

        async with uow_factory() as uow:
            subject_slots = await uow.retrohunt_reservations.count_for_subject(first.subject.id)
            first_invocations = tuple(
                await uow.tool_invocations.list_for_investigation(first.investigation.id)
            )
            first_observations = tuple(
                await uow.evidence_observations.list_for_investigation(first.investigation.id)
            )
            first_findings = tuple(
                await uow.investigation_findings.list_for_investigation(first.investigation.id)
            )
            first_version = await uow.detection_candidates.get(
                stacks[first.investigation.id]["version"].id
            )
            human_decisions = tuple(
                await uow.analyst_decisions.list_for_investigation(first.investigation.id)
            )
        assert subject_slots == 2
        assert first_version is not None
        persisted_authorization = next(
            item for item in human_decisions if item.target_id == first_version.id
        ).retrohunt_authorization
        assert persisted_authorization is not None
        assert persisted_authorization.rule_version_id == first_version.id
        assert persisted_authorization.source_sha256 == first_version.source_sha256
        assert sum(item.tool_name == "vt.retrohunt.submit" for item in first_invocations) == 1
        assert sum(item.tool_name == "vt.retrohunt.results" for item in first_invocations) == 1
        hit_observation = next(
            item
            for item in first_observations
            if item.source == "VirusTotal Retrohunt"
            and item.object_type == "vt_retrohunt_candidate_hit"
        )
        hit_payload = await driver.artifact_store.read_json(hit_observation.blob_id)
        assert hit_payload["qualification"] == "candidate"
        assert hit_payload["confirmed_member"] is False
        candidate_finding = next(
            item
            for item in first_findings
            if item.kind is FindingKind.NEW_SAMPLE and hit_observation.id in item.observation_refs
        )
        assert candidate_finding.assessment is FindingAssessment.QUALIFIED
        async with uow_factory() as uow:
            first_associations = tuple(
                await uow.sample_associations.list_for_investigation(first.investigation.id)
            )
        assert hit_observation.object_id not in {item.sample_sha256 for item in first_associations}

        replay_app, replay_registry, replay_dispatcher, replay_transport, _candidate, _factory = (
            stack(
                first,
                material_a,
                corpus_a,
                mode="replay-third",
                candidate_id=stacks[first.investigation.id]["candidate_id"],
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replay_app), base_url="http://w14-e-replay"
        ) as client:
            current = await client.get(f"/api/investigations/{first.investigation.id}")
            assert current.status_code == 200, current.text
            resumed = await client.post(
                f"/api/investigations/{first.investigation.id}/resume",
                json={"expected_version": current.json()["version"]},
                headers={"Idempotency-Key": f"w14-e-replay-{first.investigation.id}"},
            )
            assert resumed.status_code == 200, resumed.text
            await run_http_job(
                replay_app,
                uow_factory,
                replay_registry,
                replay_dispatcher,
                UUID(resumed.json()["job_id"]),
            )
        assert len(provider.requests) == 2, (
            "committed replay or refused third operation reached fake VT"
        )
        async with uow_factory() as uow:
            final_slots = await uow.retrohunt_reservations.count_for_subject(first.subject.id)
            final_invocations = tuple(
                await uow.tool_invocations.list_for_investigation(first.investigation.id)
            )
            final_observations = tuple(
                await uow.evidence_observations.list_for_investigation(first.investigation.id)
            )
            final_findings = tuple(
                await uow.investigation_findings.list_for_investigation(first.investigation.id)
            )
        assert final_slots == 2
        retro_submits = [
            item for item in final_invocations if item.tool_name == "vt.retrohunt.submit"
        ]
        assert len(retro_submits) == 3
        assert retro_submits[0].normalized_parameters == retro_submits[1].normalized_parameters
        assert retro_submits[1].status is ToolInvocationStatus.SUCCEEDED
        assert retro_submits[2].status is ToolInvocationStatus.FAILED
        submit_results = [
            await driver.artifact_store.read_json(item.blob_id)
            for item in final_observations
            if item.source == "vt.retrohunt.submit"
        ]
        by_invocation = {
            item.invocation_ref: item
            for item in final_observations
            if item.source == "vt.retrohunt.submit"
        }
        replay_payload = await driver.artifact_store.read_json(
            by_invocation[retro_submits[1].id].blob_id
        )
        denied_payload = await driver.artifact_store.read_json(
            by_invocation[retro_submits[2].id].blob_id
        )
        assert replay_payload["result"]["slots_consumed"] == 2
        assert replay_payload["result"]["slots_remaining"] == 0
        assert denied_payload["result"]["reason"] == "subject_slot_limit"
        assert denied_payload["result"]["slots_consumed"] == 2
        assert denied_payload["result"]["slots_remaining"] == 0
        assert len(submit_results) == 3
        assert len([item for item in final_findings if item.kind is FindingKind.NEW_SAMPLE]) == 1
        assert len(replay_transport.prompts) >= 3
        for invocation in retro_submits[1:]:
            observation = by_invocation[invocation.id]
            assert str(observation.id) in replay_transport.prompts[-1] or any(
                str(observation.id) in prompt for prompt in replay_transport.prompts
            )
        _assert_observations_reach_following_bridge_turns(
            replay_transport,
            tuple(
                item
                for item in final_invocations
                if item.id in {retro_submits[1].id, retro_submits[2].id}
            ),
        )

        blocks = (
            {
                "block_type": OpinionBlockType.PROPOSED_DETECTIONS.value,
                "text": (
                    f"The exact reviewed rule returned {hit_observation.object_id} as a candidate "
                    "under the fake bounded Retrohunt corpus. The hit is not confirmed membership; "
                    "the subject-wide budget consumed two slots and rejected a distinct third "
                    "operation."
                ),
                "finding_refs": [str(candidate_finding.id)],
                "evidence_refs": [str(hit_observation.id)],
                "qualification": OpinionClaimQualification.INFERENCE.value,
                "certainty": 0.73,
            },
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=replay_app), base_url="http://w14-e-opinion"
        ) as client:
            opinion = await _approve_opinion(
                client,
                investigation_id=first.investigation.id,
                blocks=blocks,
                unresolved=("The Retrohunt hit requires separate human membership review.",),
            )
        async with uow_factory() as uow:
            persisted_opinion = await uow.analyst_opinions.get(UUID(opinion["id"]))
        assert persisted_opinion is not None and persisted_opinion.status.value == "approved"
        assert "not confirmed membership" in persisted_opinion.blocks[0].text
