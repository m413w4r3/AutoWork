from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_annex_export import write_investigation_annex
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Sample, SampleState, Subject
from cti_app.domain.entities import SampleOrigin as StoredSampleOrigin
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    MotifProvenance,
    SampleAssociation,
    SampleOrigin,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.investigation_annex_reader import (
    load_investigation_annex_snapshot,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


async def _seed_investigation(session_factory):
    suffix = uuid4().hex
    edition = Edition(
        country=f"Annex integration {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Synthetic annex export subject",
        slug=f"annex-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="d" * 64,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=synthesis.id,
        budget=LoopBudget(),
        created_at=NOW,
        updated_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation, subject


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_postgres_annex_export_reads_canonical_records_and_filters_restricted_samples(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    blob_store = FilesystemBlobStore(tmp_path / "annex-blobs")
    catalog = BlobCatalogService(blob_store, lambda: SqlAlchemyUnitOfWork(session_factory))
    artifact_store = ProductionArtifactStore(catalog)
    try:
        investigation, subject = await _seed_investigation(session_factory)

        # Canonical report and IOC observations are persisted as versioned evidence blobs.
        report_id = uuid4()
        report = {
            "schema": "investigation-yara-test.v1",
            "version_id": "pending",
            "candidate_id": "pending",
            "candidate_status": "tested",
            "final_status": "passed",
            "test_refs": ["synthetic-yara-fixture"],
            "source": "synthetic integration report",
        }
        vt_payload = {
            "schema": "virustotal-object-report.v1",
            "candidate": {"type": "domain", "id": "annex-example.invalid"},
            "descriptors": [],
        }
        vt_blob = await catalog.ingest(
            BytesIO(_json_bytes(vt_payload)),
            logical_bucket="annex-test",
            mime_type="application/json",
        )
        rule_body = (
            'rule annex_bridge_candidate {\n strings:\n  $marker = "'
            + "BRIDGE_SYNTHETIC_MARKER_"
            + "x" * 7000
            + '"\n condition:\n  $marker\n}\n'
        )
        bridge_source = "// author: bridge-fixture; origin: Bridge\n" + rule_body
        revised_source = bridge_source.replace("candidate", "candidate_revised")
        suricata_source = (
            'alert http any any -> any any (msg:"annex fixture"; content:"x"; sid:990001; rev:1;)\n'
        )
        advisor_payload = {
            "schema": "operator-advisor-call.v1",
            "output": {
                "rule_source": suricata_source,
                "rule_source_sha256": hashlib.sha256(suricata_source.encode()).hexdigest(),
                "author": "Bridge synthetic fixture",
                "canonicalization_eligible": False,
                "candidate_version": "draft-1",
            },
        }
        advisor_blob = await catalog.ingest(
            BytesIO(_json_bytes(advisor_payload)),
            logical_bucket="annex-test",
            mime_type="application/json",
        )
        syntax_blob = await catalog.ingest(
            BytesIO(
                _json_bytes(
                    {
                        "schema": "SuricataObservationV1",
                        "engine": {
                            "rule_sha256": hashlib.sha256(suricata_source.encode()).hexdigest(),
                            "status": "succeeded",
                        },
                    }
                )
            ),
            logical_bucket="annex-test",
            mime_type="application/json",
        )
        missing_blob = await catalog.ingest(
            BytesIO(_json_bytes({"schema": "synthetic-unavailable-object.v1"})),
            logical_bucket="annex-test",
            mime_type="application/json",
        )

        sample_bytes = b"SYNTHETIC_RESTRICTED_SAMPLE_BYTES_" + bytes(range(64))
        sample_record = await catalog.ingest(
            BytesIO(sample_bytes),
            logical_bucket="samples-quarantine",
            mime_type="application/octet-stream",
        )
        sample = Sample(
            subject_id=subject.id,
            blob_id=sample_record.id,
            original_name="quarantined-synthetic.bin",
            origin="synthetic integration acquisition",
            acquired_at=NOW,
            license_restriction=None,
            tlp=TLP.RED,
            do_not_submit=False,
            external_llm_allowed=True,
            origin_kind=StoredSampleOrigin.MANUAL,
            state=SampleState.QUARANTINED,
            created_at=NOW,
        )
        sample_sha = sample_record.descriptor.sha256
        unacquired_sha = hashlib.sha256(b"synthetic-not-acquired").hexdigest()
        bridge_invocation_id = uuid4()
        operator_invocation_id = uuid4()
        bridge_observation_id = uuid4()
        operator_observation_id = uuid4()
        candidate_id = uuid4()
        source_v1_id = uuid4()
        source_v1_payload = {
            "schema": "investigation-tool-observation.v1",
            "tool_name": "rule.propose_bridge",
            "result": {
                "candidate_id": str(candidate_id),
                "version_id": str(source_v1_id),
                "source_author": "bridge",
                "source_attribution": {
                    "author": "bridge",
                    "source_author": "bridge",
                    "origin_author": "bridge",
                    "change_kind": "bridge_yara_draft",
                },
            },
        }
        source_v1_blob = await catalog.ingest(
            BytesIO(_json_bytes(source_v1_payload)),
            logical_bucket="annex-test",
            mime_type="application/json",
        )
        source_v1_observation = EvidenceObservation(
            id=bridge_observation_id,
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=source_v1_blob.id,
            sha256=source_v1_blob.descriptor.sha256,
            source="rule.propose_bridge",
            source_version="rule.propose_bridge.v1",
            observed_at=NOW,
            object_type="tool_result",
            object_id="synthetic-bridge-proposal",
            invocation_ref=bridge_invocation_id,
        )
        source_v2_id = uuid4()
        operator_payload = {
            "schema": "investigation-tool-observation.v1",
            "tool_name": "rule.operator_syntax_fix",
            "result": {
                "candidate_id": str(candidate_id),
                "version_id": str(source_v2_id),
                "source_author": "operator",
                "source_attribution": {
                    "author": "operator",
                    "source_author": "operator",
                    "origin_author": "bridge",
                    "change_kind": "operator_syntax_fix",
                },
            },
        }
        operator_blob = await catalog.ingest(
            BytesIO(_json_bytes(operator_payload)),
            logical_bucket="annex-test",
            mime_type="application/json",
        )
        operator_observation = EvidenceObservation(
            id=operator_observation_id,
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=operator_blob.id,
            sha256=operator_blob.descriptor.sha256,
            source="rule.operator_syntax_fix",
            source_version="rule.operator_syntax_fix.v1",
            observed_at=NOW,
            object_type="tool_result",
            object_id="synthetic-operator-revision",
            invocation_ref=operator_invocation_id,
        )
        bridge_invocation = ToolInvocation(
            id=bridge_invocation_id,
            investigation_id=investigation.id,
            operation_key=f"annex-bridge:{bridge_invocation_id}",
            tool_name="rule.propose_bridge",
            schema_version="rule.propose_bridge.v1",
            normalized_parameters={},
            purpose="Synthetic canonical provenance fixture",
            status=ToolInvocationStatus.SUCCEEDED,
            response_refs=(bridge_observation_id,),
            created_at=NOW,
            updated_at=NOW,
        )
        operator_invocation = ToolInvocation(
            id=operator_invocation_id,
            investigation_id=investigation.id,
            operation_key=f"annex-operator:{operator_invocation_id}",
            tool_name="rule.operator_syntax_fix",
            schema_version="rule.operator_syntax_fix.v1",
            normalized_parameters={},
            purpose="Synthetic canonical provenance fixture",
            status=ToolInvocationStatus.SUCCEEDED,
            response_refs=(operator_observation_id,),
            created_at=NOW,
            updated_at=NOW,
        )
        source_v1 = DetectionCandidateVersion(
            id=source_v1_id,
            candidate_id=candidate_id,
            investigation_id=investigation.id,
            source=bridge_source,
            source_sha256=hashlib.sha256(bridge_source.encode()).hexdigest(),
            motif_provenance=(
                MotifProvenance(
                    motif="bridge marker",
                    rationale="Synthetic Bridge source provenance",
                    observation_refs=(bridge_observation_id,),
                ),
            ),
            status=DetectionCandidateStatus.EXPERIMENTAL,
            created_at=NOW,
        )
        source_v2 = DetectionCandidateVersion(
            id=source_v2_id,
            investigation_id=investigation.id,
            candidate_id=candidate_id,
            source=revised_source,
            source_sha256=hashlib.sha256(revised_source.encode()).hexdigest(),
            motif_provenance=(
                MotifProvenance(
                    motif="bridge marker",
                    rationale=(
                        "rule-workbench-motif-v1:{\"source_attribution\":"
                        "{\"author\":\"operator\",\"source_author\":\"operator\","
                        "\"origin_author\":\"bridge\",\"change_kind\":\"operator_syntax_fix\"}}"
                    ),
                    observation_refs=(bridge_observation_id,),
                ),
            ),
            status=DetectionCandidateStatus.TESTED,
            version=2,
            parent_version_id=source_v1.id,
            test_refs=(report_id,),
            created_at=NOW,
        )
        report.update(
            {"version_id": str(source_v2.id), "candidate_id": str(source_v1.candidate_id)}
        )
        report_blob = await catalog.ingest(
            BytesIO(_json_bytes(report)), logical_bucket="annex-test", mime_type="application/json"
        )
        vt_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.VT_FILE_REPORT,
            blob_id=vt_blob.id,
            sha256=vt_blob.descriptor.sha256,
            source="VirusTotal",
            source_version="synthetic-v1",
            observed_at=NOW,
            object_type="file",
            object_id=sample_sha,
        )
        test_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=report_blob.id,
            sha256=report_blob.descriptor.sha256,
            source="AutoWork RuleWorkbench",
            source_version="synthetic-ruleworkbench-v1",
            observed_at=NOW,
            object_type="detection_candidate_version",
            object_id=str(source_v2.id),
            id=report_id,
        )
        advisor_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=advisor_blob.id,
            sha256=advisor_blob.descriptor.sha256,
            source="advisor.consult",
            source_version="operator-advisor-call.v1",
            observed_at=NOW,
            object_type="advisor_call",
            object_id="suricata_draft",
        )
        syntax_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=syntax_blob.id,
            sha256=syntax_blob.descriptor.sha256,
            source="network.suricata_validate_rules",
            source_version="suricata-syntax-v1",
            observed_at=NOW,
            object_type="suricata_rule_validation",
            object_id=hashlib.sha256(suricata_source.encode()).hexdigest(),
        )
        unavailable_observation = EvidenceObservation(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=missing_blob.id,
            sha256=missing_blob.descriptor.sha256,
            source="synthetic.unavailable",
            source_version="fixture-v1",
            observed_at=NOW,
            object_type="synthetic_record",
            object_id="deleted-filesystem-object",
        )
        association = SampleAssociation(
            investigation_id=investigation.id,
            sample_sha256=sample_sha,
            origin=SampleOrigin.VT_PIVOT,
            related_sample_sha256=(unacquired_sha,),
        )
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.samples.add(sample)
            await uow.tool_invocations.add_if_absent(bridge_invocation)
            await uow.tool_invocations.add_if_absent(operator_invocation)
            await uow.evidence_observations.append_if_absent(vt_observation)
            await uow.evidence_observations.append_if_absent(source_v1_observation)
            await uow.evidence_observations.append_if_absent(operator_observation)
            await uow.evidence_observations.append_if_absent(test_observation)
            await uow.evidence_observations.append_if_absent(advisor_observation)
            await uow.evidence_observations.append_if_absent(syntax_observation)
            await uow.evidence_observations.append_if_absent(unavailable_observation)
            await uow.detection_candidates.append_version(source_v1, expected_previous_id=None)
            await uow.detection_candidates.append_version(
                source_v2, expected_previous_id=source_v1.id
            )
            await uow.sample_associations.append_version(association, expected_version=0)
            await uow.investigation_envelopes.save_if_version(
                InvestigationEnvelope(
                    investigation_id=investigation.id,
                    allowed_providers=(),
                    budget=InvestigationBudget(),
                    acquire_hits_allowed=False,
                    disclosure=DisclosureBounds(
                        tlp=TLPLevel.GREEN,
                        external_llm_allowed=True,
                    ),
                    approval_decision_id=uuid4(),
                    approval_actor_id="synthetic-test-approver",
                    approved_at=NOW,
                ),
                expected_version=None,
            )
            await uow.commit()

        # Simulate a canonical blob record whose backing object is missing.
        await blob_store.delete(missing_blob.descriptor)

        # No session or operator workspace remains open while the canonical snapshot is read.
        snapshot = await load_investigation_annex_snapshot(
            session_factory, investigation.id, artifact_store
        )
        public_a = tmp_path / "public-a"
        public_b = tmp_path / "public-b"
        public_result = write_investigation_annex(snapshot, public_a)
        write_investigation_annex(snapshot, public_b)
        public_files = _files(public_a)
        assert public_files == _files(public_b)
        assert sample_bytes not in b"".join(public_files.values())
        manifest = json.loads(public_files["manifest.json"])
        manifest_digest = hashlib.sha256(public_files["manifest.json"]).hexdigest()
        assert public_files["manifest.sha256"] == f"{manifest_digest}  manifest.json\n".encode()
        assert public_result.manifest_sha256 == manifest_digest
        for entry in manifest["files"]:
            content = public_files[entry["path"]]
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
            assert len(content) == entry["size_bytes"]
        for item in manifest["objects"]:
            filename = item["filename"]
            if filename is not None and item["sha256"] is not None:
                assert hashlib.sha256(public_files[filename]).hexdigest() == item["sha256"]
        assert any(
            item["type"] == "evidence_observation" and item["availability"] == "unavailable"
            for item in manifest["objects"]
        )
        by_type = {}
        for item in manifest["objects"]:
            by_type.setdefault(item["type"], []).append(item)
        yara = by_type["yara_rule"]
        assert {item["version"] for item in yara} >= {1, 2}
        assert any(item["sha256"] == source_v1.source_sha256 for item in yara)
        assert any(item["sha256"] == source_v2.source_sha256 for item in yara)
        assert {item["metadata"]["candidate_status"] for item in yara} >= {"experimental", "tested"}
        by_version_id = {item["canonical_id"]: item for item in yara}
        bridge_manifest = by_version_id[str(source_v1.id)]["metadata"]
        operator_manifest = by_version_id[str(source_v2.id)]["metadata"]
        assert bridge_manifest["author"] == "bridge"
        assert bridge_manifest["author_status"] == "recorded"
        assert bridge_manifest["origin"] == "bridge"
        assert bridge_manifest["origin_tool"] == "rule.propose_bridge"
        assert bridge_manifest["origin_invocation_id"] == str(bridge_invocation_id)
        assert bridge_manifest["source_observation_ids"] == [str(bridge_observation_id)]
        assert bridge_manifest["parent_version"] is None
        assert operator_manifest["author"] == "operator"
        assert operator_manifest["author_status"] == "recorded"
        assert operator_manifest["origin"] == "operator"
        assert operator_manifest["origin_tool"] == "rule.operator_syntax_fix"
        assert operator_manifest["origin_invocation_id"] == str(operator_invocation_id)
        assert operator_manifest["source_observation_ids"] == sorted(
            [str(bridge_observation_id), str(operator_observation_id)]
        )
        assert operator_manifest["parent_version"] == str(source_v1.id)
        for item in yara:
            assert item["filename"] is not None
            source_bytes = public_files[item["filename"]]
            assert hashlib.sha256(source_bytes).hexdigest() == item["sha256"]
            assert len(source_bytes) > 7000
        yara_sources = [public_files[item["filename"]] for item in yara]
        assert any(b"author: bridge-fixture; origin: Bridge" in source for source in yara_sources)
        suricata = by_type["suricata_rule"][0]
        assert suricata["metadata"]["rule_syntax_validation"] == "valid"
        assert suricata["metadata"]["replay_status"] == "not_tested"
        assert suricata["filename"] is not None
        assert public_files[suricata["filename"]] == suricata_source.encode()
        assert any(item["type"] == "yara_test_report" for item in manifest["objects"])
        assert any(item["type"] == "observed_ioc" for item in manifest["objects"])
        assert any(
            item["type"] == "discovered_sample" and item["metadata"].get("status") == "not_acquired"
            for item in manifest["objects"]
        )

        archive_dir = tmp_path / "restricted-archive"
        write_investigation_annex(snapshot, archive_dir, include_restricted_archive=True)
        archive = json.loads((archive_dir / "manifest.json").read_bytes())
        sample_assets = [item for item in archive["objects"] if item["type"] == "sample"]
        assert sample_assets
        assert all(item["availability"] == "restricted" for item in sample_assets)
        assert all("tlp_red" in item["restrictions"] for item in sample_assets)
        assert sample_bytes not in b"".join(_files(archive_dir).values())
    finally:
        await engine.dispose()
