"""PostgreSQL-backed proof of the canonical AW-012 Synthesis pipeline.

The scenario keeps PostgreSQL, the blob catalog, the real workflow, jobs and
repositories in the loop; only the HTTP and model provider boundaries stay
scripted.  REFERENCES and EXTRACTION run through their canonical stages and
SYNTHESIS runs through ``ProductionSynthesisService``: one stateless
``ModelGateway.draft`` call whose answer is the structured proposal.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date
from typing import Any
from uuid import UUID

import pytest

from cti_app.application import (
    production_extraction,
    production_parsers,
    production_references,
    production_synthesis,
    production_workflow,
)
from cti_app.application.editions import EditionService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelRole,
    ModelUsage,
    SafeModelRequest,
)
from cti_app.application.production_extraction import extraction_input_hash
from cti_app.application.production_jobs import (
    PRODUCTION_STAGE_MAX_ATTEMPTS,
    ProductionStageParameters,
    production_stage_idempotency_key,
    stage_job_kind,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.application.production_synthesis import (
    SYNTHESIS_PROMPT_VERSION,
    SynthesisClaimProposalV1,
    SynthesisProposalV1,
    SynthesisSectionProposalV1,
    build_synthesis_delta,
    canonical_extraction_hash,
    production_synthesis_from_json,
)
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.subject_production import SubjectProductionService
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    DetectionRuleType,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_extraction import (
    ExtractionFactV1,
    ProductionExtractionV1,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisSectionKind,
    extraction_evidence_refs_v1,
)

from .support import ProductionScenario

pytestmark = pytest.mark.integration

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]

SOURCE_URLS = (
    "https://canonical.test/core-one",
    "https://canonical.test/core-two",
)

_BODY_ONE = (
    "On 2026-08-05 the loader delivered ExampleRAT against the organization.\n"
    "The implant beaconed to alpha-c2.security-lab.io.\n"
    "ExampleRAT is the tracked malware family of the publication.\n"
)

_BODY_TWO = (
    "On 2026-08-07 the operators moved to beta-c2.security-lab.io.\n"
    "The reports document a second execution chain.\n"
    "rule CanonicalSupport { condition: true }\n"
    "The persistence mechanism remains unconfirmed.\n"
)

Q1_RESPONSE = """# REFERENCES

## SOURCE S1

title: Canonical core report one
url: https://canonical.test/core-one
publisher: Canonical Labs
published-at: 2026-08-03
role: primary
kind: publication
reason: Primary reporting on the campaign

## SOURCE S2

title: Canonical core report two
url: https://canonical.test/core-two
publisher: Canonical Labs
published-at: 2026-08-06
role: independent
kind: publication
reason: Independent corroboration of the campaign

## EVENT R1

date: 2026-08-30
sources: S2
text: The references stage records an editorial boundary event.
"""

_CORE_ONE_OUTPUT = Q2SourceOutput(
    facts=[
        Q2FactProposal(
            category="malware",
            value="ExampleRAT",
            evidence_quote="ExampleRAT is the tracked malware family of the publication.",
        )
    ],
    events=[
        Q2EventProposal(
            event_date=date(2026, 8, 5),
            text="The loader delivered ExampleRAT.",
            evidence_quote=(
                "On 2026-08-05 the loader delivered ExampleRAT against the organization."
            ),
        )
    ],
    artifacts=[
        Q2ArtifactProposal(
            value="alpha-c2.security-lab.io",
            artifact_type="domain",
            indicator_status="confirmed_ioc",
            evidence_quote="The implant beaconed to alpha-c2.security-lab.io.",
        )
    ],
    uncertainties=["The initial access vector remains unconfirmed."],
)

_CORE_TWO_OUTPUT = Q2SourceOutput(
    events=[
        Q2EventProposal(
            event_date=date(2026, 8, 7),
            text="The operators moved to the second infrastructure.",
            evidence_quote="On 2026-08-07 the operators moved to beta-c2.security-lab.io.",
        ),
        Q2EventProposal(
            text="The reports document a second execution chain.",
            evidence_quote="The reports document a second execution chain.",
        ),
    ],
    artifacts=[
        Q2ArtifactProposal(
            value="beta-c2.security-lab.io",
            artifact_type="domain",
            indicator_status="contextual",
            evidence_quote="On 2026-08-07 the operators moved to beta-c2.security-lab.io.",
        )
    ],
    rules=[
        Q2RuleProposal(
            rule_type=DetectionRuleType.YARA,
            name="CanonicalSupport",
            body="rule CanonicalSupport { condition: true }",
            evidence_quote="rule CanonicalSupport { condition: true }",
        )
    ],
    uncertainties=["The persistence mechanism remains unconfirmed."],
)


def _sources() -> dict[str, dict[str, object]]:
    return {
        SOURCE_URLS[0]: {"status": 200, "mime": "text/html", "body": _BODY_ONE},
        SOURCE_URLS[1]: {"status": 200, "mime": "text/html", "body": _BODY_TWO},
    }


def _configure(scenario: ProductionScenario) -> ProductionScenario:
    script = scenario.model.script
    script.references(Q1_RESPONSE)
    script.q2(source_url=SOURCE_URLS[0], response=_CORE_ONE_OUTPUT)
    script.q2(source_url=SOURCE_URLS[1], response=_CORE_TWO_OUTPUT)
    return scenario


def _proposal_from_prompt(prompt_text: str) -> SynthesisProposalV1:
    """Answer the canonical prompt with a grounded, deterministic proposal."""
    payload = json.loads(prompt_text)
    pack = payload["current_evidence_pack"]
    narrative = list(pack["narrative_evidence"])
    technical = list(pack["technical_evidence"])
    facts = [record for record in narrative if record["kind"] == "fact"]
    assert facts, "The canonical evidence pack exposes the narrative fact"
    fact = facts[0]
    lead = SynthesisClaimProposalV1(
        text=f"{fact['value']} is documented by the selected publications.",
        evidence_handles=(fact["handle"],),
    )
    domains = tuple(
        SynthesisClaimProposalV1(
            text=f"The publications list the infrastructure domain {record['value']}.",
            evidence_handles=(record["handle"],),
        )
        for record in technical
        if record["kind"] == "indicator"
    )
    rules = tuple(
        SynthesisClaimProposalV1(
            text=f"The publication provides the detection rule {record['name']}.",
            evidence_handles=(record["handle"],),
        )
        for record in technical
        if record["kind"] == "rule" and record["name"]
    )
    sections: list[SynthesisSectionProposalV1] = []
    if domains:
        sections.append(
            SynthesisSectionProposalV1(
                kind=SynthesisSectionKind.INFRASTRUCTURE,
                heading="Infrastructure",
                claims=domains,
            )
        )
    if rules:
        sections.append(
            SynthesisSectionProposalV1(
                kind=SynthesisSectionKind.DETECTION,
                heading="Detection",
                claims=rules,
            )
        )
    if not sections:
        sections.append(
            SynthesisSectionProposalV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="Overview",
                claims=(lead,),
            )
        )
    return SynthesisProposalV1(lead=(lead,), sections=tuple(sections))


def _install_canonical_synthesis(scenario: ProductionScenario) -> list[SafeModelRequest]:
    """Answer canonical drafting requests; fail the test on any bad request."""
    adapter = scenario.model._adapter
    requests: list[SafeModelRequest] = []
    base_invoke = adapter.invoke

    async def invoke(
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        if request.prompt_template_id != "production-synthesis":
            return await base_invoke(request, role=role, output_schema=output_schema)
        if request.web_search:
            raise AssertionError("Canonical Synthesis must never enable web search")
        if request.conversation is not None:
            raise AssertionError("Canonical Synthesis must be stateless")
        requests.append(request)
        scenario.model.provider_calls.append(request)
        proposal = _proposal_from_prompt(request.text)
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=adapter.provider,
            requested_model=str(adapter.requested_model),
            actual_model_version=str(adapter.requested_model),
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            response_id=f"canonical-synthesis-{len(requests)}",
            output_text=proposal.model_dump_json(),
            structured_output=proposal,
        )

    adapter.invoke = invoke  # type: ignore[method-assign]
    return requests


def _install_legacy_synthesis_tripwire(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Explode if the canonical path reaches a legacy Synthesis input."""
    triggered: list[str] = []

    def explode(name: str) -> Callable[..., Any]:
        def fail(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            triggered.append(name)
            raise AssertionError(f"legacy Synthesis input was read: {name}")

        return fail

    targets: tuple[tuple[Any, tuple[str, ...]], ...] = (
        (
            production_synthesis,
            (
                "ReferenceReport",
                "TechnicalExtraction",
                "load_reference_projection",
                "reference_report_to_json",
                "load_legacy_technical_extraction",
            ),
        ),
        (
            production_workflow,
            (
                "ReferenceReport",
                "TechnicalExtraction",
                "load_reference_projection",
                "reference_report_to_json",
                "load_legacy_technical_extraction",
            ),
        ),
        (production_references, ("load_reference_projection",)),
        (production_parsers, ("reference_report_to_json",)),
        (production_extraction, ("load_legacy_technical_extraction",)),
    )
    for module, names in targets:
        for name in names:
            monkeypatch.setattr(module, name, explode(name), raising=False)
    return triggered


async def _switch_edition_language(scenario: ProductionScenario, language: str) -> None:
    async with scenario.uow_factory() as uow:
        edition = await uow.editions.get(scenario.edition.id)
    assert edition is not None
    await EditionService(scenario.uow_factory).update(
        edition.id,
        expected_version=edition.version,
        country=edition.country,
        country_code=edition.country_code,
        period_start=edition.period_start,
        period_end=edition.period_end,
        tlp=edition.tlp,
        languages=(language,),
        actor_id="canonical-synthesis-test",
        correlation_id="canonical-synthesis-test",
    )


async def _current_artifact(
    scenario: ProductionScenario, run_id: UUID, stage: ProductionArtifactStage
) -> ProductionArtifact | None:
    async with scenario.uow_factory() as uow:
        return await uow.production_artifacts.get_current(run_id, stage.value)


async def _read_synthesis(
    scenario: ProductionScenario, artifact: ProductionArtifact
) -> ProductionSynthesisV1:
    assert artifact.canonical_blob_id is not None
    payload = await scenario.artifact_store.read_json(artifact.canonical_blob_id)
    return production_synthesis_from_json(payload)


def _evidence_refs(synthesis: ProductionSynthesisV1) -> set[ExtractionEvidenceRefV1]:
    refs = {ref for paragraph in synthesis.lead for ref in paragraph.evidence_refs}
    refs.update(
        ref
        for section in synthesis.sections
        for paragraph in section.paragraphs
        for ref in paragraph.evidence_refs
    )
    refs.update(ref for entry in synthesis.timeline for ref in entry.evidence_refs)
    return refs


async def _start_followup_run(scenario: ProductionScenario) -> ProductionRun:
    """Start one more real run of the same subject on unchanged inputs."""
    production = SubjectProductionService(scenario.uow_factory)
    run, created = await production.create_run(scenario.subject.id, scenario.edition.id)
    assert created
    run = await production.start_run(run.id)
    parameters = ProductionStageParameters(
        run_id=run.id,
        expected_stage=ProductionStage.SOURCES.value,
        pipeline_generation=run.pipeline_generation,
    )
    job = await scenario.jobs.submit(
        kind=stage_job_kind(ProductionStage.SOURCES),
        aggregate_type="subject",
        aggregate_id=run.subject_id,
        idempotency_key=production_stage_idempotency_key(run, ProductionStage.SOURCES),
        correlation_id="canonical-synthesis-test",
        input_parameters=parameters.model_dump(mode="json"),
        max_attempts=PRODUCTION_STAGE_MAX_ATTEMPTS,
        actor_id="canonical-synthesis-test",
    )
    if job.status is JobStatus.QUEUED:
        await scenario.runner.dispatch(job.id)
    return run


async def _drain(scenario: ProductionScenario, run_id: UUID) -> ProductionRun:
    await scenario.runner.run_until_idle()
    async with scenario.uow_factory() as uow:
        run = await uow.production_runs.get(run_id)
    assert run is not None
    return run


async def _prepare_synthesis_run(scenario: ProductionScenario) -> ProductionRun:
    """Create a run parked on SYNTHESIS with a fresh frozen snapshot."""
    production = SubjectProductionService(scenario.uow_factory)
    run, created = await production.create_run(scenario.subject.id, scenario.edition.id)
    assert created
    run = await production.start_run(run.id)
    async with scenario.uow_factory() as uow:
        persisted = await uow.production_runs.get_for_update(run.id)
        assert persisted is not None
        persisted.current_stage = ProductionStage.SYNTHESIS
        await uow.production_runs.save(persisted)
        await uow.commit()
    return run


def _revised_extraction(
    extraction: ProductionExtractionV1, *, production_input_hash: str
) -> ProductionExtractionV1:
    """Swap one narrative fact for another, keeping every other evidence ref."""
    first = next(source for source in extraction.sources if source.canonical_url == SOURCE_URLS[0])
    second = next(source for source in extraction.sources if source.canonical_url == SOURCE_URLS[1])
    assert first.facts, "The canonical fixture expects a narrative fact on the first source"
    replacement = ExtractionFactV1(
        category="malware",
        value="BetaLoader",
        attack_id=None,
        context="",
        evidence_quote="BetaLoader was named by the second publication.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(second.source_document_id,),
    )
    return replace(
        extraction,
        production_input_hash=production_input_hash,
        sources=(replace(first, facts=()), replace(second, facts=(replacement,))),
    )


async def _store_extraction_artifact(
    scenario: ProductionScenario, run: ProductionRun, extraction: ProductionExtractionV1
) -> ProductionArtifact:
    _, canonical_id, _ = await scenario.artifact_store.store_stage_payloads(
        canonical=production_extraction_to_json(extraction)
    )
    assert canonical_id is not None
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=extraction.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=extraction_input_hash(references_corpus_hash=extraction.references_corpus_hash),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_id,
    )
    async with scenario.uow_factory() as uow:
        await uow.production_artifacts.append(artifact)
        await uow.commit()
    return artifact


@pytest.mark.asyncio
async def test_canonical_synthesis_pipeline_persists_reloads_and_assembles(
    production_scenario_factory: ScenarioFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _configure(production_scenario_factory(_sources()))
    tripwire = _install_legacy_synthesis_tripwire(monkeypatch)
    drafts = _install_canonical_synthesis(scenario)

    await scenario.start()
    run = await scenario.run_until_terminal()

    assert run.status is ProductionRunStatus.READY, (run.error_code, run.error_details)
    assert run.current_stage is ProductionStage.ASSEMBLY
    assert tripwire == []
    assert len(drafts) == 1

    async with scenario.uow_factory() as uow:
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        synthesis_artifact = await uow.production_artifacts.get_current(run.id, "synthesis")
        extraction_artifact = await uow.production_artifacts.get_current(run.id, "extraction")
        publication_artifact = await uow.production_artifacts.get_current(run.id, "publication")
    assert snapshot is not None
    assert synthesis_artifact is not None and synthesis_artifact.canonical_blob_id is not None
    assert extraction_artifact is not None and extraction_artifact.canonical_blob_id is not None
    assert publication_artifact is not None and publication_artifact.canonical_blob_id is not None
    assert synthesis_artifact.model_run_id is not None
    async with scenario.uow_factory() as uow:
        model_run = await uow.model_runs.get(synthesis_artifact.model_run_id)

    extraction = production_extraction_from_json(
        await scenario.artifact_store.read_json(extraction_artifact.canonical_blob_id)
    )
    synthesis = await _read_synthesis(scenario, synthesis_artifact)

    # Canonical lineage: frozen snapshot identity plus the canonical extraction hash.
    assert snapshot.publication_language == "fr"
    assert synthesis.publication_language == snapshot.publication_language == "fr"
    assert synthesis.title == snapshot.subject_title
    assert synthesis.subject_id == snapshot.subject_id
    assert synthesis.production_input_hash == snapshot.input_hash
    assert synthesis.extraction_hash == canonical_extraction_hash(extraction)
    # The persisted drafting ModelRun is the one behind the canonical artifact.
    assert model_run is not None
    assert model_run.id == synthesis_artifact.model_run_id
    assert model_run.prompt_template_id == "production-synthesis"

    # Every narrative paragraph is grounded in extraction evidence.
    paragraphs = (*synthesis.lead, *(p for s in synthesis.sections for p in s.paragraphs))
    assert paragraphs
    assert all(paragraph.evidence_refs for paragraph in paragraphs)
    known_refs = set(extraction_evidence_refs_v1(extraction))
    assert _evidence_refs(synthesis) <= known_refs

    # The timeline is derived from AW-011 extraction events, never from Q1 EVENTs.
    assert [entry.event_date for entry in synthesis.timeline] == [
        date(2026, 8, 5),
        date(2026, 8, 7),
        None,
    ]
    assert all(
        ref.kind is EvidenceKind.EVENT
        for entry in synthesis.timeline
        for ref in entry.evidence_refs
    )
    assert [item.text for item in synthesis.uncertainties] == [
        "The initial access vector remains unconfirmed.",
        "The persistence mechanism remains unconfirmed.",
    ]

    request = drafts[0]
    assert request.prompt_template_id == "production-synthesis"
    assert request.prompt_template_version == SYNTHESIS_PROMPT_VERSION
    assert request.web_search is False
    assert request.conversation is None
    assert request.metadata["synthesis_mode"] == "fresh"
    assert request.metadata["synthesis_input_hash"] == synthesis_artifact.input_hash
    assert model_run.status is ModelRunStatus.SUCCEEDED
    for source in extraction.sources:
        assert str(source.source_document_id) not in request.text
    assert "[S1]" not in request.text

    assert synthesis_artifact.metadata["schema_version"] == 1
    assert synthesis_artifact.metadata["language"] == "fr"
    assert synthesis_artifact.metadata["mode"] == "fresh"
    assert synthesis_artifact.conversation_turn_id is None
    assert synthesis_artifact.rendered_blob_id is not None
    preview = await scenario.artifact_store.read_text(synthesis_artifact.rendered_blob_id)
    assert "The references stage records an editorial boundary event." not in preview

    # Assembly consumes the canonical blob through the one-way compatibility adapter.
    publication_payload = await scenario.artifact_store.read_json(
        publication_artifact.canonical_blob_id
    )
    assert publication_payload["schema_version"] == "2"
    publication_text = json.dumps(publication_payload)
    assert "ExampleRAT is documented by the selected publications." in publication_text
    assert "alpha-c2.security-lab.io" in publication_text
    assert "beta-c2.security-lab.io" in publication_text


@pytest.mark.asyncio
async def test_canonical_synthesis_exact_reuse_skips_drafting(
    production_scenario_factory: ScenarioFactory,
) -> None:
    scenario = _configure(production_scenario_factory(_sources()))
    drafts = _install_canonical_synthesis(scenario)

    await scenario.start()
    first_run = await scenario.run_until_terminal()
    assert first_run.status is ProductionRunStatus.READY, (
        first_run.error_code,
        first_run.error_details,
    )
    assert len(drafts) == 1
    first_artifact = await _current_artifact(
        scenario, first_run.id, ProductionArtifactStage.SYNTHESIS
    )
    assert first_artifact is not None and first_artifact.canonical_blob_id is not None
    first_payload = await scenario.artifact_store.read_json(first_artifact.canonical_blob_id)
    provider_calls_before = len(scenario.model.provider_calls)

    second_run = await _start_followup_run(scenario)
    terminal = await _drain(scenario, second_run.id)

    assert terminal.status is ProductionRunStatus.READY, (
        terminal.error_code,
        terminal.error_details,
    )
    # Exact reuse: not a single provider submission, drafting included.
    assert len(drafts) == 1
    assert len(scenario.model.provider_calls) == provider_calls_before

    second_artifact = await _current_artifact(
        scenario, second_run.id, ProductionArtifactStage.SYNTHESIS
    )
    assert second_artifact is not None
    assert second_artifact.id != first_artifact.id
    assert second_artifact.reused_from_artifact_id == first_artifact.id
    assert second_artifact.canonical_blob_id == first_artifact.canonical_blob_id
    assert second_artifact.rendered_blob_id == first_artifact.rendered_blob_id
    assert second_artifact.metadata["reused"] is True
    reloaded = await scenario.artifact_store.read_json(first_artifact.canonical_blob_id)
    assert reloaded == first_payload


@pytest.mark.asyncio
async def test_canonical_synthesis_revision_drops_removed_evidence(
    production_scenario_factory: ScenarioFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = _configure(production_scenario_factory(_sources()))
    tripwire = _install_legacy_synthesis_tripwire(monkeypatch)
    drafts = _install_canonical_synthesis(scenario)

    await scenario.start()
    first_run = await scenario.run_until_terminal()
    assert first_run.status is ProductionRunStatus.READY, (
        first_run.error_code,
        first_run.error_details,
    )
    assert tripwire == []

    first_snapshot, first_artifact = None, None
    async with scenario.uow_factory() as uow:
        first_snapshot = await uow.production_input_snapshots.get_by_run(first_run.id)
        first_artifact = await uow.production_artifacts.get_current(first_run.id, "synthesis")
        extraction_artifact = await uow.production_artifacts.get_current(first_run.id, "extraction")
    assert first_snapshot is not None and first_snapshot.publication_language == "fr"
    assert first_artifact is not None and first_artifact.canonical_blob_id is not None
    assert extraction_artifact is not None and extraction_artifact.canonical_blob_id is not None
    extraction_a = production_extraction_from_json(
        await scenario.artifact_store.read_json(extraction_artifact.canonical_blob_id)
    )
    synthesis_a = await _read_synthesis(scenario, first_artifact)
    assert synthesis_a.publication_language == "fr"

    # A later Edition language change never rewrites an existing run.
    await _switch_edition_language(scenario, "en")
    second_run = await _prepare_synthesis_run(scenario)
    async with scenario.uow_factory() as uow:
        second_snapshot = await uow.production_input_snapshots.get_by_run(second_run.id)
        preserved_snapshot = await uow.production_input_snapshots.get_by_run(first_run.id)
    assert second_snapshot is not None and second_snapshot.publication_language == "en"
    assert second_snapshot.input_hash != first_snapshot.input_hash
    assert preserved_snapshot is not None and preserved_snapshot.publication_language == "fr"

    extraction_b = _revised_extraction(
        extraction_a, production_input_hash=second_snapshot.input_hash
    )
    expected_delta = build_synthesis_delta(extraction_a, extraction_b)
    assert expected_delta.removed_evidence
    assert expected_delta.added_evidence
    assert set(expected_delta.removed_evidence) <= _evidence_refs(synthesis_a)
    await _store_extraction_artifact(scenario, second_run, extraction_b)

    orchestrator = ProductionWorkflowOrchestrator(
        scenario.uow_factory,
        model_gateway=scenario.model,
        artifact_store=scenario.artifact_store,
    )
    drafts_before = len(drafts)
    result = await orchestrator.execute_stage(second_run.id, ProductionStage.SYNTHESIS)

    assert result["status"] == "success", result
    assert result["mode"] == "revise_previous"
    assert result["previous_synthesis_artifact_id"] == str(first_artifact.id)
    assert result["added_evidence_count"] == len(expected_delta.added_evidence)
    assert result["removed_evidence_count"] == len(expected_delta.removed_evidence)
    assert len(drafts) == drafts_before + 1
    assert drafts[-1].web_search is False
    assert drafts[-1].conversation is None

    revised_artifact = await _current_artifact(
        scenario, second_run.id, ProductionArtifactStage.SYNTHESIS
    )
    assert revised_artifact is not None and revised_artifact.canonical_blob_id is not None
    assert revised_artifact.metadata["mode"] == "revise_previous"
    revised = await _read_synthesis(scenario, revised_artifact)

    assert revised.publication_language == "en"
    assert revised.extraction_hash == canonical_extraction_hash(extraction_b)
    revised_refs = _evidence_refs(revised)
    assert revised_refs.isdisjoint(set(expected_delta.removed_evidence))
    assert set(expected_delta.added_evidence) <= revised_refs
    assert revised_refs <= set(extraction_evidence_refs_v1(extraction_b))
    assert tripwire == []
