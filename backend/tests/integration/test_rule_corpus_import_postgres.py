from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisResult,
)
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import LocalSampleAnalysisService
from cti_app.application.investigation_rule_tools import QuarantineRuleCorpusBytes
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.rule_corpus_import import (
    ManualRuleCorpusImportService,
    list_manual_rule_corpus_imports,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import SampleOrigin, SampleState, Subject
from cti_app.domain.investigation import (
    SampleOrigin as AssociationSampleOrigin,
)
from cti_app.domain.investigation import (
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.rule_workbench import RuleSampleProvenance, RuleTestCorpusRole
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


class _PostgresEvidenceRecorder:
    def __init__(self, uow_factory, artifact_store) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def record_existing_blob(self, **kwargs):
        async with self._uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(self._artifact_store, uow.evidence_observations)
            observation = await recorder.record_existing_blob(**kwargs)
            await uow.commit()
            return observation

    async def record_json(self, **kwargs):
        async with self._uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(self._artifact_store, uow.evidence_observations)
            observation = await recorder.record_json(**kwargs)
            await uow.commit()
            return observation


class _ImportedSampleAnalysisWorker:
    def __init__(self) -> None:
        self.family_statuses = {
            "sample_analysis": ConfinedAnalysisFamilyStatus(
                "sample_analysis", True, "static-test-v1"
            )
        }
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return ConfinedAnalysisResult(
            status=ToolInvocationStatus.SUCCEEDED,
            operation_key=f"confined-analysis:{request.sha256}",
            artifact_id=request.artifact_id,
            input_sha256=request.sha256,
            result_blob_id=request.artifact_id,
            result_sha256=request.sha256,
            payload={
                "result": {
                    "status": "SUCCEEDED",
                    "families": {
                        "static": {
                            "version": "static-test-v1",
                            "status": "SUCCEEDED",
                            "strings": [{"value": "harmless imported fixture"}],
                        }
                    },
                }
            },
        )


@pytest.mark.asyncio
async def test_operator_import_is_persisted_once_in_quarantine_and_read_back_verified(
    uow_factory, tmp_path
) -> None:
    suffix = uuid4().hex
    blob_store = FilesystemBlobStore(tmp_path / "rule-corpus-blobs")
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    tlp = TLP.AMBER
    publication_edition = Edition(
        country=f"Rule corpus import {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=tlp,
        languages=("en",),
    )
    subject = Subject(
        edition_id=publication_edition.id,
        title="Operator-imported rule corpus test",
        slug=f"operator-rule-corpus-{suffix}",
        tlp=tlp,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=publication_edition.id)
    synthesis_payload = {
        "title": "Rule corpus import integration fixture",
        "subject_id": str(subject.id),
        "lead": [{"text": "A synthetic integration claim."}],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="rule-corpus-import-synthesis"
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
        budget=LoopBudget(),
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {"id": str(synthesis.id)},
        "policy": {"tlp": "AMBER", "do_not_submit": True, "external_llm_allowed": False},
    }
    input_pack_blob_id, input_pack_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="rule-corpus-import-input-packs"
    )
    investigation.input_pack_blob_id = input_pack_blob_id
    investigation.input_sha256 = input_pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_pack_blob_id,
        sha256=input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(publication_edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.commit()

    content = (
        b'<?xml version="1.0"?><configuration><runtime>'
        b'<etwEnable enabled="false"/></runtime></configuration>'
    )
    sha256 = hashlib.sha256(content).hexdigest()
    service = ManualRuleCorpusImportService(
        uow_factory=uow_factory,
        blob_catalog=blob_catalog,
        artifact_store=artifact_store,
        settings=SimpleNamespace(rule_corpus_import_max_bytes=1024 * 1024),
    )

    imported = await service.import_bytes(
        investigation_id=investigation.id,
        content=content,
        intended_role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
        provenance=RuleSampleProvenance.SYNTHETIC,
        operator_id="operator:postgres-integration",
        reason="Reproduce one quoted configuration fragment.",
        source_description="Synthetic non-PE .NET XML configuration.",
        declared_sha256=sha256,
    )
    replay = await service.import_bytes(
        investigation_id=investigation.id,
        content=content,
        intended_role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
        provenance=RuleSampleProvenance.SYNTHETIC,
        operator_id="operator:postgres-integration",
        reason="Reproduce one quoted configuration fragment.",
        source_description="Synthetic non-PE .NET XML configuration.",
        declared_sha256=sha256,
    )

    assert imported.sha256 == sha256
    assert imported.size_bytes == len(content)
    assert imported.logical_bucket == "samples-quarantine"
    assert not imported.idempotent_replay
    assert replay.idempotent_replay
    assert replay.observation_id == imported.observation_id
    assert replay.blob_id == imported.blob_id
    assert replay.sample_id == imported.sample_id
    assert replay.association_id == imported.association_id
    assert imported.sample_id is not None
    assert imported.association_id is not None
    inventory = await list_manual_rule_corpus_imports(uow_factory, investigation.id)
    assert len(inventory) == 1
    assert inventory[0].sample_sha256 == sha256
    assert inventory[0].role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    assert inventory[0].provenance is RuleSampleProvenance.SYNTHETIC
    assert inventory[0].size_bytes == len(content)

    reader = QuarantineRuleCorpusBytes(uow_factory, blob_store)
    assert await reader.get_bytes_for_sample(sha256) == content
    hits = HitAcquisitionService(
        uow_factory,
        acquisition=None,
        recorder=_PostgresEvidenceRecorder(uow_factory, artifact_store),
    )
    resolved = await hits.resolve_quarantined_sample(imported.association_id)
    assert resolved.sample_id == imported.sample_id
    assert resolved.sha256 == sha256
    worker = _ImportedSampleAnalysisWorker()
    analyzer = LocalSampleAnalysisService(
        hits,
        uow_factory,
        _PostgresEvidenceRecorder(uow_factory, artifact_store),
        confined_analysis=worker,  # type: ignore[arg-type]
    )
    analyzed = await analyzer.analyze(
        association_id=imported.association_id,
        invocation_ref=None,
    )
    assert analyzed.status is ToolInvocationStatus.PARTIAL
    assert analyzed.output["sample_id"] == str(imported.sample_id)
    assert analyzed.output["sample_sha256"] == sha256
    assert worker.requests[0].artifact_id == imported.blob_id
    assert worker.requests[0].sha256 == sha256
    async with uow_factory() as uow:
        observation = await uow.evidence_observations.get(imported.observation_id)
        assert observation is not None
        assert observation.source == "operator.rule-corpus"
        assert observation.object_type == "manual_rule_corpus_import"
        assert observation.kind.value == "other"
        associations = await uow.sample_associations.list_for_investigation(investigation.id)
        assert len(associations) == 1
        assert associations[0].id == imported.association_id
        assert associations[0].origin is AssociationSampleOrigin.MANUAL
        assert associations[0].qualification is SampleQualification.CANDIDATE
        sample = await uow.samples.get(imported.sample_id)
        assert sample is not None
        assert sample.origin == "operator_import"
        assert sample.source_service == "operator_import"
        assert sample.origin_kind is SampleOrigin.MANUAL
        assert sample.state is SampleState.QUARANTINED
        assert sample.do_not_submit is True
        assert sample.external_llm_allowed is False
        assert any(
            event.event_type == "sample_imported_by_operator"
            and event.payload["provenance"] == "operator_import"
            for event in await uow.provenance.list_for_aggregate("sample", sample.id)
        )


@pytest.mark.asyncio
async def test_postgres_rule_propose_test_and_revise_use_imported_non_pe_corpus(
    uow_factory, tmp_path
) -> None:
    pytest.importorskip("yara")

    from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
    from cti_app.application.investigation_rule_tools import (
        register_rule_tools,
        rule_tool_capabilities,
    )
    from cti_app.application.investigation_tools import (
        InMemoryToolRegistry,
        RegistryToolDispatcher,
        ToolExecutionContext,
    )
    from cti_app.application.rule_workbench import RuleWorkbench
    from cti_app.domain.goodware import NonDiscriminantPatternRegistry
    from cti_app.domain.investigation import (
        BudgetDimension,
        InvestigationBudget,
        ToolInvocationStatus,
    )
    from cti_app.domain.legitimate_services import LegitimateServiceRegistry
    from cti_app.domain.rule_workbench import RuleSampleProvenance, RuleTestCorpusRole
    from cti_app.infrastructure.yara_runner import IsolatedYaraRunner

    suffix = uuid4().hex
    blob_store = FilesystemBlobStore(tmp_path / "rule-corpus-e2e-blobs")
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    tlp = TLP.AMBER
    publication_edition = Edition(
        country=f"Rule corpus rule-test {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=tlp,
        languages=("en",),
    )
    subject = Subject(
        edition_id=publication_edition.id,
        title="Imported rule corpus end-to-end test",
        slug=f"rule-corpus-rule-test-{suffix}",
        tlp=tlp,
    )
    run = ProductionRun(
        subject_id=subject.id,
        edition_id=publication_edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
    )
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        {
            "title": "Rule workbench integration fixture",
            "subject_id": str(subject.id),
            "lead": [{"text": "The article describes a .NET runtime configuration."}],
            "sections": [],
            "timeline": [],
            "uncertainties": [],
        },
        bucket="rule-corpus-e2e-synthesis",
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
        budget=LoopBudget(),
    )
    input_pack_blob_id, input_pack_sha256 = await artifact_store.put_canonical_json(
        {
            "schema_version": "analyst-input-pack-v1",
            "investigation": {"id": str(investigation.id)},
            "synthesis_artifact": {"id": str(synthesis.id)},
            "policy": {"tlp": "AMBER", "do_not_submit": True, "external_llm_allowed": False},
        },
        bucket="rule-corpus-e2e-input-packs",
    )
    investigation.input_pack_blob_id = input_pack_blob_id
    investigation.input_sha256 = input_pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_pack_blob_id,
        sha256=input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(publication_edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.commit()

    second_run = ProductionRun(
        subject_id=subject.id, edition_id=publication_edition.id, run_number=2
    )
    second_synthesis_blob_id, second_synthesis_sha256 = await artifact_store.put_canonical_json(
        {
            "title": "Second rule workbench integration fixture",
            "subject_id": str(subject.id),
            "lead": [{"text": "The same article describes a .NET runtime configuration."}],
            "sections": [],
            "timeline": [],
            "uncertainties": [],
        },
        bucket="rule-corpus-e2e-synthesis",
    )
    second_synthesis = ProductionArtifact(
        production_run_id=second_run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=second_synthesis_sha256,
        canonical_blob_id=second_synthesis_blob_id,
    )
    second_investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=second_synthesis,
        budget=LoopBudget(),
    )
    second_input_pack_blob_id, second_input_pack_sha256 = await artifact_store.put_canonical_json(
        {
            "schema_version": "analyst-input-pack-v1",
            "investigation": {"id": str(second_investigation.id)},
            "synthesis_artifact": {"id": str(second_synthesis.id)},
            "policy": {"tlp": "AMBER", "do_not_submit": True, "external_llm_allowed": False},
        },
        bucket="rule-corpus-e2e-input-packs",
    )
    second_investigation.input_pack_blob_id = second_input_pack_blob_id
    second_investigation.input_sha256 = second_input_pack_sha256
    second_input_pack = AnalystInputPack(
        investigation_id=second_investigation.id,
        blob_id=second_input_pack_blob_id,
        sha256=second_input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    async with uow_factory() as uow:
        await uow.production_runs.add(second_run)
        await uow.production_artifacts.append(second_synthesis)
        await uow.analyst_investigations.add(second_investigation)
        await uow.analyst_input_packs.append(second_input_pack)
        await uow.commit()

    exact_positive = (
        b'<configuration><runtime><etwEnable enabled="false"/>'
        b'<appDomainManagerAssembly assemblyName="Telemetry" '
        b'appDomainManagerType="Telemetry.Loader"/></runtime></configuration>'
    )
    spaced_positive = (
        b'<configuration><runtime><etwEnable enabled = "false"/>'
        b'<appDomainManagerAssembly assemblyName="Telemetry" '
        b'appDomainManagerType="Telemetry.Loader"/></runtime></configuration>'
    )
    negative = (
        b'<configuration><runtime><gcServer enabled="true"/>'
        b'<assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1"/>'
        b"</runtime></configuration>"
    )
    goodware = tuple(
        (
            f'<configuration><runtime><gcServer enabled="{index % 2 == 0}"/>'
            f'<assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1">'
            f'<dependentAssembly><assemblyIdentity name="System.ComponentModel.{index}"/>'
            f"</dependentAssembly></assemblyBinding></runtime></configuration>"
        ).encode()
        for index in range(8)
    )
    imports = [
        (
            exact_positive,
            RuleTestCorpusRole.CONFIRMED_POSITIVE,
            RuleSampleProvenance.SYNTHETIC,
            "Synthetic .NET config reproducing the cited fragment.",
        ),
        (
            spaced_positive,
            RuleTestCorpusRole.CONFIRMED_POSITIVE,
            RuleSampleProvenance.SYNTHETIC,
            "Synthetic whitespace variant of the cited .NET config fragment.",
        ),
        (
            negative,
            RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE,
            RuleSampleProvenance.SYNTHETIC,
            "Synthetic negative .NET runtime config control.",
        ),
        *(
            (
                content,
                RuleTestCorpusRole.GOODWARE,
                RuleSampleProvenance.REAL,
                f"Operator-provided benign .NET configuration fixture {index + 1}.",
            )
            for index, content in enumerate(goodware)
        ),
    ]
    importer = ManualRuleCorpusImportService(
        uow_factory=uow_factory,
        blob_catalog=blob_catalog,
        artifact_store=artifact_store,
        settings=SimpleNamespace(rule_corpus_import_max_bytes=1024 * 1024),
    )
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from cti_app.api.investigation_rule_corpus import router as rule_corpus_router

    class OperatorIdentity:
        async def current(self):
            return SimpleNamespace(actor_id="operator:rule-workbench-integration")

    app = FastAPI()
    app.state.rule_corpus_import_service = importer
    app.state.identity_provider = OperatorIdentity()
    app.include_router(rule_corpus_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://rule-corpus.integration"
    ) as client:
        for target_investigation in (investigation, second_investigation):
            for index, (content, role, provenance, source_description) in enumerate(imports):
                response = await client.post(
                    f"/api/investigations/{target_investigation.id}/rule-corpus/imports",
                    data={
                        "intended_role": role.value,
                        "provenance": provenance.value,
                        "reason": (
                            "Exercise rule tools against an explicitly imported local corpus."
                        ),
                        "source_description": source_description,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                    files={
                        "file": (
                            f"corpus-{index + 1}.xml",
                            content,
                            "application/xml",
                        )
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["sha256"] == hashlib.sha256(content).hexdigest()
                assert response.json()["local_only"] is True
                assert response.json()["executed"] is False
    assert len(await list_manual_rule_corpus_imports(uow_factory, investigation.id)) == 11
    assert len(await list_manual_rule_corpus_imports(uow_factory, second_investigation.id)) == 11

    engine = IsolatedYaraRunner()
    if not engine.available:
        pytest.skip("the isolated YARA worker is unavailable in this environment")
    corpus_bytes = QuarantineRuleCorpusBytes(uow_factory, blob_store)

    class ArticleClaims:
        async def get_claims(self, requested_investigation_id, claim_ids):
            assert requested_investigation_id in {investigation.id, second_investigation.id}
            claims = (
                SimpleNamespace(
                    id="L1.1",
                    statement="The article fragment cites etwEnable enabled=false.",
                    source_refs=("article:paragraph:0004",),
                ),
                SimpleNamespace(
                    id="L1.2",
                    statement="The article fragment names appDomainManagerAssembly and Type.",
                    source_refs=("article:paragraph:0004",),
                ),
                SimpleNamespace(
                    id="L1.3",
                    statement="The article fragment sets appDomainManagerType.",
                    source_refs=("article:paragraph:0004",),
                ),
            )
            return tuple(claim for claim in claims if claim.id in claim_ids)

    async with uow_factory() as uow:
        recorder = ProductionEvidenceRecorder(artifact_store, uow.evidence_observations)
        workbench = RuleWorkbench(
            corpus_bytes=corpus_bytes,
            yara_engine=engine,
            detection_candidates=uow.detection_candidates,
            non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
            legitimate_services=LegitimateServiceRegistry(()),
        )
        capabilities = rule_tool_capabilities(
            workbench=workbench,
            candidates=uow.detection_candidates,
            associations=uow.sample_associations,
            corpus_bytes=corpus_bytes,
            recorder=recorder,
            evidence_observations=uow.evidence_observations,
            evidence_payloads=artifact_store,
        )
        registry = InMemoryToolRegistry()
        register_rule_tools(
            registry,
            workbench=workbench,
            candidates=uow.detection_candidates,
            associations=uow.sample_associations,
            corpus_bytes=corpus_bytes,
            recorder=recorder,
            evidence_observations=uow.evidence_observations,
            evidence_payloads=artifact_store,
            corpus_inventory=corpus_bytes,
            article_claims=ArticleClaims(),
        )
        budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 600})
        context = ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=None,
            budget=budget,
            idempotency_key=f"rule-corpus-postgres-e2e:{suffix}",
            available_capabilities=capabilities,
        )
        dispatcher = RegistryToolDispatcher(registry)

        motifs = [
            {
                "kind": "string",
                "value": "etwEnable",
                "provenance_kind": "manual",
                "provenance_id": "article-fragment:etw-enable",
                "article_claim_refs": ["L1.1"],
            },
            {
                "kind": "string",
                "value": 'enabled="false"',
                "provenance_kind": "manual",
                "provenance_id": "article-fragment:etw-enabled-false",
                "article_claim_refs": ["L1.1"],
            },
            {
                "kind": "string",
                "value": "appDomainManagerAssembly",
                "provenance_kind": "manual",
                "provenance_id": "article-fragment:app-domain-manager-assembly",
                "article_claim_refs": ["L1.2"],
            },
            {
                "kind": "string",
                "value": "appDomainManagerType",
                "provenance_kind": "manual",
                "provenance_id": "article-fragment:app-domain-manager-type",
                "article_claim_refs": ["L1.3"],
            },
        ]

        async def call(tool_name, arguments, operation_key):
            return await dispatcher.dispatch(
                context,
                tool_name=tool_name,
                schema_version="1",
                arguments=arguments,
                purpose=f"PostgreSQL rule corpus integration: {tool_name}",
                operation_key=operation_key,
            )

        proposed = await call(
            "rule.propose",
            {
                "candidate_id": "",
                "motifs": motifs,
                "feature_refs": "",
                "condition": "all",
                "meta": "",
            },
            "rule-propose",
        )
        assert proposed.status is ToolInvocationStatus.SUCCEEDED
        candidate_id = proposed.output["candidate_id"]
        assert proposed.output["version"] == "v1"
        assert proposed.output["candidate_status"] == "candidate"
        assert proposed.output["validation_status"] == "untested"
        assert proposed.output["compile_check"]["status"] == "ok"
        assert "report" not in proposed.output
        replay_without_meta = await call(
            "rule.propose",
            {"candidate_id": candidate_id, "motifs": motifs, "condition": "all"},
            "rule-propose-meta-absent",
        )
        replay_with_empty_meta = await call(
            "rule.propose",
            {
                "candidate_id": candidate_id,
                "motifs": motifs,
                "condition": "all",
                "meta": {},
            },
            "rule-propose-meta-object",
        )
        for replayed_proposal in (replay_without_meta, replay_with_empty_meta):
            assert replayed_proposal.status is ToolInvocationStatus.SUCCEEDED
            assert replayed_proposal.output["candidate_id"] == candidate_id
            assert replayed_proposal.output["version_id"] == proposed.output["version_id"]
            assert replayed_proposal.output["replayed"] is True

        tested = await call(
            "rule.test",
            {"candidate_id": candidate_id, "version": ""},
            "rule-test",
        )
        report = tested.output["report"]
        assert tested.status is ToolInvocationStatus.SUCCEEDED
        assert tested.output["validation_status"] == "real_partial"
        assert report["execution_status"] == "ok"
        assert report["validation_status"] == "real_partial"
        assert report["false_positive_count"] == 0
        assert report["false_negative_count"] == 1
        by_role = {item["role"]: item for item in report["roles"]}
        assert len(by_role["confirmed_positive"]["non_matches"]) == 1
        assert len(by_role["confirmed_positive"]["matches"]) == 1
        assert (
            hashlib.sha256(spaced_positive).hexdigest()
            in by_role["confirmed_positive"]["non_matches"]
        )
        assert (
            hashlib.sha256(exact_positive).hexdigest() in by_role["confirmed_positive"]["matches"]
        )
        assert len(by_role["out_of_cluster_candidate"]["non_matches"]) == 1
        assert by_role["goodware"]["provenance_counts"] == {"real": 8, "synthetic": 0}
        assert len(by_role["goodware"]["non_matches"]) == 8
        assert report["provenance_counts_by_role"]["confirmed_positive"] == {
            "real": 0,
            "synthetic": 2,
        }
        assert report["revision_recommendation"] == "revise_false_negative"
        assert "confirmed-positive byte sample was missed" in report["revision_guidance"]

        revised = await call(
            "rule.revise",
            {
                "candidate_id": candidate_id,
                "parent_version": 1,
                "condition": "",
                "meta": "",
                "motifs": [motif for motif in motifs if motif["value"] != 'enabled="false"'],
            },
            "rule-revise",
        )
        assert revised.status is ToolInvocationStatus.SUCCEEDED
        assert revised.output["version"] == "v2"
        assert revised.output["candidate_status"] == "candidate"
        assert revised.output["validation_status"] == "untested"
        assert revised.output["compile_check"]["status"] == "ok"
        assert "report" not in revised.output

        revised_test = await call(
            "rule.test",
            {"candidate_id": candidate_id, "version": 2},
            "rule-test-v2",
        )
        revised_report = revised_test.output["report"]
        assert revised_test.status is ToolInvocationStatus.SUCCEEDED
        assert revised_test.output["validation_status"] == "real_partial"
        assert revised_report["false_negative_count"] == 0
        assert revised_report["false_positive_count"] == 0
        revised_positive = next(
            item for item in revised_report["roles"] if item["role"] == "confirmed_positive"
        )
        assert len(revised_positive["matches"]) == 2
        assert hashlib.sha256(spaced_positive).hexdigest() in revised_positive["matches"]

        versions = await uow.detection_candidates.list_for_candidate(UUID(candidate_id))
        assert [item.version for item in versions] == [1, 2]
        await uow.commit()

    context_versions = await corpus_bytes.rule_candidate_versions(investigation.id)
    current = max(
        (item for item in context_versions if str(item.candidate_id) == candidate_id),
        key=lambda item: item.version,
    )
    assert current.version == 2
    assert current.id == versions[-1].id

    async with uow_factory() as second_uow:
        second_recorder = ProductionEvidenceRecorder(
            artifact_store, second_uow.evidence_observations
        )
        second_workbench = RuleWorkbench(
            corpus_bytes=corpus_bytes,
            yara_engine=engine,
            detection_candidates=second_uow.detection_candidates,
            non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
            legitimate_services=LegitimateServiceRegistry(()),
        )
        second_capabilities = rule_tool_capabilities(
            workbench=second_workbench,
            candidates=second_uow.detection_candidates,
            associations=second_uow.sample_associations,
            corpus_bytes=corpus_bytes,
            recorder=second_recorder,
            evidence_observations=second_uow.evidence_observations,
            evidence_payloads=artifact_store,
        )
        second_registry = InMemoryToolRegistry()
        register_rule_tools(
            second_registry,
            workbench=second_workbench,
            candidates=second_uow.detection_candidates,
            associations=second_uow.sample_associations,
            corpus_bytes=corpus_bytes,
            recorder=second_recorder,
            evidence_observations=second_uow.evidence_observations,
            evidence_payloads=artifact_store,
            corpus_inventory=corpus_bytes,
            article_claims=ArticleClaims(),
        )
        second_context = ToolExecutionContext(
            investigation_id=second_investigation.id,
            envelope=None,
            budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 600}),
            idempotency_key=f"rule-corpus-postgres-e2e-second:{suffix}",
            available_capabilities=second_capabilities,
        )
        second_dispatcher = RegistryToolDispatcher(second_registry)
        second_proposed = await second_dispatcher.dispatch(
            second_context,
            tool_name="rule.propose",
            schema_version="1",
            arguments={"motifs": motifs, "condition": "all", "meta": {}},
            purpose="Reproduce a proposal in another investigation for the same subject.",
            operation_key="second-investigation-rule-propose",
        )
        assert second_proposed.status is ToolInvocationStatus.SUCCEEDED
        assert second_proposed.output["candidate_id"] != candidate_id
        assert second_proposed.output["version"] == "v1"
        assert second_proposed.output["validation_status"] == "untested"
        assert second_proposed.output["compile_check"]["status"] == "ok"
        assert "report" not in second_proposed.output
        await second_uow.commit()
