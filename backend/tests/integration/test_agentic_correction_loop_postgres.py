"""PostgreSQL proof of localized inspection, targeted correction, and render reuse."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy import select

from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.pdf_inspection import PdfInspectionService
from cti_app.application.production_editorial_enrichment import (
    ProductionEditorialEnrichmentService,
    editorial_enrichment_from_json,
)
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.typst_compilation import TypstCompileRequest
from cti_app.application.typst_input_inspection import RenderInputInspectionService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.pdf_inspection import PdfInspectionProfile
from cti_app.domain.production import ProductionArtifactStage, ProductionRunStatus
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionConflictError,
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from cti_app.infrastructure.database.models.publication_render import PublicationRenderRow
from cti_app.infrastructure.database.repositories.pdf_inspection import (
    SqlAlchemyPdfInspectionSourceRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.production.support import ProductionScenario, grounded_editorial_proposal
from tests.integration.production.test_pipeline_happy_path import (
    _configured_editorial_scenario,
    _install_canonical_synthesis,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def production_scenario_factory(
    uow_factory: Any, tmp_path: Path
) -> Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]:
    def factory(sources: Mapping[str, Mapping[str, object]]) -> ProductionScenario:
        return ProductionScenario(uow_factory, tmp_path / "blobs", sources)

    return factory


class _CountingTypstCompiler(TypstSubprocessCompiler):
    def __init__(self) -> None:
        super().__init__()
        self.compile_calls = 0
        self.query_calls = 0

    async def compile(self, request: TypstCompileRequest) -> Any:
        self.compile_calls += 1
        return await super().compile(request)

    async def query_metadata(self, request: TypstCompileRequest) -> bytes:
        self.query_calls += 1
        return await super().query_metadata(request)


def _current_identity(artifact: Any) -> tuple[UUID, int, str]:
    return artifact.id, artifact.version, artifact.input_hash


def _identity_json(artifact: Any) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "version": artifact.version,
        "input_hash": artifact.input_hash,
    }


async def _model_run_ids(scenario: ProductionScenario) -> set[UUID]:
    async with scenario.uow_factory() as uow:
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        assert sql_uow._session is not None
        rows = await sql_uow._session.scalars(select(ModelRunRow.id))
        return set(rows.all())


async def _publication_render_ids(scenario: ProductionScenario) -> set[UUID]:
    async with scenario.uow_factory() as uow:
        sql_uow = cast(SqlAlchemyUnitOfWork, uow)
        assert sql_uow._session is not None
        rows = await sql_uow._session.scalars(select(PublicationRenderRow.id))
        return set(rows.all())


@pytest.mark.asyncio
async def test_postgres_localized_diagram_correction_and_rerender_loop(
    production_scenario_factory: Any,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    scenario: ProductionScenario = await _configured_editorial_scenario(
        production_scenario_factory, capture_tag="e2-correction-loop"
    )
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    synthesis_calls = _install_canonical_synthesis(scenario)

    compile_diagram = scenario.diagram_compiler.compile

    async def compile_tiny_diagram(diagram: Any) -> Any:
        compiled = await compile_diagram(diagram)
        media = (
            b'<svg xmlns="http://www.w3.org/2000/svg" width="30" height="10" '
            b'viewBox="0 0 30 10"><rect width="30" height="10" fill="#607d8b"/></svg>'
        )
        return replace(compiled, media_bytes=media, media_sha256=hashlib.sha256(media).hexdigest())

    scenario.diagram_compiler.compile = compile_tiny_diagram  # type: ignore[method-assign]
    initial_run = await scenario.start()
    assert initial_run.status is ProductionRunStatus.RUNNING
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY, (run.error_code, run.error_message)
    assert len(synthesis_calls) == 1

    stages_to_preserve = (
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.RELEVANCE_PROJECTION,
        ProductionArtifactStage.SYNTHESIS,
    )
    async with scenario.uow_factory() as uow:
        preserved_before = {
            stage: await uow.production_artifacts.get_current(run.id, stage.value)
            for stage in stages_to_preserve
        }
        enrichment_before = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        publication_before = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.PUBLICATION.value
        )
    assert all(item is not None for item in preserved_before.values())
    assert enrichment_before is not None and publication_before is not None
    enrichment_payload_before = await scenario.artifact_store.read_json(
        enrichment_before.canonical_blob_id  # type: ignore[arg-type]
    )
    enrichment_canonical_sha_before = hashlib.sha256(
        await scenario.artifact_store.read_bytes(
            enrichment_before.canonical_blob_id  # type: ignore[arg-type]
        )
    ).hexdigest()
    publication_canonical_sha_before = hashlib.sha256(
        await scenario.artifact_store.read_bytes(
            publication_before.canonical_blob_id  # type: ignore[arg-type]
        )
    ).hexdigest()
    canonical_enrichment_before = editorial_enrichment_from_json(enrichment_payload_before)
    assert len(canonical_enrichment_before.diagrams) == 1
    assert len(canonical_enrichment_before.tables) == 1
    diagram_key = canonical_enrichment_before.diagrams[0].key
    canonical_document_before = await scenario.artifact_store.read_json(
        publication_before.canonical_blob_id  # type: ignore[arg-type]
    )
    assert canonical_document_before["subject_id"] == str(run.subject_id)
    assert diagram_key in json.dumps(canonical_document_before)
    table_before = next(
        item
        for item in enrichment_payload_before["tables"]
        if item["key"] == canonical_enrichment_before.tables[0].key
    )

    engine = create_postgres_engine(migrated_postgres_url)
    cache = PdfInspectionCache(tmp_path / "inspection-cache")
    compiler = _CountingTypstCompiler()
    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    media_asset_store = MediaAssetStore(scenario.artifact_store, scenario.uow_factory)
    render_service = PublicationRenderService(
        uow_factory=scenario.uow_factory,
        artifact_store=scenario.artifact_store,
        media_asset_store=media_asset_store,
        renderer=TypstRenderer(),
        compiler=compiler,
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
        mapping_cache=cache,
    )
    try:
        pipeline_model_calls = len(scenario.model.provider_calls)
        pipeline_model_run_ids = await _model_run_ids(scenario)
        initial_render = await render_service.render_pdf(publication_before.id)
        assert initial_render.output_sha256 is not None
        assert compiler.compile_calls == 1
        assert compiler.query_calls == 1
        inspection_repository = SqlAlchemyPdfInspectionSourceRepository(
            create_session_factory(engine)
        )
        inspection = PdfInspectionService(
            inspection_repository,
            scenario.artifact_store,
            PopplerSubprocess(temp_root=tmp_path / "poppler"),
            cache,
        )
        input_inspection = RenderInputInspectionService(
            inspection_repository,
            scenario.artifact_store,
            inspection,
            cache,
        )
        before_inspection = await inspection.metadata(initial_render.output_sha256)
        initial_render_ids = await _publication_render_ids(scenario)
        assert len(initial_render_ids) == 1
        initial_render_id = str(next(iter(initial_render_ids)))
        inspected_inputs = await input_inspection.metadata(initial_render_id)
        source_inventory = await input_inspection.files(initial_render_id)
        entrypoint = next(
            item for item in source_inventory["files"] if item["path"].endswith("publication.typ")
        )
        inspected_entrypoint = await input_inspection.file(initial_render_id, entrypoint["file_id"])
        inspected_render_data = await input_inspection.render_data(initial_render_id)
        inspected_input_diagnostics = await input_inspection.diagnostics(initial_render_id)
        assert inspected_inputs["source"]["availability"] == "ready"
        assert source_inventory["availability"] == "ready"
        assert inspected_entrypoint["path"] == entrypoint["path"]
        assert inspected_entrypoint["sha256"] == entrypoint["sha256"]
        assert isinstance(inspected_entrypoint["content"], str)
        assert inspected_render_data["availability"] == "ready"
        assert diagram_key in json.dumps(inspected_render_data["json"])
        assert inspected_input_diagnostics["render_id"] == initial_render_id
        assert inspected_input_diagnostics["pdf"]["sha256"] == initial_render.output_sha256
        assert len(scenario.model.provider_calls) == pipeline_model_calls
        assert await _model_run_ids(scenario) == pipeline_model_run_ids
        old_hash = before_inspection["pdf"]["sha256"]
        assert old_hash == initial_render.output_sha256
        assert before_inspection["pdf"]["page_count"] >= 1
        diagram_mapping = next(
            item
            for item in before_inspection["mapping"]["items"]
            if item["target"]["block_key"] == diagram_key
        )
        table_mapping_before = next(
            item
            for item in before_inspection["mapping"]["items"]
            if item["target"]["block_key"] == canonical_enrichment_before.tables[0].key
        )
        assert diagram_mapping["target"]["block_type"] == "diagram"
        assert diagram_mapping["mapping_quality"] == "canonical_block_key"
        assert diagram_mapping["target"]["artifact_id"] == str(enrichment_before.id)
        assert diagram_mapping["target"]["artifact_version"] == enrichment_before.version
        assert diagram_mapping["target"]["artifact_input_hash"] == enrichment_before.input_hash
        title_mapping = next(
            item
            for item in before_inspection["mapping"]["items"]
            if item["target"]["block_type"] == "article_title"
        )
        synthesis_before = preserved_before[ProductionArtifactStage.SYNTHESIS]
        assert title_mapping["target"]["artifact_id"] == str(synthesis_before.id)
        assert title_mapping["target"]["revision_channel"] == {
            "family": "synthesis",
            "operations": ["rewrite_title"],
        }
        small_finding = next(
            item
            for item in before_inspection["diagnostics"]["items"]
            if item["code"] == "block_too_small" and item["target"]["block_key"] == diagram_key
        )
        assert small_finding["class"].value == "deterministic"
        old_page = diagram_mapping["locations"][0]["page"]
        old_png = await inspection.page_png(old_hash, old_page, PdfInspectionProfile.PREVIEW)
        assert await _publication_render_ids(scenario) == initial_render_ids
        assert await _model_run_ids(scenario) == pipeline_model_run_ids
        old_png_path = tmp_path / "before.png"
        old_png_path.write_bytes(old_png.content)
        assert old_png.content.startswith(b"\x89PNG")

        revision_service = ProductionEditorialEnrichmentRevisionService(
            enrichment_service=ProductionEditorialEnrichmentService(
                uow_factory=scenario.uow_factory,
                artifact_store=scenario.artifact_store,
                model_gateway=scenario.model,
                editorial_enrichment_service=EditorialEnrichmentService(
                    scenario.uow_factory, scenario.artifact_store
                ),
                media_asset_store=media_asset_store,
                diagram_compiler=scenario.diagram_compiler,
                chart_compiler=AnalyticChartRenderer(),
                resource_search_enabled=False,
            ),
            persistence_service=EditorialEnrichmentService(
                scenario.uow_factory, scenario.artifact_store
            ),
            publication_render_service=render_service,
        )
        base_bytes = await scenario.artifact_store.read_bytes(
            enrichment_before.canonical_blob_id  # type: ignore[arg-type]
        )
        base_sha256 = hashlib.sha256(base_bytes).hexdigest()
        correction = TargetedEditorialRevisionRequest(
            subject_id=run.subject_id,
            base_artifact_id=enrichment_before.id,
            base_version=enrichment_before.version,
            base_input_hash=enrichment_before.input_hash,
            base_canonical_sha256=base_sha256,
            before_hash=base_sha256,
            target=TargetedEditorialRevisionTarget(
                kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
                element_kind=EditorialEnrichmentElementKind.DIAGRAM,
                element_key=diagram_key,
            ),
            operation=TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            instruction="Delete the measured undersized diagram.",
            mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
            requester="e2-integration-test",
        )
        preview = await revision_service.preview_targeted(correction)
        assert preview.expected_model_calls == 0
        assert preview.stages_preserved == tuple(stage.value for stage in stages_to_preserve)
        assert preview.stages_invalidated == (ProductionArtifactStage.PUBLICATION.value,)
        assert preview.target_path == f"editorial_enrichment.diagram[{diagram_key}]"

        calls_before_apply = len(scenario.model.provider_calls)
        model_runs_before_apply = await _model_run_ids(scenario)
        corrected = await revision_service.revise_targeted(correction)
        assert corrected.artifact is not None and corrected.publication_artifact is not None
        assert corrected.revision is not None
        assert corrected.artifact.model_run_id is None
        assert (
            len(scenario.model.provider_calls) - calls_before_apply == preview.expected_model_calls
        )
        assert await _model_run_ids(scenario) == model_runs_before_apply
        assert compiler.compile_calls == 2
        assert compiler.query_calls == 2

        enrichment_after = corrected.artifact
        publication_after = corrected.publication_artifact
        assert enrichment_after.id != enrichment_before.id
        assert publication_after.id != publication_before.id
        assert enrichment_after.version > enrichment_before.version
        assert publication_after.version > publication_before.version
        enrichment_payload_after = await scenario.artifact_store.read_json(
            enrichment_after.canonical_blob_id  # type: ignore[arg-type]
        )
        enrichment_canonical_sha_after = hashlib.sha256(
            await scenario.artifact_store.read_bytes(
                enrichment_after.canonical_blob_id  # type: ignore[arg-type]
            )
        ).hexdigest()
        publication_canonical_sha_after = hashlib.sha256(
            await scenario.artifact_store.read_bytes(
                publication_after.canonical_blob_id  # type: ignore[arg-type]
            )
        ).hexdigest()
        assert enrichment_canonical_sha_after != enrichment_canonical_sha_before
        assert publication_canonical_sha_after != publication_canonical_sha_before
        canonical_enrichment_after = editorial_enrichment_from_json(enrichment_payload_after)
        assert canonical_enrichment_after.diagrams == ()
        assert canonical_enrichment_after.tables == canonical_enrichment_before.tables
        table_after = next(
            item
            for item in enrichment_payload_after["tables"]
            if item["key"] == canonical_enrichment_before.tables[0].key
        )
        assert table_after == table_before

        async with scenario.uow_factory() as uow:
            preserved_after = {
                stage: await uow.production_artifacts.get_current(run.id, stage.value)
                for stage in stages_to_preserve
            }
        assert {
            stage: _current_identity(artifact) for stage, artifact in preserved_before.items()
        } == {stage: _current_identity(artifact) for stage, artifact in preserved_after.items()}

        new_render = await render_service.render_pdf(publication_after.id)
        assert compiler.compile_calls == 2
        assert compiler.query_calls == 2
        assert await _model_run_ids(scenario) == model_runs_before_apply
        assert new_render.output_sha256 is not None
        assert new_render.output_sha256 != old_hash
        render_ids_after = await _publication_render_ids(scenario)
        new_render_id = str(next(iter(render_ids_after - initial_render_ids)))
        inspected_after_render_data = await input_inspection.render_data(new_render_id)
        assert inspected_after_render_data["availability"] == "ready"
        assert diagram_key not in json.dumps(inspected_after_render_data["json"])
        assert canonical_enrichment_before.tables[0].key in json.dumps(
            inspected_after_render_data["json"]
        )
        canonical_document_after = await scenario.artifact_store.read_json(
            publication_after.canonical_blob_id  # type: ignore[arg-type]
        )
        assert diagram_key not in json.dumps(canonical_document_after)
        assert canonical_enrichment_before.tables[0].key in json.dumps(canonical_document_after)
        after_inspection = await inspection.metadata(new_render.output_sha256)
        assert after_inspection["pdf"]["sha256"] == new_render.output_sha256
        assert after_inspection["pdf"]["page_count"] >= 1
        assert not any(
            item["code"] == "block_too_small"
            and item["target"] is not None
            and item["target"].get("block_key") == diagram_key
            for item in after_inspection["diagnostics"]["items"]
        )
        table_mapping_after = next(
            item
            for item in after_inspection["mapping"]["items"]
            if item["target"]["block_key"] == canonical_enrichment_after.tables[0].key
        )
        assert (
            table_mapping_after["target"]["block_key"]
            == table_mapping_before["target"]["block_key"]
        )
        assert table_mapping_after["target"]["artifact_id"] == str(enrichment_after.id)
        assert table_mapping_after["target_id"] != table_mapping_before["target_id"]
        assert table_mapping_after["locations"]
        new_png = await inspection.page_png(
            new_render.output_sha256,
            table_mapping_after["locations"][0]["page"],
            PdfInspectionProfile.PREVIEW,
        )
        new_png_path = tmp_path / "after.png"
        new_png_path.write_bytes(new_png.content)
        assert new_png.content.startswith(b"\x89PNG")

        old_again = await inspection.metadata(old_hash)
        assert old_again["pdf"]["sha256"] == old_hash
        old_png_again = await inspection.page_png(old_hash, old_page, PdfInspectionProfile.PREVIEW)
        assert old_png_again.content == old_png.content
        assert old_png_path.is_file() and new_png_path.is_file()

        async with scenario.uow_factory() as uow:
            artifact_ids_before_stale = {
                item.id for item in await uow.production_artifacts.list_for_run(run.id)
            }
        stale_request = replace(correction, instruction="Stale-fence replay with a new identity.")
        with pytest.raises(TargetedEditorialRevisionConflictError):
            await revision_service.revise_targeted(stale_request)
        async with scenario.uow_factory() as uow:
            artifact_ids_after_stale = {
                item.id for item in await uow.production_artifacts.list_for_run(run.id)
            }
        assert artifact_ids_after_stale == artifact_ids_before_stale
        assert (
            len(scenario.model.provider_calls) - calls_before_apply == preview.expected_model_calls
        )
        proof = {
            "scenario": "S3 delete diagram + S5 rerender reuse + S4 stale fence",
            "canonical_target": {
                "diagram_block_key": diagram_key,
                "table_block_key": canonical_enrichment_before.tables[0].key,
                "table_content_equal_before_after": (
                    table_after == table_before
                    and canonical_enrichment_after.tables == canonical_enrichment_before.tables
                ),
                "table_target_id_changes_with_artifact_version": (
                    table_mapping_after["target_id"] != table_mapping_before["target_id"]
                ),
            },
            "artifacts": {
                "enrichment_before": _identity_json(enrichment_before),
                "enrichment_after": _identity_json(enrichment_after),
                "enrichment_canonical_sha256_before": enrichment_canonical_sha_before,
                "enrichment_canonical_sha256_after": enrichment_canonical_sha_after,
                "publication_before": _identity_json(publication_before),
                "publication_after": _identity_json(publication_after),
                "publication_canonical_sha256_before": publication_canonical_sha_before,
                "publication_canonical_sha256_after": publication_canonical_sha_after,
                "preserved_stages_before": {
                    stage.value: _identity_json(artifact)
                    for stage, artifact in preserved_before.items()
                },
                "preserved_stages_after": {
                    stage.value: _identity_json(artifact)
                    for stage, artifact in preserved_after.items()
                },
            },
            "pdf": {
                "before_sha256": old_hash,
                "after_sha256": new_render.output_sha256,
                "before_pages": before_inspection["pdf"]["page_count"],
                "after_pages": after_inspection["pdf"]["page_count"],
                "diagram_page_before": old_page,
                "table_page_before": table_mapping_before["locations"][0]["page"],
                "table_page_after": table_mapping_after["locations"][0]["page"],
                "before_png": {
                    "path": old_png_path.name,
                    "sha256": hashlib.sha256(old_png.content).hexdigest(),
                },
                "after_png": {
                    "path": new_png_path.name,
                    "sha256": hashlib.sha256(new_png.content).hexdigest(),
                },
                "old_pdf_still_resolves": old_again["pdf"]["sha256"] == old_hash,
                "undersized_diagnostic_removed": not any(
                    item["code"] == "block_too_small"
                    and item["target"] is not None
                    and item["target"].get("block_key") == diagram_key
                    for item in after_inspection["diagnostics"]["items"]
                ),
            },
            "model_and_render_work": {
                "preview_expected_model_calls": preview.expected_model_calls,
                "actual_provider_call_delta": len(scenario.model.provider_calls)
                - calls_before_apply,
                "model_run_ids_unchanged": await _model_run_ids(scenario)
                == model_runs_before_apply,
                "typst_compile_calls_for_two_distinct_publications": compiler.compile_calls,
                "typst_query_calls_for_two_distinct_publications": compiler.query_calls,
                "rerender_reused_existing_output": new_render.output_sha256
                == after_inspection["pdf"]["sha256"],
            },
            "stale_fence_rejected_without_artifact_writes": (
                artifact_ids_after_stale == artifact_ids_before_stale
            ),
        }
        proof_path = tmp_path / "correction-loop-proof.json"
        proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
        assert proof_path.is_file()
    finally:
        await engine.dispose()
