"""PostgreSQL proofs for infrastructure review, rule lifecycles, and safe resume."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_rule_archive import EditionRuleArchiveService
from cti_app.application.investigation_rule_tools import RuleCorpusBytesUnavailable
from cti_app.domain.analyst_opinion import (
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.infrastructure.virustotal import VirusTotalQuotaExceededError
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner
from tests.integration.test_edition_multibatch_publication_postgres import _compose_three_runs
from tests.shodan_support import FakeShodanPort
from tests.test_investigation_vt_tools import _page
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    approve_opinion_http,
    article_document_v5,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    decision,
    investigation_settings,
    publish_opinion_http,
    read_composition,
    read_manifest,
    revise_opinion_http,
    run_http_job,
    seed_investigation_for_existing_article,
    seed_verified_article,
    start_investigation_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


class _ControlledRuleCorpus:
    """A bounded corpus backed only by inert per-test fixture bytes."""

    def __init__(self, samples: dict[str, bytes], goodware: tuple[str, ...]) -> None:
        self.samples = dict(samples)
        self.goodware = goodware

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        try:
            payload = self.samples[sample_sha256]
        except KeyError as exc:
            raise RuleCorpusBytesUnavailable("synthetic corpus item is unavailable") from exc
        assert hashlib.sha256(payload).hexdigest() == sample_sha256
        return payload

    async def goodware_sample_sha256s(self, investigation_id: UUID) -> tuple[str, ...]:
        del investigation_id
        return self.goodware

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> tuple[str, ...]:
        del investigation_id
        return ()


def _call(
    name: str,
    arguments: dict[str, Any],
    *,
    purpose: str | None = None,
) -> dict[str, Any]:
    return {
        "tool_name": name,
        "schema_version": "1",
        "arguments": arguments,
        "purpose": purpose or f"Run the bounded synthetic {name} operation.",
    }


async def _start_and_approve(
    driver: WorkflowDriver,
    client: httpx.AsyncClient,
    *,
    seeded,
    providers: tuple[str, ...],
    budget_limits: dict[str, int],
    acquire: bool,
    max_volume: int = 1,
    do_not_submit: bool = False,
) -> dict[str, Any]:
    started = await driver._perform(
        "start_investigation_http",
        lambda: start_investigation_http(client, seeded),
        via="http",
        action_name="start the verified article investigation through the API",
        identities={"subject": seeded.subject.id, "synthesis": seeded.synthesis.id},
        assertions=("verified-synthesis-identity-checked",),
    )
    investigation_id = seeded.investigation.id
    envelope = await driver._perform(
        "approve_g2_http",
        lambda: approve_envelope_http(
            client,
            investigation_id,
            providers=providers,
            budget_limits=budget_limits,
            allow_acquisition=acquire,
            max_acquire_volume=max_volume,
            do_not_submit=do_not_submit,
        ),
        via="http",
        action_name="approve the scoped G2 investigation envelope through the API",
        identities={"investigation": investigation_id},
        assertions=("g2-is-human-and-versioned",),
    )
    assert envelope.status_code == 200, envelope.text
    assert envelope.json()["approved"] is True
    return started


@pytest.mark.asyncio
async def test_w13_d4_ip_only_network_review_and_local_capability_fallback(
    workflow_driver: WorkflowDriver,
    uow_factory,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W13-D4-ip-only-network-review")
    async with driver:
        material = SyntheticSampleMaterial.unique()
        seeded = await driver._perform(
            "seed_ip_only_article",
            lambda: seed_verified_article(
                uow_factory,
                driver.artifact_store,
                suffix=f"w13-d4-{uuid4().hex[:8]}",
                material=material,
                include_seed=False,
                article_claim=(
                    f"C1: network service activity at {material.ip} is associated "
                    "with this incident."
                ),
            ),
            via="fixture",
            action_name="seed a verified IOC-only article with no sample indicator",
            assertions=("article-has-ip-only", "no-sample-seeded"),
        )
        assert seeded.seed_observation is None
        ip_bytes = f"Article A records IOC {material.ip}; synthetic W13 evidence {uuid4()}".encode()
        ip_blob_id = await driver.artifact_store.put_bytes(
            ip_bytes,
            bucket="workflow-ip-article-evidence",
            mime_type="text/plain",
        )
        ip_observation = EvidenceObservation(
            investigation_id=seeded.investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=ip_blob_id,
            sha256=hashlib.sha256(ip_bytes).hexdigest(),
            source="synthetic-article-ip",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="ip_address",
            object_id=material.ip,
            limitations=("Synthetic article-linked IOC fixture.",),
        )
        async with uow_factory() as uow:
            await uow.evidence_observations.append_if_absent(ip_observation)
            await uow.commit()

        vt = HashCheckedVirusTotal(
            material=material,
            include_candidate=False,
            relationships=[_page()],
        )
        shodan = FakeShodanPort()

        async def findings(observations):
            refs = tuple(
                item.id
                for item in observations
                if item.kind in {EvidenceKind.SHODAN_OBSERVATION, EvidenceKind.VT_RELATIONSHIP}
            )
            assert len(refs) >= 2
            return decision(
                "The dated passive network observations qualify C1 without identifying an actor.",
                finding_proposals=[
                    {
                        "kind": FindingKind.CLAIM_VERIFICATION.value,
                        "assessment": FindingAssessment.QUALIFIED.value,
                        "statement": "C1 is supported only as a dated observation of indexed "
                        "infrastructure.",
                        "observation_refs": [str(ref) for ref in refs],
                        "claim_refs": ["L1.1"],
                        "novelty": None,
                        "scope": "Passive VT relationship and Shodan host observations for the "
                        "article IOC.",
                        "limitations": [
                            "A shared certificate does not establish infrastructure attribution.",
                            "The observation dates do not establish incident-time activity.",
                        ],
                    }
                ],
                next_action="finish",
            )

        transport = ScriptedInvestigationTransport(
            [
                decision(
                    "Check VT's IP relationships for the article IOC.",
                    tool_call=_call(
                        "vt.relationships",
                        {
                            "object_type": "ip_address",
                            "object_id": material.ip,
                            "relation": "communicating_files",
                        },
                    ),
                ),
                decision(
                    "Read the passive host index for the same article IOC.",
                    tool_call=_call("shodan.host", {"ip": material.ip}),
                ),
            ],
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
            final_response_builder=findings,
        )
        settings = investigation_settings(providers=True, relationships=True)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=transport,
            virustotal=vt,
            shodan=shodan,
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w13-d4"
        ) as client:
            started = await _start_and_approve(
                driver,
                client,
                seeded=seeded,
                providers=("vt", "shodan"),
                budget_limits={
                    "bridge_turns": 12,
                    "vt_units": 12,
                    "shodan_credits": 2,
                    "downloads": 0,
                },
                acquire=False,
            )
            await driver._perform(
                "run_d4_network_job",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name="execute real investigation, VT relationship, and Shodan tools",
                identities={"investigation": seeded.investigation.id},
                assertions=("ip-only-no-download", "provider-fakes-no-network"),
            )
            async with uow_factory() as uow:
                observations = await uow.evidence_observations.list_for_investigation(
                    seeded.investigation.id
                )
                findings_rows = await uow.investigation_findings.list_for_investigation(
                    seeded.investigation.id
                )
                invocations = await uow.tool_invocations.list_for_investigation(
                    seeded.investigation.id
                )
            network_observations = [
                item for item in observations if item.source.casefold() in {"shodan", "virustotal"}
            ]
            shodan_observation = next(
                item for item in observations if item.kind is EvidenceKind.SHODAN_OBSERVATION
            )
            limitations = " ".join(shodan_observation.limitations).casefold()
            assert "partagés" in limitations and "attribution" in limitations
            assert "date" in limitations or "observed" in limitations or "période" in limitations
            assert any(item.kind is EvidenceKind.VT_RELATIONSHIP for item in network_observations)
            assert any(item.kind is FindingKind.CLAIM_VERIFICATION for item in findings_rows)
            assert {item.tool_name for item in invocations} == {"vt.relationships", "shodan.host"}
            assert vt.download_calls == 0
            assert not any(item.tool_name.startswith("rule.") for item in invocations)

            draft_response = await driver._perform(
                "compose_d4_opinion",
                lambda: compose_opinion_http(client, seeded.investigation.id),
                via="http",
                action_name="compose a dated network opinion for the IOC-only article",
                identities={"investigation": seeded.investigation.id},
                assertions=("dated-limits-flow-into-opinion",),
            )
            assert draft_response.status_code == 200, draft_response.text
            draft = draft_response.json()["draft"]
            claim = next(
                item for item in findings_rows if item.kind is FindingKind.CLAIM_VERIFICATION
            )
            cited_refs = tuple(claim.observation_refs)
            revised = await revise_opinion_http(
                client,
                UUID(draft["id"]),
                expected_version=draft["version"],
                blocks=(
                    {
                        "block_type": OpinionBlockType.NETWORK_AND_INFRASTRUCTURE.value,
                        "text": (
                            f"Shodan a observé des services sur {material.ip} le 11-12 octobre "
                            "2026. Le certificat partagé ne prouve aucune attribution et cette "
                            "fenêtre ne permet pas de dater l'activité pendant l'incident."
                        ),
                        "finding_refs": [str(claim.id)],
                        "evidence_refs": [str(ref) for ref in cited_refs],
                        "qualification": OpinionClaimQualification.INFERENCE.value,
                        "certainty": 0.62,
                    },
                ),
                unresolved_points=(
                    "L'attribution et l'activité à la date de l'incident restent inconnues.",
                ),
            )
            assert revised.status_code == 200, revised.text
            revised_opinion = revised.json()["draft"]
            approved = await approve_opinion_http(
                client,
                UUID(revised_opinion["id"]),
                version=revised_opinion["version"],
                sha256=revised_opinion["content_sha256"],
            )
            assert approved.status_code == 200, approved.text
            # Approval appends a new immutable approved version (status/reviewer are
            # part of its canonical hash); publication targets that exact version.
            published = await publish_opinion_http(
                client,
                UUID(revised_opinion["id"]),
                version=approved.json()["approved"]["version"],
                base_publication_sha256=revised_opinion["provenance"]["base_publication_sha256"],
            )
            assert published.status_code == 200, published.text

            # A second article has VT unavailable, but a configured Shodan path still
            # produces evidence from which the analyst can compose an opinion.
            fallback_material = SyntheticSampleMaterial.unique()
            fallback = await seed_verified_article(
                uow_factory,
                driver.artifact_store,
                suffix=f"w13-d4-fallback-{uuid4().hex[:8]}",
                material=fallback_material,
                include_seed=False,
                article_claim=(
                    f"C1: indexed network infrastructure is reported at {fallback_material.ip}."
                ),
            )
            fallback_blob = await driver.artifact_store.put_bytes(
                f"Synthetic IOC record {fallback_material.ip} {uuid4()}".encode(),
                bucket="workflow-ip-article-evidence",
                mime_type="text/plain",
            )
            fallback_observation = EvidenceObservation(
                investigation_id=fallback.investigation.id,
                kind=EvidenceKind.OTHER,
                blob_id=fallback_blob,
                sha256=hashlib.sha256(
                    await driver.artifact_store.read_bytes(fallback_blob)
                ).hexdigest(),
                source="synthetic-article-ip",
                source_version="1",
                observed_at=datetime.now(UTC),
                object_type="ip_address",
                object_id=fallback_material.ip,
                limitations=("Synthetic article-linked IOC fixture.",),
            )
            async with uow_factory() as uow:
                await uow.evidence_observations.append_if_absent(fallback_observation)
                await uow.commit()
            fallback_shodan = FakeShodanPort()
            fallback_transport = ScriptedInvestigationTransport(
                [
                    decision(
                        "The optional VT capability is absent; continue with the independent "
                        "Shodan path.",
                        tool_call=_call(
                            "vt.file_report", {"file_hash": fallback_material.seed_hash}
                        ),
                    ),
                    decision(
                        "Read the dated Shodan host record.",
                        tool_call=_call("shodan.host", {"ip": fallback_material.ip}),
                    ),
                ],
                uow_factory=uow_factory,
                investigation_id=fallback.investigation.id,
                material=fallback_material,
                inject_analysis_at_turn=None,
                final_response_builder=findings,
            )
            # Start with a deliberately VT-disabled capability set, but retain the
            # local Shodan fake and its configured key.
            no_vt_settings = settings.model_copy(
                update={
                    "virustotal_file_report_enabled": False,
                    "virustotal_object_relationships_enabled": False,
                    "virustotal_intelligence_search_enabled": False,
                    "virustotal_file_download_enabled": False,
                }
            )
            fallback_factory = composition_factory(
                uow_factory=uow_factory,
                artifact_store=driver.artifact_store,
                settings=no_vt_settings,
                transport=fallback_transport,
                virustotal=None,
                shodan=fallback_shodan,
            )
            fallback_app, fallback_registry, fallback_dispatcher = build_investigation_app(
                uow_factory=uow_factory,
                artifact_store=driver.artifact_store,
                composition_factory=fallback_factory,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=fallback_app), base_url="http://w13-d4-fallback"
            ) as fallback_client:
                fallback_started = await _start_and_approve(
                    driver,
                    fallback_client,
                    seeded=fallback,
                    # VT is not configured in this variant: G2 cannot approve an unavailable
                    # provider (provider_unavailable), so only Shodan is granted.
                    providers=("shodan",),
                    budget_limits={
                        "bridge_turns": 12,
                        "vt_units": 4,
                        "shodan_credits": 2,
                        "downloads": 0,
                    },
                    acquire=False,
                )
                await run_http_job(
                    fallback_app,
                    uow_factory,
                    fallback_registry,
                    fallback_dispatcher,
                    UUID(fallback_started["job_id"]),
                )
                fallback_draft = await compose_opinion_http(
                    fallback_client, fallback.investigation.id
                )
                assert fallback_draft.status_code == 200, fallback_draft.text
                async with uow_factory() as uow:
                    fallback_invocations = await uow.tool_invocations.list_for_investigation(
                        fallback.investigation.id
                    )
                assert any(
                    item.tool_name == "vt.file_report"
                    and item.status is ToolInvocationStatus.UNAVAILABLE
                    for item in fallback_invocations
                )
                assert any(
                    item.tool_name == "shodan.host"
                    and item.status is ToolInvocationStatus.SUCCEEDED
                    for item in fallback_invocations
                )


@pytest.mark.asyncio
async def test_w13_d5_rule_v1_v2_review_and_manifest_bound_archive(
    workflow_driver: WorkflowDriver,
    uow_factory,
    tmp_path: Path,
) -> None:
    driver = workflow_driver
    driver.begin("W13-D5-rule-revision-archive")
    async with driver:
        edition, artifact_store, subjects, batches = await driver._perform(
            "seed_independent_articles",
            lambda: _compose_three_runs(
                uow_factory,
                tmp_path,
                production_order=("A", "B", "C"),
                composition_order=(),
            ),
            via="fixture",
            action_name="seed independent A/B/C publication runs for rule archive proof",
            assertions=("three-distinct-runs", "three-distinct-production-batches"),
        )
        driver.edition_id = edition.id
        driver.set_artifact_store(artifact_store)

        async def add_v5_and_extractions():
            runs: dict[str, Any] = {}
            publications: dict[str, ProductionArtifact] = {}
            extraction_ids: dict[str, UUID] = {}
            for label in ("A", "B", "C"):
                async with uow_factory() as uow:
                    subject_runs = await uow.production_runs.list_for_subject(subjects[label].id)
                assert len(subject_runs) == 1
                run = subject_runs[0]
                runs[label] = run
                extraction_blob, extraction_sha = await artifact_store.put_canonical_json(
                    {"items": [], "uncertainties": [], "rules": []},
                    bucket="w13-rule-extractions",
                )
                extraction = ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subjects[label].id,
                    stage=ProductionArtifactStage.EXTRACTION,
                    version=1,
                    input_hash=extraction_sha,
                    canonical_blob_id=extraction_blob,
                )
                extraction_ids[label] = extraction.id
                doc = article_document_v5(subjects[label].id, label, version="W13")
                # Keep the typed serializer used by the actual publication surface.
                from cti_app.domain.publication_document import serialize_publication_document

                payload = serialize_publication_document(doc)
                blob_id, doc_sha = await artifact_store.put_canonical_json(
                    payload, bucket="w13-v5-publications"
                )
                publication = ProductionArtifact(
                    production_run_id=run.id,
                    subject_id=subjects[label].id,
                    stage=ProductionArtifactStage.PUBLICATION,
                    version=2,
                    input_hash=doc_sha,
                    status=ProductionArtifactStatus.VERIFIED,
                    canonical_blob_id=blob_id,
                    metadata={"input_artifacts": {"extraction_artifact_id": str(extraction.id)}},
                )
                async with uow_factory() as uow:
                    await uow.production_artifacts.append(extraction)
                    await uow.production_artifacts.append(publication)
                    await uow.commit()
                publications[label] = publication
            return runs, publications, extraction_ids

        runs, publications, _extraction_ids = await driver._perform(
            "seed_verified_v5_and_extractions",
            add_v5_and_extractions,
            via="fixture",
            action_name="seed V5 articles with manifest-resolvable verified extraction artifacts",
            assertions=("a-v5-extraction-pin-is-canonical", "b-and-c-are-independent"),
        )
        await driver.compose(
            tuple(subjects[label].id for label in ("A", "B", "C")),
            provenance_batch_ids={subjects[label].id: batches[label] for label in ("A", "B", "C")},
        )
        await driver.review(expect_can_accept=True)
        await driver.validate()
        await driver.accept()
        await driver.assemble()

        token = uuid4().hex
        material = SyntheticSampleMaterial.unique()
        marker_v1 = f"AlphaFamilyMarker{token[:10]}"
        marker_v2 = f"BetaVariantMarker{token[10:20]}"
        first_bytes = b"MZ\x00W13-rule-v1:" + marker_v1.encode() + b":synthetic-inert"
        second_bytes = b"MZ\x00W13-rule-v2:" + marker_v2.encode() + b":synthetic-inert"
        goodware_bytes = b"synthetic-goodware:" + marker_v1.encode() + b":not-malware"
        first_sha = hashlib.sha256(first_bytes).hexdigest()
        second_sha = hashlib.sha256(second_bytes).hexdigest()
        goodware_sha = hashlib.sha256(goodware_bytes).hexdigest()
        material = replace(material, sample_bytes=first_bytes, sample_hash=first_sha)
        candidate_id = uuid4()
        vt = HashCheckedVirusTotal(
            material=material,
            extra_samples={second_sha: second_bytes},
        )
        corpus = _ControlledRuleCorpus(
            {first_sha: first_bytes, second_sha: second_bytes, goodware_sha: goodware_bytes},
            (goodware_sha,),
        )
        seeded = await seed_investigation_for_existing_article(
            uow_factory,
            artifact_store,
            edition=edition,
            subject=subjects["A"],
            run=runs["A"],
            publication=publications["A"],
            publication_bytes=await artifact_store.read_bytes(publications["A"].canonical_blob_id),
            material=material,
            article_claim=f"C1: article A describes the verified seed {material.seed_hash}.",
        )

        async def dynamic_rule_turn(observations):
            async with uow_factory() as uow:
                associations = tuple(
                    await uow.sample_associations.list_for_investigation(seeded.investigation.id)
                )
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                versions = tuple(await uow.detection_candidates.list_for_candidate(candidate_id))
            analyzed_ids = {
                str(item.normalized_parameters.get("association_id"))
                for item in invocations
                if item.tool_name == "sample.analyze"
            }
            analyzed_hashes = {
                association.sample_sha256
                for association in associations
                if str(association.id) in analyzed_ids
            }
            if not any(item.tool_name == "vt.intelligence_search" for item in invocations):
                return decision(
                    "Discover related candidates with the controlled VT index.",
                    tool_call=_call(
                        "vt.intelligence_search",
                        {
                            "query": f'content:"{marker_v1}"',
                            "justification": "Find inert fixture candidates for a rule lifecycle "
                            "proof.",
                            "evidence_refs": [str(seeded.seed_observation.id)],
                        },
                    ),
                )
            if first_sha not in {item.sample_sha256 for item in associations}:
                return decision(
                    "Acquire the first locally analyzable candidate.",
                    tool_call=_call(
                        "vt.download_sample",
                        {
                            "sha256": first_sha,
                            "purpose": "Quarantine the first inert controlled rule sample.",
                            "evidence_refs": [str(seeded.seed_observation.id)],
                        },
                    ),
                )
            first_ref = None
            second_ref = None
            for observation in observations:
                if observation.kind is not EvidenceKind.BINARY_STRING:
                    continue
                payload = json.dumps(
                    await artifact_store.read_json(observation.blob_id), ensure_ascii=True
                )
                if observation.object_id == first_sha and marker_v1 in payload:
                    first_ref = observation.id
                if observation.object_id == second_sha and marker_v2 in payload:
                    second_ref = observation.id
            if not versions:
                assert first_sha in analyzed_hashes and first_ref is not None
                return decision(
                    "Propose an intentionally noisy v1 motif measured from local strings.",
                    tool_call=_call(
                        "rule.propose",
                        {
                            "candidate_id": str(candidate_id),
                            "condition": "any",
                            "motifs": [
                                {
                                    "kind": "string",
                                    "value": marker_v1,
                                    "provenance_kind": "p09_invariant",
                                    "provenance_id": str(first_ref),
                                    "observation_refs": [str(first_ref)],
                                }
                            ],
                        },
                    ),
                )
            if not any(
                item.tool_name == "rule.test" and item.normalized_parameters.get("version") == 1
                for item in invocations
            ):
                return decision(
                    "Retest v1 on the controlled positive and goodware corpus.",
                    tool_call=_call("rule.test", {"candidate_id": str(candidate_id), "version": 1}),
                )
            if len(versions) == 1 and second_sha not in {
                item.sample_sha256 for item in associations
            }:
                return decision(
                    "Acquire the new independent sample for revision testing.",
                    tool_call=_call(
                        "vt.download_sample",
                        {
                            "sha256": second_sha,
                            "purpose": "Associate a new inert sample before revising v1.",
                            "evidence_refs": [str(seeded.seed_observation.id)],
                        },
                    ),
                )
            if len(versions) == 1:
                assert second_sha in analyzed_hashes and second_ref is not None
                return decision(
                    "Revise noisy v1 into a distinct v2 using the newly analyzed sample.",
                    tool_call=_call(
                        "rule.revise",
                        {
                            "candidate_id": str(candidate_id),
                            "parent_version": 1,
                            "condition": "any",
                            "motifs": [
                                {
                                    "kind": "string",
                                    "value": marker_v2,
                                    "provenance_kind": "p09_invariant",
                                    "provenance_id": str(second_ref),
                                    "observation_refs": [str(second_ref)],
                                }
                            ],
                        },
                    ),
                )
            if not any(
                item.tool_name == "rule.test" and item.normalized_parameters.get("version") == 2
                for item in invocations
            ):
                return decision(
                    "Retest v2 against both associated samples and synthetic goodware.",
                    tool_call=_call("rule.test", {"candidate_id": str(candidate_id), "version": 2}),
                )
            if not any(item.tool_name == "rule.compare" for item in invocations):
                return decision(
                    "Compare the immutable v1 and v2 match sets.",
                    tool_call=_call(
                        "rule.compare",
                        {"candidate_id": str(candidate_id), "version_a": 1, "version_b": 2},
                    ),
                )
            analysis_refs = tuple(
                item.id
                for item in observations
                if item.kind is EvidenceKind.BINARY_ANALYSIS and item.object_id == first_sha
            )
            return decision(
                "Keep the article claim qualified by the local analysis evidence.",
                finding_proposals=[
                    {
                        "kind": FindingKind.CLAIM_VERIFICATION.value,
                        "assessment": FindingAssessment.QUALIFIED.value,
                        "statement": "C1 is supported by the verified article seed and qualified "
                        "by local static review.",
                        "observation_refs": [str(ref) for ref in analysis_refs],
                        "claim_refs": ["L1.1"],
                        "novelty": None,
                        "scope": "Local static observations for the article seed only.",
                        "limitations": ["Static analysis does not establish runtime behavior."],
                    }
                ],
                next_action="finish",
            )

        transport = ScriptedInvestigationTransport(
            [],
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
            analyze_pending_samples=True,
            final_response_builder=dynamic_rule_turn,
        )
        settings = investigation_settings(
            providers=True, local_analysis=True, rules=True
        ).model_copy(update={"investigation_max_hits_acquired": 2})
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            settings=settings,
            transport=transport,
            virustotal=vt,
            include_local_analysis=True,
            rule_corpus_bytes=corpus,
            yara_engine=IsolatedYaraRunner(),
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            composition_factory=factory,
            include_detection=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w13-d5"
        ) as client:
            started = await _start_and_approve(
                driver,
                client,
                seeded=seeded,
                providers=("vt",),
                budget_limits={
                    "bridge_turns": 24,
                    "vt_units": 24,
                    "shodan_credits": 0,
                    "downloads": 2,
                },
                acquire=True,
                max_volume=2,
            )
            await driver._perform(
                "run_d5_v1_rule_job",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name=(
                    "run real investigation, local analysis, isolated YARA v1/v2, and VT fake"
                ),
                identities={"investigation": seeded.investigation.id},
                assertions=("real-postgres-rule-tools", "isolated-yara-process", "no-network"),
            )
            async with uow_factory() as uow:
                associations = tuple(
                    await uow.sample_associations.list_for_investigation(seeded.investigation.id)
                )
                versions = tuple(await uow.detection_candidates.list_for_candidate(candidate_id))
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                findings_rows = await uow.investigation_findings.list_for_investigation(
                    seeded.investigation.id
                )
            assert first_sha in {item.sample_sha256 for item in associations}
            assert second_sha in {item.sample_sha256 for item in associations}
            # The dynamic planner completes v1 and v2 in this job. rule.test adds
            # report observations; it does not append a candidate version.
            assert len(versions) == 2
            assert [item.version for item in versions] == [1, 2]
            assert versions[0].source_sha256 != versions[1].source_sha256
            assert versions[0].status.value == "rejected"
            assert any(
                item.tool_name == "rule.propose" and item.status is ToolInvocationStatus.SUCCEEDED
                for item in invocations
            )
            assert any(
                item.tool_name == "rule.test" and item.status is ToolInvocationStatus.SUCCEEDED
                for item in invocations
            )
            successful_tests = {
                item.normalized_parameters.get("version")
                for item in invocations
                if item.tool_name == "rule.test" and item.status is ToolInvocationStatus.SUCCEEDED
            }
            assert {1, 2} <= successful_tests
            assert {item.tool_name for item in invocations} >= {
                "rule.propose",
                "rule.test",
                "rule.revise",
                "rule.compare",
                "sample.analyze",
            }
            comparison_invocation = next(
                item for item in invocations if item.tool_name == "rule.compare"
            )
            assert comparison_invocation.response_refs
            comparison_observation = next(
                item
                for item in await _list_observations(uow_factory, seeded.investigation.id)
                if item.id == comparison_invocation.response_refs[0]
            )
            comparison_payload = await artifact_store.read_json(comparison_observation.blob_id)
            role_deltas = comparison_payload.get("roles", [])
            assert any(
                role.get("matches_gained") or role.get("matches_lost")
                for role in role_deltas
                if isinstance(role, dict)
            )

            detection_list = await client.get(
                f"/api/investigations/{seeded.investigation.id}/detections"
            )
            assert detection_list.status_code == 200, detection_list.text
            v1_view = next(
                item
                for item in detection_list.json()["versions"]
                if item["version_id"] == str(versions[0].id)
            )
            assert any(
                goodware_sha in report["known_false_positives"]
                for report in v1_view["test_reports"]
            )
            assert v1_view["status"] == "rejected"

            opinion_draft_response = await compose_opinion_http(client, seeded.investigation.id)
            assert opinion_draft_response.status_code == 200, opinion_draft_response.text
            opinion_draft = opinion_draft_response.json()["draft"]
            claim = next(
                item for item in findings_rows if item.kind is FindingKind.CLAIM_VERIFICATION
            )
            claim_block = {
                "block_type": OpinionBlockType.CLAIM_VERIFICATION.value,
                "text": "C1 reste qualifiée après l'analyse statique locale de l'échantillon S1.",
                "finding_refs": [str(claim.id)],
                "evidence_refs": [str(ref) for ref in claim.observation_refs],
                "qualification": OpinionClaimQualification.INFERENCE.value,
                "certainty": 0.67,
            }
            revised_opinion = await revise_opinion_http(
                client,
                UUID(opinion_draft["id"]),
                expected_version=opinion_draft["version"],
                blocks=(claim_block,),
                unresolved_points=("Le comportement d'exécution reste inconnu.",),
            )
            assert revised_opinion.status_code == 200, revised_opinion.text
            opinion = revised_opinion.json()["draft"]
            approved_opinion = await approve_opinion_http(
                client,
                UUID(opinion["id"]),
                version=opinion["version"],
                sha256=opinion["content_sha256"],
            )
            assert approved_opinion.status_code == 200, approved_opinion.text
            published = await publish_opinion_http(
                client,
                UUID(opinion["id"]),
                version=approved_opinion.json()["approved"]["version"],
                base_publication_sha256=opinion["provenance"]["base_publication_sha256"],
            )
            assert published.status_code == 200, published.text
            v6_id = UUID(published.json()["publication"]["artifact_id"])
            async with uow_factory() as uow:
                v6 = await uow.production_artifacts.get(v6_id)
                assert v6 is not None and v6.canonical_blob_id is not None
                v6_blob = await uow.blobs.get(v6.canonical_blob_id)
            assert v6_blob is not None

            initial_manifest = await read_manifest(uow_factory, edition.id)
            archive_service = EditionRuleArchiveService(uow_factory, artifact_store)
            before_review = await archive_service.build(edition.id)
            assert before_review.rule_count == 0
            assert initial_manifest is not None

            async def repin_opinion_v6():
                service = EditionCompositionService(uow_factory)
                current = await service.get(edition.id)
                assert current.composition is not None
                entries = tuple(
                    replace(
                        entry,
                        artifact_id=v6.id,
                        artifact_version=v6.version,
                        artifact_input_hash=v6.input_hash,
                        document_sha256=v6_blob.descriptor.sha256,
                    )
                    if entry.subject_id == subjects["A"].id
                    else entry
                    for entry in current.composition.entries
                )
                return await service.replace_entries(
                    edition.id,
                    entries,
                    expected_version=current.composition.version,
                    idempotency_key=f"w13-v6-repin-{uuid4()}",
                    actor_id="human:w13-editor",
                    correlation_id=f"w13-v6-repin:{uuid4()}",
                )

            await driver._perform(
                "human_repin_v6_for_opinion",
                repin_opinion_v6,
                via="service",
                action_name="human CAS repin the published opinion before rule approval",
                identities={"edition": edition.id, "publication": v6.id},
                assertions=("opinion-is-published-before-rule-review",),
            )
            await driver.review(expect_can_accept=True)
            await driver.validate()
            manifest_with_opinion = (await driver.accept()).manifest
            await driver.assemble()
            assert manifest_with_opinion.id != initial_manifest.id
            # The noisy v1 cannot enter an archive even while the article and opinion
            # are published. Approval targets the exact tested v2 source hash.
            still_empty = await archive_service.build(edition.id)
            assert still_empty.rule_count == 0
            v2 = versions[1]
            approval = await client.post(
                f"/api/detections/{v2.id}/approve",
                json={
                    "expected_source_sha256": v2.source_sha256,
                    "reason": (
                        "Reviewed the exact v2 source, test report, and changed corpus matches."
                    ),
                    "known_limitations": "Synthetic corpus only; no runtime or holdout coverage.",
                },
                headers={"Idempotency-Key": f"w13-approve-v2-{v2.id}"},
            )
            assert approval.status_code == 200, approval.text
            assert approval.json()["status"] == "approved"
            assert approval.json()["decision"]["actor"] == "editor:workflow-emulator"
            historical_archive = await archive_service.build(edition.id)
            assert historical_archive.content == still_empty.content

            current = await read_composition(uow_factory, edition.id)
            assert current is not None
            reordered = tuple(
                replace(
                    entry,
                    position=3
                    if entry.position == 2
                    else 2
                    if entry.position == 3
                    else entry.position,
                )
                for entry in current.entries
            )
            changed = await EditionCompositionService(uow_factory).replace_entries(
                edition.id,
                reordered,
                expected_version=current.version,
                idempotency_key=f"w13-new-manifest-after-approval-{uuid4()}",
                actor_id="human:w13-editor",
                correlation_id=f"w13-new-manifest-after-approval:{uuid4()}",
            )
            assert changed.version == current.version + 1
            await driver.review(expect_can_accept=True)
            await driver.validate()
            manifest_after_approval = (await driver.accept()).manifest
            await driver.assemble()
            assert manifest_after_approval.id != manifest_with_opinion.id
            final_archive = await archive_service.build(edition.id)
            assert final_archive.rule_count == 1
            with zipfile.ZipFile(BytesIO(final_archive.content)) as bundle:
                names = bundle.namelist()
                analyst_names = [name for name in names if name.startswith("analyst/yara/")]
                assert len(analyst_names) == 1
                archived_source = bundle.read(analyst_names[0]).decode("utf-8")
                archive_manifest = json.loads(bundle.read("manifest.json"))
            assert marker_v2 in archived_source and marker_v1 not in archived_source
            assert len(archive_manifest["analyst_rules"]) == 1
            assert archive_manifest["analyst_rules"][0]["id"] == str(v2.id)
            assert archive_manifest["analyst_rules"][0]["article"]["artifact_id"] == str(v6.id)


async def _list_observations(uow_factory, investigation_id: UUID):
    async with uow_factory() as uow:
        return tuple(await uow.evidence_observations.list_for_investigation(investigation_id))


@pytest.mark.asyncio
async def test_w13_d7_tool_replay_ambiguous_bridge_resume_and_local_denials(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W13-D7-replay-resume-policy")
    async with driver:
        material = SyntheticSampleMaterial.unique()
        second_bytes = b"MZ\x00W13-independent-sample-" + uuid4().bytes + b"\x00inert"
        second_sha = hashlib.sha256(second_bytes).hexdigest()
        vt = HashCheckedVirusTotal(material=material, extra_samples={second_sha: second_bytes})
        shodan = FakeShodanPort()
        seeded = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w13-d7-{uuid4().hex[:8]}",
            material=material,
        )
        ambiguous = TurnSubmission(
            TurnSubmissionStatus.NEEDS_REVIEW, detail="simulated Bridge timeout"
        )
        duplicate_download_decision = decision(
            "Repeat the same acquisition operation in the same Bridge turn to prove idempotency.",
            next_action="continue",
        )
        download_call = _call(
            "vt.download_sample",
            {
                "sha256": material.sample_hash,
                "purpose": "Acquire the one approved inert sample for replay testing.",
                "evidence_refs": [str(seeded.seed_observation.id)],
            },
        )
        duplicate_download_decision["proposed_tool_calls"] = [download_call, dict(download_call)]
        second_download = decision(
            "The download budget is spent; still pursue the independent passive Shodan lead.",
            next_action="continue",
        )
        second_download["proposed_tool_calls"] = [
            _call(
                "vt.download_sample",
                {
                    "sha256": second_sha,
                    "purpose": "Attempt only the unbudgeted second sample acquisition.",
                    "evidence_refs": [str(seeded.seed_observation.id)],
                },
            ),
            _call("shodan.host", {"ip": material.ip}),
        ]
        transport = ScriptedInvestigationTransport(
            [
                decision(
                    "Read a deterministic VT search result containing two inert candidates.",
                    tool_call=_call(
                        "vt.intelligence_search",
                        {
                            "query": 'type:peexe content:"AUTOWORK-SYNTHETIC"',
                            "justification": "Prepare harmless candidates for budget and replay "
                            "checks.",
                            "evidence_refs": [str(seeded.seed_observation.id)],
                        },
                    ),
                ),
                duplicate_download_decision,
                second_download,
                ambiguous,
            ],
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            inject_analysis_at_turn=None,
        )
        settings = investigation_settings(providers=True)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=transport,
            virustotal=vt,
            shodan=shodan,
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w13-d7"
        ) as client:
            started = await _start_and_approve(
                driver,
                client,
                seeded=seeded,
                providers=("vt", "shodan"),
                budget_limits={
                    "bridge_turns": 12,
                    "vt_units": 12,
                    "shodan_credits": 2,
                    "downloads": 1,
                },
                acquire=True,
                max_volume=1,
            )
            await driver._perform(
                "run_d7_before_crash",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name="run search, duplicate download replay, budget denial, and Shodan lead",
                identities={"investigation": seeded.investigation.id},
                assertions=(
                    "one-provider-download-for-one-operation-key",
                    "unbudgeted-second-download-is-operation-local",
                    "independent-shodan-action-continues",
                ),
            )
            assert vt.download_calls == 1
            assert shodan.calls and shodan.calls[0][0] == "host"
            assert transport.submission_count == 4
            submissions_before_resume = transport.submission_count
            attempts_before_resume = transport.attempt_count
            state_response = await client.get(f"/api/investigations/{seeded.investigation.id}")
            assert state_response.status_code == 200, state_response.text
            current_version = state_response.json()["version"]
            resume = await client.post(
                f"/api/investigations/{seeded.investigation.id}/resume",
                json={"expected_version": current_version},
                headers={"Idempotency-Key": f"w13-resume-ambiguous-{seeded.investigation.id}"},
            )
            assert resume.status_code == 200, resume.text
            await driver._perform(
                "resume_without_resubmission",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(resume.json()["job_id"])
                ),
                via="service",
                action_name="resume after ambiguous Bridge turn without resubmitting it",
                identities={"investigation": seeded.investigation.id},
                assertions=("ambiguous-bridge-turn-is-still-needs-review", "no-second-submit"),
            )
            assert transport.submission_count == submissions_before_resume
            assert transport.attempt_count == attempts_before_resume + 1
            async with uow_factory() as uow:
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                samples = tuple(
                    await uow.sample_associations.list_for_investigation(seeded.investigation.id)
                )
            first_downloads = [
                item for item in invocations if item.tool_name == "vt.download_sample"
            ]
            assert len(first_downloads) == 2
            succeeded = [
                item for item in first_downloads if item.status is ToolInvocationStatus.SUCCEEDED
            ]
            denied = [
                item
                for item in first_downloads
                if item.status is not ToolInvocationStatus.SUCCEEDED
            ]
            assert len(succeeded) == 1 and len(denied) == 1
            assert denied[0].normalized_parameters.get("sha256") == second_sha
            assert (
                len([item for item in samples if item.sample_sha256 == material.sample_hash]) == 1
            )
            assert second_sha not in {item.sample_sha256 for item in samples}
            assert denied[0].status is ToolInvocationStatus.NEEDS_REVIEW
            assert any(
                item.tool_name == "shodan.host" and item.status is ToolInvocationStatus.SUCCEEDED
                for item in invocations
            )

        # A separate investigation without acquisition rights. VT read and Shodan remain
        # independent, while only the attempted acquisition is denied locally.
        policy_material = SyntheticSampleMaterial.unique()
        policy_seed = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w13-d7-policy-{uuid4().hex[:8]}",
            material=policy_material,
        )
        policy_vt = HashCheckedVirusTotal(material=policy_material)
        policy_shodan = FakeShodanPort()
        policy_transport = ScriptedInvestigationTransport(
            [
                decision(
                    "The VT read is independent from sample submission.",
                    tool_call=_call("vt.file_report", {"file_hash": policy_material.seed_hash}),
                ),
                decision(
                    "do_not_submit blocks the selected acquisition, but the Shodan path remains "
                    "available.",
                    next_action="continue",
                ),
                decision(
                    "Finish after local policy and provider outcomes have been recorded.",
                    next_action="finish",
                ),
            ],
            uow_factory=uow_factory,
            investigation_id=policy_seed.investigation.id,
            material=policy_material,
            inject_analysis_at_turn=None,
        )
        policy_download = _call(
            "vt.download_sample",
            {
                "sha256": policy_material.sample_hash,
                "purpose": "Attempt only the forbidden external sample submission.",
                "evidence_refs": [str(policy_seed.seed_observation.id)],
            },
        )
        policy_transport._responses[1] = decision(
            "The acquisition is policy-denied; still query passive infrastructure.",
            next_action="continue",
        )
        policy_transport._responses[1]["proposed_tool_calls"] = [
            policy_download,
            _call("shodan.host", {"ip": policy_material.ip}),
        ]
        policy_factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=policy_transport,
            virustotal=policy_vt,
            shodan=policy_shodan,
        )
        policy_app, policy_registry, policy_dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=policy_factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=policy_app), base_url="http://w13-d7-policy"
        ) as policy_client:
            policy_started = await _start_and_approve(
                driver,
                policy_client,
                seeded=policy_seed,
                providers=("vt", "shodan"),
                budget_limits={
                    "bridge_turns": 12,
                    "vt_units": 12,
                    "shodan_credits": 2,
                    "downloads": 1,
                },
                # The envelope grants passive reads but **not** sample acquisition: only the
                # attempted download is denied locally while VT/Shodan reads continue.
                # (do_not_submit instead withholds the whole context from the Bridge and
                # yields model_unavailable; that gate is covered by the loop unit tests.)
                acquire=False,
                max_volume=0,
            )
            await run_http_job(
                policy_app,
                uow_factory,
                policy_registry,
                policy_dispatcher,
                UUID(policy_started["job_id"]),
            )
            async with uow_factory() as uow:
                policy_invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(policy_seed.investigation.id)
                )
                policy_samples = tuple(
                    await uow.sample_associations.list_for_investigation(
                        policy_seed.investigation.id
                    )
                )
            assert policy_vt.download_calls == 0
            assert not any(
                item.sample_sha256 == policy_material.sample_hash for item in policy_samples
            )
            assert any(
                item.tool_name == "vt.download_sample"
                and item.status
                in {ToolInvocationStatus.POLICY_DENIED, ToolInvocationStatus.NEEDS_REVIEW}
                for item in policy_invocations
            )
            assert any(
                item.tool_name == "shodan.host" and item.status is ToolInvocationStatus.SUCCEEDED
                for item in policy_invocations
            )

        # Quota is a local unavailable result; the loop still executes its independent
        # Shodan branch, which keeps the investigation usable for opinion drafting.
        quota_material = SyntheticSampleMaterial.unique()
        quota_seed = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w13-d7-quota-{uuid4().hex[:8]}",
            material=quota_material,
        )
        quota_vt = HashCheckedVirusTotal(material=quota_material)
        quota_vt.errors["file_report"] = VirusTotalQuotaExceededError("simulated quota")
        quota_shodan = FakeShodanPort()
        quota_transport = ScriptedInvestigationTransport(
            [
                {
                    **decision("Try VT report and continue to the independent host lookup."),
                    "proposed_tool_calls": [
                        _call("vt.file_report", {"file_hash": quota_material.seed_hash}),
                        _call("shodan.host", {"ip": quota_material.ip}),
                    ],
                },
                decision("Finish after recording the local quota result.", next_action="finish"),
            ],
            uow_factory=uow_factory,
            investigation_id=quota_seed.investigation.id,
            material=quota_material,
            inject_analysis_at_turn=None,
        )
        quota_factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=quota_transport,
            virustotal=quota_vt,
            shodan=quota_shodan,
        )
        quota_app, quota_registry, quota_dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=quota_factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=quota_app), base_url="http://w13-d7-quota"
        ) as quota_client:
            quota_started = await _start_and_approve(
                driver,
                quota_client,
                seeded=quota_seed,
                providers=("vt", "shodan"),
                budget_limits={
                    "bridge_turns": 12,
                    "vt_units": 12,
                    "shodan_credits": 2,
                    "downloads": 0,
                },
                acquire=False,
            )
            await run_http_job(
                quota_app,
                uow_factory,
                quota_registry,
                quota_dispatcher,
                UUID(quota_started["job_id"]),
            )
            async with uow_factory() as uow:
                quota_invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(quota_seed.investigation.id)
                )
            assert any(
                item.tool_name == "vt.file_report"
                and item.status is ToolInvocationStatus.UNAVAILABLE
                for item in quota_invocations
            )
            assert any(
                item.tool_name == "shodan.host" and item.status is ToolInvocationStatus.SUCCEEDED
                for item in quota_invocations
            )
