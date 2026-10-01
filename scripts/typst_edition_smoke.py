"""Compile a two-publication edition with the production Typst renderer."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import UUID

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import load_template_bundle
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.publication import PublicationParagraphV1
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CHP_TYPST_ROOT = _REPOSITORY_ROOT / "chpTypst"
_FONT_BUNDLE_ROOT = Path(os.environ["FONT_BUNDLE_ROOT"])
_FONT_LOCK_PATH = Path("/usr/local/share/autowork/typst-fonts.lock")


def _publication(subject_id: UUID, title: str) -> PublicationDocumentV4:
    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=subject_id,
        publication_language="fr",
        title=title,
        lead=(PublicationParagraphV1(f"Contenu de {title}.", ()),),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(),
        uncertainties=(),
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

    font_snapshot = load_font_bundle_snapshot(_FONT_BUNDLE_ROOT, _FONT_LOCK_PATH)
    with tempfile.TemporaryDirectory(prefix="autowork-typst-edition-smoke-") as directory:
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        shutil.copytree(_CHP_TYPST_ROOT / "RENDERER", workspace / "RENDERER")
        shutil.copytree(_CHP_TYPST_ROOT / "UTILS", workspace / "UTILS")
        render_data_path = workspace / rendered.render_data_relative_path
        render_data_path.parent.mkdir(parents=True, exist_ok=True)
        render_data_path.write_bytes(rendered.render_data_bytes)

        font_root = Path(directory) / "fonts"
        font_root.mkdir()
        request = TypstCompileRequest(
            workspace_root=workspace,
            entrypoint_relative_path=rendered.entrypoint_relative_path,
            font_paths=materialize_font_bundle(font_snapshot, font_root),
        )
        compiler = TypstSubprocessCompiler(binary=os.environ.get("TYPST_BINARY", "typst"))
        compiled = await compiler.compile(request)
        output_path = workspace / "output.pdf"
        if compiled.media_type != "application/pdf" or not output_path.is_file():
            raise RuntimeError("edition smoke did not produce one PDF")
        if not output_path.read_bytes().startswith(b"%PDF-"):
            raise RuntimeError("edition smoke output is not a PDF")


if __name__ == "__main__":
    asyncio.run(_compile_edition())
