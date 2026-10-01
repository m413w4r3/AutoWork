"""Compile a two-publication edition with the production Typst renderer."""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.typst_compilation import load_font_bundle_snapshot
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_render_execution import TypstRenderExecutor
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.discovery import SourceRole
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.publication import (
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationParagraphV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler

_CHP_TYPST_ROOT, _FONT_BUNDLE_ROOT, _FONT_LOCK_PATH = typst_bundle_paths()


def _publication(subject_id: UUID, title: str) -> PublicationDocumentV4:
    source_id = UUID(int=subject_id.int + 100)
    evidence = (
        PublicationEvidenceRefV1(
            source_document_id=source_id,
            kind=PublicationEvidenceKind.FACT,
            evidence_key="a" * 64,
        ),
    )
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=subject_id,
        publication_language="fr",
        title=title,
        lead=(PublicationParagraphV1(f"Contenu de {title}.", evidence),),
        sections=(),
        timeline=(
            PublicationTimelineEntryV1(None, "août 2026", f"Evenement de {title}", evidence),
        ),
        indicators=(),
        sources=(
            PublicationSourceV1(
                source_document_id=source_id,
                canonical_url=f"https://example.test/{subject_id.int}",
                title=f"Source de {title}",
                publisher="Example",
                published_at=None,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
            ),
        ),
        uncertainties=(PublicationUncertaintyV1(f"Incertitude de {title}", (source_id,)),),
        tables=(),
        diagrams=(),
        figures=(),
    )


def _edition_document() -> EditionDocumentV2:
    first_subject_id = UUID(int=1)
    second_subject_id = UUID(int=2)
    return EditionDocumentV2(
        edition={
            "id": "00000000-0000-0000-0000-000000000020",
            "country": "France",
            "country_code": "FR",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
            "tlp": "GREEN",
            "languages": ["fr"],
            "version": 1,
        },
        publications=(
            EditionPublicationV2(
                position=1,
                subject_id=first_subject_id,
                document=_publication(first_subject_id, "Article smoke A"),
            ),
            EditionPublicationV2(
                position=2,
                subject_id=second_subject_id,
                document=_publication(second_subject_id, "Article smoke B"),
            ),
        ),
    )


async def _compile_edition() -> None:
    bundle = load_template_bundle(
        _CHP_TYPST_ROOT,
        manifest_name="edition-renderer-manifest.json",
    )
    rendered = EditionTypstRenderer().render(_edition_document(), bundle)
    render_data = json.loads(rendered.render_data_bytes)
    if len(render_data["publications"]) < 2:
        raise RuntimeError("edition smoke must contain at least two publications")

    # The smoke document has no media, so the media store is never consulted.
    executor = TypstRenderExecutor(
        media_asset_store=None,  # type: ignore[arg-type]
        compiler=TypstSubprocessCompiler(binary=os.environ.get("TYPST_BINARY", "typst")),
    )
    executed = await executor.execute(
        render_source=rendered,
        template_bundle=bundle,
        font_bundle=load_font_bundle_snapshot(_FONT_BUNDLE_ROOT, _FONT_LOCK_PATH),
        resolved_media={},
    )
    if not executed.compiled_document.content.startswith(b"%PDF-"):
        raise RuntimeError("edition smoke output is not a PDF")


if __name__ == "__main__":
    asyncio.run(_compile_edition())
