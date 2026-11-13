from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader

from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.network_protocol_model import render_protocol_model
from cti_app.application.protocol_diagram_projection import (
    PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION,
    compile_protocol_render_result,
    opinion_protocol_figures,
    project_protocol_diagram,
)
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TypstDraftPreviewDocument,
    TypstRenderer,
    load_template_bundle,
)
from cti_app.domain.analyst_opinion import AnalystOpinionStatus, OpinionProtocolFigureV1
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.media_assets import MediaAssetManifest
from cti_app.domain.network_protocol import (
    NetworkExtractionLimitsV1,
    NetworkTransport,
    StreamDirection,
)
from cti_app.domain.protocol_model import (
    AssuranceLevel,
    FieldDefinitionV1,
    FieldStability,
    MessageDefinitionV1,
    ProtocolDiagramKind,
    ProtocolDiagramStatus,
    ProtocolEvidenceLevelV1,
    ProtocolEvidenceRefV1,
    ProtocolModelV1,
    ProtocolRenderResultV1,
    ProtocolStateV1,
    ProtocolTransitionV1,
    ProtocolValidationStatus,
)
from cti_app.domain.publication import (
    PublicationParagraphV1,
    PublicationSectionKind,
    PublicationSectionV1,
)
from cti_app.domain.publication_document import publication_document_text_anchors
from cti_app.domain.semantic_annotation import (
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler, encode_d2_source
from cti_app.infrastructure.database.repositories.investigation import _opinion_from_row
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_edition_typst_rendering import _edition_document
from tests.test_protocol_diagrams import _three_views
from tests.test_publication_builder_v4 import _frontmatter_v6_case
from tests.test_publication_document_v6 import _approved_opinion, _base_v5
from tests.test_typst_compiler_runtime import _build_runtime_workspace

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TYPST_BINARY = os.environ.get("TYPST_BINARY") or shutil.which("typst")
_D2_BINARY = os.environ.get("D2_BINARY") or shutil.which("d2")
_FONT_LOCK = _REPOSITORY_ROOT / "infra" / "typst-fonts.lock"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GENERIC_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 80">'
    '<rect width="320" height="80" fill="#f2f4f7"/>'
    '<text x="8" y="44" font-size="18">Synthetic fixture</text></svg>'
)


def test_dense_protocol_sequence_projection_keeps_compact_numbered_arrows() -> None:
    base = _three_views()[0]
    participant = "CLIENT_ESTABLISHED_" * 6
    message = "[PING] handshake (lws, tls) " + "payload_field_" * 7
    spec = replace(
        base,
        nodes=(replace(base.nodes[0], label=participant), *base.nodes[1:]),
        edges=tuple(replace(base.edges[0], label=message, order=index) for index in range(1, 10)),
    )
    projected = project_protocol_diagram(spec)
    labels = tuple(edge.label or "" for edge in projected.edges)
    source = encode_d2_source(projected).decode()

    assert len(projected.edges) == 9
    assert len(projected.nodes) == len(spec.nodes)
    assert "shape: sequence_diagram" in source
    assert source.count(" -> ") == 9
    assert all(label.startswith("#") for label in labels)
    assert all("[PING]" in label for label in labels)
    assert all(len(label) <= 44 for label in labels)
    assert all(len(node.label) <= 44 for node in projected.nodes)
    assert all(participant not in label for label in labels)
    assert all(message not in label for label in labels)
    assert all("…" in label for label in labels)


def test_sparse_protocol_projection_keeps_full_node_detail_and_field_labels() -> None:
    long_label = ("Long descriptive protocol label preserving every original term " * 3).strip()
    sequence, state_machine, message_format = _three_views()

    projected_sequence = project_protocol_diagram(
        replace(
            sequence,
            nodes=(replace(sequence.nodes[0], label=long_label), *sequence.nodes[1:]),
            edges=(replace(sequence.edges[0], label=long_label), *sequence.edges[1:]),
        )
    )
    projected_state_machine = project_protocol_diagram(
        replace(
            state_machine,
            nodes=(replace(state_machine.nodes[0], label=long_label), *state_machine.nodes[1:]),
            edges=(replace(state_machine.edges[0], label=long_label), *state_machine.edges[1:]),
        )
    )
    projected_message_format = project_protocol_diagram(
        replace(
            message_format,
            fields=(replace(message_format.fields[0], name=long_label), *message_format.fields[1:]),
        )
    )

    assert len(long_label) > 100
    assert long_label in projected_sequence.nodes[0].label
    assert any(long_label in (edge.label or "") for edge in projected_sequence.edges)
    assert long_label in projected_state_machine.nodes[0].label
    assert any(long_label in (edge.label or "") for edge in projected_state_machine.edges)
    assert long_label in projected_message_format.nodes[0].label
    assert all(
        "…" not in label
        for diagram in (
            projected_sequence,
            projected_state_machine,
            projected_message_format,
        )
        for label in (
            *(node.label for node in diagram.nodes),
            *(edge.label or "" for edge in diagram.edges),
        )
    )


class _MemoryBlobStore:
    def __init__(self) -> None:
        self._contents: dict[UUID, bytes] = {}

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        del bucket, mime_type
        blob_id = uuid4()
        self._contents[blob_id] = content
        return blob_id

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self._contents[blob_id]
        if len(content) > max_bytes:
            raise ValueError("test blob exceeds its read limit")
        return content


class _MemoryMediaAssetRepository:
    def __init__(self) -> None:
        self._assets: dict[UUID, MediaAssetManifest] = {}

    async def add_if_absent(self, manifest: MediaAssetManifest) -> MediaAssetManifest:
        return self._assets.setdefault(manifest.asset_id, manifest)

    async def get(self, asset_id: UUID) -> MediaAssetManifest | None:
        return self._assets.get(asset_id)


class _MemoryUnitOfWork:
    def __init__(self, repository: _MemoryMediaAssetRepository) -> None:
        self.media_assets = repository

    async def __aenter__(self) -> _MemoryUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _MemoryUnitOfWorkFactory:
    def __init__(self, repository: _MemoryMediaAssetRepository) -> None:
        self._repository = repository

    def __call__(self) -> _MemoryUnitOfWork:
        return _MemoryUnitOfWork(self._repository)


def _memory_media_store():
    from cti_app.application.media_assets import MediaAssetStore

    blobs = _MemoryBlobStore()
    repository = _MemoryMediaAssetRepository()
    return MediaAssetStore(blobs, _MemoryUnitOfWorkFactory(repository)), blobs


def _typst_binary() -> str:
    if _TYPST_BINARY is None:
        pytest.skip("Typst compiler capability is unavailable")
    result = subprocess.run((_TYPST_BINARY, "--version"), capture_output=True, check=False)
    reported = result.stdout.decode("utf-8", errors="replace").strip()
    assert result.returncode == 0 and reported.startswith("typst 0.15.1"), reported
    return _TYPST_BINARY


def _pdf_text_line_sizes(
    pdf_path: Path,
    *,
    mutool_binary: str,
) -> dict[int, list[tuple[str, float]]]:
    xml_path = pdf_path.with_suffix(".stext.xml")
    _run_blocking_process(
        (mutool_binary, "draw", "-F", "stext", "-o", str(xml_path), str(pdf_path)),
        30,
    )
    root = ET.parse(xml_path).getroot()
    result: dict[int, list[tuple[str, float]]] = {}
    for page_number, page in enumerate(root.findall("page"), start=1):
        lines = result.setdefault(page_number, [])
        for line in page.findall(".//line"):
            text = "".join(char.attrib.get("c", "") for char in line.findall(".//char"))
            for font in line.findall("font"):
                size = font.attrib.get("size")
                if text and size is not None:
                    lines.append((text, float(size)))
    return result


def _render_result() -> ProtocolRenderResultV1:
    return ProtocolRenderResultV1(
        schema_version="ProtocolRenderResultV1",
        model_sha256="a" * 64,
        diagrams=_three_views(),
        factual_statements=("Three views use synthetic passive protocol evidence.",),
    )


def _install_workspace(
    source,
    media_bytes: dict[UUID, bytes],
    workspace_root: Path,
    *,
    entrypoint: str,
) -> None:
    render_data = json.loads(source.render_data_bytes)
    media_files: dict[str, str] = {}
    media_content: dict[str, bytes] = {}
    for reference in source.media_refs:
        content = media_bytes.get(reference.asset_id)
        if content is None:
            if reference.expected_mime_type == "image/svg+xml":
                content = _GENERIC_SVG.encode("utf-8")
            elif reference.expected_mime_type == "image/png":
                content = (_REPOSITORY_ROOT / "chpTypst" / "UTILS" / "chap.png").read_bytes()
            else:
                raise AssertionError(
                    f"Unexpected fixture media type: {reference.expected_mime_type}"
                )
        media_name = Path(reference.media_path).name
        media_content[media_name] = content
        if content.startswith(b"<svg"):
            media_files[media_name] = content.decode("utf-8")
    _build_runtime_workspace(
        _REPOSITORY_ROOT,
        workspace_root,
        render_data=render_data,
        media_files=media_files,
    )
    entrypoint_path = workspace_root / entrypoint
    entrypoint_path.parent.mkdir(parents=True, exist_ok=True)
    entrypoint_path.write_bytes(source.source_bytes)
    media_root = workspace_root / "RENDERER" / "media"
    for name, content in media_content.items():
        (media_root / name).write_bytes(content)
    if entrypoint == "RENDERER/edition.typ":
        (workspace_root / "RENDERER" / "edition-render-data.json").write_bytes(
            source.render_data_bytes
        )


def _compile_pdf(
    *,
    typst_binary: str,
    workspace_root: Path,
    font_root: Path,
    entrypoint: str,
):
    # CI installs the pinned fonts outside the repository and exports FONT_BUNDLE_ROOT.
    font_bundle_root = Path(os.environ.get("FONT_BUNDLE_ROOT") or _REPOSITORY_ROOT / "chpTypst")
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_LOCK)
    font_paths = materialize_font_bundle(font_snapshot, font_root)
    return TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace_root,
            entrypoint_relative_path=entrypoint,
            font_paths=font_paths,
        )
    )


def _run_blocking_process(
    arguments: tuple[str, ...], timeout_seconds: int
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        capture_output=True,
        check=True,
        timeout=timeout_seconds,
    )


def _compile_real_typst_pdf(
    *,
    typst_binary: str,
    workspace_root: Path,
    font_root: Path,
    entrypoint: str,
) -> bytes:
    font_bundle_root = Path(os.environ.get("FONT_BUNDLE_ROOT") or _REPOSITORY_ROOT / "chpTypst")
    font_snapshot = load_font_bundle_snapshot(font_bundle_root, _FONT_LOCK)
    font_paths = materialize_font_bundle(font_snapshot, font_root)
    argv = [typst_binary, "compile", "--root", str(workspace_root)]
    for font_path in font_paths:
        argv.extend(("--font-path", str(font_path)))
    argv.extend(
        (
            "--ignore-system-fonts",
            "--creation-timestamp",
            "0",
            str(workspace_root / entrypoint),
            str(workspace_root / "output.pdf"),
        )
    )
    _run_blocking_process(tuple(argv), 30)
    return (workspace_root / "output.pdf").read_bytes()


def _persist_and_verify_pdf_outputs(
    output_root: Path,
    rendered,
    article_pdf: bytes,
    edition_pdf: bytes,
) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    by_kind = {item.spec.kind: item for item in rendered}
    for kind in ProtocolDiagramKind:
        preview_path = output_root / f"{kind.value}.png"
        preview_path.write_bytes(by_kind[kind].preview.media_bytes)
        assert preview_path.stat().st_size > len(_PNG_SIGNATURE)

    for name, pdf_content in (("article", article_pdf), ("edition", edition_pdf)):
        pdf_path = output_root / f"synthetic-{name}.pdf"
        pdf_path.write_bytes(pdf_content)
        info = subprocess.run(("pdfinfo", str(pdf_path)), capture_output=True, check=True)
        info_text = info.stdout.decode("utf-8", errors="replace")
        page_count = re.search(r"^Pages:\s+(\d+)$", info_text, re.MULTILINE)
        assert page_count is not None and int(page_count.group(1)) > 0
        extracted = subprocess.run(
            ("pdftotext", str(pdf_path), "-"), capture_output=True, check=True
        )
        pdf_text = " ".join(extracted.stdout.decode("utf-8", errors="replace").split())
        for spec in _three_views():
            assert spec.title in pdf_text
            assert spec.kind.value in pdf_text
        assert "Observation étayée : vert" in pdf_text
        assert "inférence : orange" in pdf_text
        assert "non résolu : gris" in pdf_text
        assert "contredit : rouge" in pdf_text
        raster_prefix = output_root / f"synthetic-{name}-page"
        subprocess.run(
            (
                "pdftoppm",
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-png",
                str(pdf_path),
                str(raster_prefix),
            ),
            capture_output=True,
            check=True,
        )
        assert raster_prefix.with_suffix(".png").stat().st_size > 1024


@pytest.mark.asyncio
@pytest.mark.skipif(_D2_BINARY is None, reason="D2 compiler capability is unavailable")
@pytest.mark.skipif(_TYPST_BINARY is None, reason="Typst compiler capability is unavailable")
async def test_real_article_and_edition_render_all_protocol_figures(
    tmp_path: Path,
) -> None:
    typst_binary = _typst_binary()
    d2 = D2DiagramCompiler(binary=_D2_BINARY or "d2")
    media_store, _blobs = _memory_media_store()
    rendered = await compile_protocol_render_result(
        _render_result(),
        compiler=d2,
        media_asset_store=media_store,
        production_run_id=UUID(int=9901),
    )
    figures = opinion_protocol_figures(rendered)
    format_figure = next(
        figure for figure in figures if figure.kind is ProtocolDiagramKind.MESSAGE_FORMAT
    )
    assert len(format_figure.field_evidence) == 3
    assert format_figure.field_evidence[0].field_id == "length"
    assert format_figure.field_evidence[0].evidence_refs[0].capture_sha256 == "e" * 64
    assert format_figure.field_evidence[0].evidence_refs[0].frame_numbers == (31, 32)
    assert OpinionProtocolFigureV1.from_payload(format_figure.to_payload()) == format_figure
    base = _base_v5()
    opinion, citation_context = _approved_opinion(base)
    approved_with_figures = replace(
        opinion,
        protocol_figures=figures,
        content_sha256="",
    )
    v6 = augment_publication_with_opinion(base, approved_with_figures, citation_context)
    assert v6.provenance.opinion_content_sha256 == approved_with_figures.content_sha256
    assert v6.rendered_opinion is not None
    assert v6.rendered_opinion.protocol_figures == figures

    publication_bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
    article_source = TypstRenderer().render(
        v6,
        publication_bundle,
        document_label="Protocol figure test",
    )
    media_bytes: dict[UUID, bytes] = {}
    for reference in article_source.media_refs:
        try:
            media_bytes[reference.asset_id] = await media_store.read(reference.asset_id)
        except EntityNotFoundError:
            continue
    article_workspace = tmp_path / "protocol-article-workspace"
    _install_workspace(
        article_source,
        media_bytes,
        article_workspace,
        entrypoint="RENDERER/publication.typ",
    )
    article_pdf = _compile_real_typst_pdf(
        typst_binary=typst_binary,
        workspace_root=article_workspace,
        font_root=tmp_path / "article-fonts",
        entrypoint="RENDERER/publication.typ",
    )

    edition_bundle = load_template_bundle(
        _REPOSITORY_ROOT / "chpTypst", manifest_name="edition-renderer-manifest.json"
    )
    edition_source = EditionTypstRenderer().render(
        _edition_document(((1, v6), (2, v6))),
        edition_bundle,
        document_label=(
            "Édition publiée 00000000-0000-4000-8000-000000000030 — release "
            "00000000-0000-4000-8000-000000000099"
        ),
    )
    for source in (article_source, edition_source):
        data = json.loads(source.render_data_bytes)
        sections = data.get("content_sections")
        if sections is None:
            sections = [
                section
                for publication in data["publications"]
                for section in publication["content_sections"]
            ]
        projected_figures = [
            figure for section in sections for figure in section.get("protocol_figures", ())
        ]
        assert projected_figures
        assert {figure["pin_sha256"] for figure in projected_figures} == {
            item.pin_sha256 for item in rendered
        }
        assert {figure["spec_sha256"] for figure in projected_figures} == {
            item.spec_sha256 for item in rendered
        }
        projected_format = next(
            figure for figure in projected_figures if figure["key"] == "protocol-message_format"
        )
        assert projected_format["field_evidence"][0]["field_id"] == "length"
        assert projected_format["field_evidence"][0]["evidence_refs"][0]["frame_numbers"] == [
            31,
            32,
        ]
    edition_media = {
        reference.asset_id: await media_store.read(reference.asset_id)
        for reference in edition_source.media_refs
        if await media_store.get(reference.asset_id) is not None
    }
    edition_workspace = tmp_path / "protocol-edition-workspace"
    _install_workspace(
        edition_source,
        edition_media,
        edition_workspace,
        entrypoint="RENDERER/edition.typ",
    )
    edition_pdf = _compile_real_typst_pdf(
        typst_binary=typst_binary,
        workspace_root=edition_workspace,
        font_root=tmp_path / "edition-fonts",
        entrypoint="RENDERER/edition.typ",
    )

    _persist_and_verify_pdf_outputs(
        tmp_path / "protocol-typst-test",
        rendered,
        article_pdf,
        edition_pdf,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(_D2_BINARY is None, reason="D2 compiler capability is unavailable")
@pytest.mark.skipif(_TYPST_BINARY is None, reason="Typst compiler capability is unavailable")
async def test_documented_protocol_model_renders_all_three_views_in_opinion_pdf(
    tmp_path: Path,
) -> None:
    long_sequence_label = (
        "WebSocket establishment and secure transport negotiation with documented "
        "request and response metadata preserved end to end"
    )
    long_state_label = (
        "Waiting for the authenticated server acknowledgement while negotiated "
        "transport identity and session readiness remain pending"
    )
    long_transition_label = (
        "Validate the negotiated client identity and acknowledge the authenticated "
        "session before accepting subsequent protocol commands"
    )
    long_field_label = (
        "Client capability bitmap describing negotiated protocol extensions, "
        "transport requirements, session flags, and compatibility metadata"
    )
    assert all(
        len(label) > 100
        for label in (
            long_sequence_label,
            long_state_label,
            long_transition_label,
            long_field_label,
        )
    )

    model_id = UUID(int=9910)
    evidence = ProtocolEvidenceRefV1(
        source="documented:claim:handshake-facts",
        documented_ref_kind="claim",
        documented_ref_id="handshake",
        documented_source_sha256="b" * 64,
    )
    model = ProtocolModelV1(
        schema_version="ProtocolModelV1",
        subject_id=str(_base_v5().subject_id),
        investigation_id=model_id,
        family=None,
        cluster=None,
        family_assurance=AssuranceLevel.UNCONFIRMED,
        sample_sha256s=(),
        capture_sha256s=(),
        report_ids=(),
        model_version="documented-protocol-model.v1",
        protocol="Sample handshake",
        transport=NetworkTransport.UNKNOWN,
        endpoints=(),
        client_server_orientation="unresolved",
        infection_state=None,
        conversations=(),
        messages=(
            MessageDefinitionV1(
                message_id="hello",
                name="ClientHello",
                ordinal=1,
                directions=(StreamDirection.UNRESOLVED,),
                min_length=None,
                max_length=None,
                field_ids=(),
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
            MessageDefinitionV1(
                message_id="server-hello",
                name="ServerHello",
                ordinal=2,
                directions=(StreamDirection.UNRESOLVED,),
                min_length=None,
                max_length=None,
                field_ids=(),
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
            MessageDefinitionV1(
                message_id="authenticate",
                name="Authenticate",
                ordinal=3,
                directions=(StreamDirection.UNRESOLVED,),
                min_length=None,
                max_length=None,
                field_ids=(),
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
            MessageDefinitionV1(
                message_id="ready",
                name="ReadyNotice",
                ordinal=4,
                directions=(StreamDirection.UNRESOLVED,),
                min_length=None,
                max_length=None,
                field_ids=(),
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
        ),
        fields=(),
        transitions=(
            ProtocolTransitionV1(
                transition_id="new-to-negotiating",
                from_state="new",
                to_state="negotiating",
                message_id="hello",
                trigger="ClientHello",
                hypotheses=(),
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
            ProtocolTransitionV1(
                transition_id="negotiating-to-ready",
                from_state="negotiating",
                to_state="ready",
                message_id="authenticate",
                trigger="Authenticate",
                hypotheses=(),
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
        ),
        function_provenance=(),
        validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
        verification_findings=(),
        test_plan=(),
        limits=NetworkExtractionLimitsV1(),
        limitations=("Documenté, non observé.",),
        extractor_versions=(("protocol-document", "1"),),
        states=(
            ProtocolStateV1(
                state_id="new",
                name="New",
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
            ProtocolStateV1(
                state_id="ready",
                name="Ready",
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
            ProtocolStateV1(
                state_id="negotiating",
                name="Negotiating",
                validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
                evidence=(evidence,),
            ),
        ),
        documentary_evidence=(evidence,),
    )
    message_ids = tuple(f"message-{index:02d}" for index in range(1, 18))
    field_ids_by_message = {
        message_id: tuple(
            f"field-{field_index:02d}"
            for field_index in range(1, 23)
            if (field_index - 1) % len(message_ids) == message_ids.index(message_id)
        )
        for message_id in message_ids
    }
    messages = tuple(
        MessageDefinitionV1(
            message_id=message_id,
            name=(
                "01 WS/TLS (sans jeton)"
                if index == 1
                else "02 [LAL_P] Profil"
                if index == 2
                else "03 [PING] Sonde"
                if index == 3
                else long_sequence_label
                if index == 4
                else f"Message {index:02d}"
            ),
            ordinal=index,
            directions=(
                (StreamDirection.UNRESOLVED,)
                if index == len(message_ids)
                else (
                    StreamDirection.ORIGINATOR_TO_RESPONDER
                    if index % 2
                    else StreamDirection.RESPONDER_TO_ORIGINATOR,
                )
            ),
            min_length=None,
            max_length=None,
            field_ids=field_ids_by_message[message_id],
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=(evidence,),
        )
        for index, message_id in enumerate(message_ids, 1)
    )
    fields = tuple(
        FieldDefinitionV1(
            field_id=f"field-{index:02d}",
            message_id=message_ids[(index - 1) % len(message_ids)],
            name=long_field_label if index == 1 else f"Field {index:02d}",
            offset=None,
            length=None,
            endianness="unknown",
            encoding=None,
            condition=None,
            source="documented protocol model",
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            stability=FieldStability.UNKNOWN,
            evidence=(evidence,),
        )
        for index in range(1, 23)
    )
    state_ids = tuple(f"state-{index:02d}" for index in range(1, 10))
    states = tuple(
        ProtocolStateV1(
            state_id=state_id,
            name=long_state_label if index == 1 else f"State {index:02d}",
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=(evidence,),
        )
        for index, state_id in enumerate(state_ids, 1)
    )
    transitions = tuple(
        ProtocolTransitionV1(
            transition_id=f"transition-{index:02d}",
            from_state=state_ids[(index - 1) % len(state_ids)],
            to_state=state_ids[index % len(state_ids)],
            message_id=message_ids[index - 1],
            trigger=(
                "(sans jeton)"
                if index == 1
                else "[LAL_P] Profile negotiation"
                if index == 2
                else "[PING] Heartbeat"
                if index == 3
                else "Message 04"
                if index == 4
                else " "
                if index == 5
                else long_transition_label
                if index == 6
                else f"Message {index:02d}"
            ),
            hypotheses=(),
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=(evidence,),
        )
        for index in range(1, 16)
    )
    model = replace(model, messages=messages, fields=fields, states=states, transitions=transitions)
    result = render_protocol_model(model)
    assert len(result.diagrams) == 3
    sequence = result.diagrams[0]
    assert sequence.status is ProtocolDiagramStatus.READY
    assert len(sequence.nodes) == 2
    assert tuple(node.label for node in sequence.nodes) == ("Initiateur", "Répondeur")
    assert all(node.role == "documented_participant" for node in sequence.nodes)
    assert all(
        node.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED
        and node.evidence_level is ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
        for node in sequence.nodes
    )
    assert len(sequence.edges) == 16
    assert tuple(edge.order for edge in sequence.edges) == tuple(range(1, 17))
    assert sequence.edges[0].label == "WS/TLS"
    assert sequence.edges[1].label == "[LAL_P] Profil"
    assert sequence.edges[2].label == "[PING] Sonde"
    assert all(
        edge.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED
        and edge.evidence_level is ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
        and edge.evidence == (evidence,)
        and edge.frame_numbers == ()
        for edge in sequence.edges
    )
    assert (sequence.edges[0].source_id, sequence.edges[0].target_id) == (
        "documented-originator",
        "documented-responder",
    )
    assert (sequence.edges[1].source_id, sequence.edges[1].target_id) == (
        "documented-responder",
        "documented-originator",
    )
    assert any(
        "Messages sans sens établi, non représentés par une flèche" in item
        and "17 Message 17" in item
        for item in sequence.limitations
    )
    state_labels = tuple(edge.label for edge in result.diagrams[1].edges)
    assert state_labels[:6] == (
        "WS/TLS",
        "[LAL_P]",
        "[PING]",
        "Message 04",
        "Message 05",
        "Message 06",
    )
    assert all(":" not in label and "(sans jeton)" not in label for label in state_labels)

    projected_sequence = project_protocol_diagram(sequence)
    sequence_source = encode_d2_source(projected_sequence).decode("utf-8")
    assert len(projected_sequence.nodes) == 2
    assert len(projected_sequence.edges) == 16
    assert sequence_source.count(" -> ") == 16
    assert "#1 WS/TLS" in sequence_source
    assert "#2 [LAL_P] Profil" in sequence_source
    assert "#3 [PING] Sonde" in sequence_source
    assert "(sans jeton) 01" not in sequence_source
    assert "documented-message" not in sequence_source

    ambiguous_messages = tuple(
        replace(
            item,
            directions=(
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                StreamDirection.RESPONDER_TO_ORIGINATOR,
            ),
        )
        if item.ordinal == 16
        else item
        for item in model.messages
    )
    ambiguous_sequence = render_protocol_model(
        replace(model, messages=ambiguous_messages)
    ).diagrams[0]
    assert len(ambiguous_sequence.edges) == 15
    assert all(edge.order != 16 for edge in ambiguous_sequence.edges)
    assert any(
        "16 Message 16" in item
        and "Messages sans sens établi, non représentés par une flèche" in item
        for item in ambiguous_sequence.limitations
    )

    c2_sequence = render_protocol_model(replace(model, protocol="Art01 C2")).diagrams[0]
    assert tuple(node.label for node in c2_sequence.nodes) == (
        "Implant (client)",
        "Serveur C2",
    )
    assert len(result.diagrams[1].nodes) == 9
    assert len(result.diagrams[1].edges) == 15
    assert len(result.diagrams[2].fields) == 22
    assert all("documenté, non observé" not in item.title.casefold() for item in result.diagrams)
    assert all(
        not item.edges or all(not edge.frame_numbers for edge in item.edges)
        for item in result.diagrams
    )

    media_store, _blobs = _memory_media_store()
    rendered = await compile_protocol_render_result(
        result,
        compiler=D2DiagramCompiler(binary=_D2_BINARY or "d2"),
        media_asset_store=media_store,
        production_run_id=UUID(int=9911),
    )
    figures = opinion_protocol_figures(rendered)
    assert {item.kind for item in figures} == set(ProtocolDiagramKind)
    sequence_figure = next(item for item in figures if item.kind is ProtocolDiagramKind.SEQUENCE)
    assert "Messages sans sens établi, non représentés par une flèche" in sequence_figure.caption
    sequence_svg = await media_store.read(sequence_figure.asset_id)
    view_box = re.search(rb'viewBox="[-0-9.]+ [-0-9.]+ ([0-9.]+) ([0-9.]+)"', sequence_svg)
    assert view_box is not None
    # The dense sequence is split into two native D2 lifeline panels to fit the page.
    assert float(view_box.group(1)) / float(view_box.group(2)) >= 0.45
    assert sequence_svg.count(b"<text") >= 17
    for figure in figures:
        svg = await media_store.read(figure.asset_id)
        view_box = re.search(rb'viewBox="[-0-9.]+ [-0-9.]+ ([0-9.]+) ([0-9.]+)"', svg)
        assert view_box is not None
        ratio = float(view_box.group(1)) / float(view_box.group(2))
        assert ratio >= 0.3, f"{figure.kind.value} SVG aspect ratio is {ratio:.3f}"
    assert all("ARTICLE_DOCUMENTED" in item.caption for item in figures)
    assert all(
        "offsets" in item.caption.casefold() or "valeurs" in item.caption.casefold()
        for item in figures
    )
    assert all(
        item.compiler_policy_version == PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION for item in figures
    )
    legacy_sequence = replace(
        figures[0],
        caption=(
            "Séquence de négociation. "
            "This view comes only from cited written facts. "
            "No network traffic, frames, offsets, or byte values were observed."
        ),
        compiler_policy_version="diagram-d2-svg-v8-chapsvision",
    )
    format_figure = next(
        item for item in figures if item.kind is ProtocolDiagramKind.MESSAGE_FORMAT
    )
    assert OpinionProtocolFigureV1.from_payload(format_figure.to_payload()) == format_figure
    format_view = next(
        item for item in rendered if item.spec.kind is ProtocolDiagramKind.MESSAGE_FORMAT
    )
    assert len(format_view.diagram.nodes) == 22
    assert format_view.diagram.nodes[0].label.startswith("#01 ")
    assert "offsets" in (format_view.diagram.caption or "").casefold()
    state_view = next(
        item for item in rendered if item.spec.kind is ProtocolDiagramKind.STATE_MACHINE
    )
    assert len(state_view.diagram.nodes) == 9
    assert len(state_view.diagram.edges) == 15
    assert all("Message 01 Message 01" not in node.label for node in state_view.diagram.nodes)

    base, *_case = _frontmatter_v6_case()
    article_section = PublicationSectionV1(
        kind=PublicationSectionKind.TECHNICAL,
        heading="Analyse complémentaire",
        paragraphs=(
            PublicationParagraphV1(
                text="La source documente un fait utile à la vérification.",
                evidence_refs=base.document.lead[0].evidence_refs,
            ),
        ),
    )
    article_document = replace(base.document, sections=(*base.document.sections, article_section))
    base = replace(
        base,
        document=article_document,
        semantic_text=SemanticTextV1(
            schema_version=base.semantic_text.schema_version,
            policy_version=base.semantic_text.policy_version,
            paragraphs=tuple(
                SemanticParagraphV1(
                    anchor=anchor,
                    spans=(SemanticTextSpanV1(SemanticRole.TEXT, text),),
                )
                for anchor, text in publication_document_text_anchors(
                    article_document,
                    references=base.references,
                ).items()
            ),
        ),
    )
    opinion, citation_context = _approved_opinion(base)
    long_hex = "AA" * 48
    yara_rule = (
        'rule synthetic_endpoint { strings: $ip = "203.0.113[.]94" $hex = { '
        + long_hex
        + " } condition: $ip and $hex }"
    )
    code_display_block = replace(
        opinion.blocks[0],
        text=(f"Adresse refangée à l'affichage : 203.0.113[.]94.\n```yara\n{yara_rule}\n```"),
    )
    opinion = replace(opinion, blocks=(code_display_block, *opinion.blocks[1:]), content_sha256="")
    opinion_with_figures = replace(opinion, protocol_figures=figures, content_sha256="")
    publication = augment_publication_with_opinion(base, opinion_with_figures, citation_context)
    publication_bundle = load_template_bundle(_REPOSITORY_ROOT / "chpTypst")
    source = TypstRenderer().render(
        publication,
        publication_bundle,
        document_label="Documented protocol test",
    )
    stale_publication = augment_publication_with_opinion(
        base,
        replace(opinion, protocol_figures=(legacy_sequence, *figures[1:]), content_sha256=""),
        citation_context,
    )
    stale_publication_source = TypstRenderer().render(
        stale_publication,
        publication_bundle,
        document_label="Documented protocol stale test",
    )
    assert "Rendu hérité".encode() not in stale_publication_source.render_data_bytes
    detached_publication = augment_publication_with_opinion(
        base,
        replace(opinion, protocol_figures=(), content_sha256=""),
        citation_context,
    )
    detached_source = TypstRenderer().render(
        detached_publication,
        publication_bundle,
        document_label="Documented protocol detached test",
    )
    detached_model = json.loads(detached_source.render_data_bytes)
    assert all(
        not section.get("protocol_figures") for section in detached_model["content_sections"]
    )
    assert {figure.asset_id for figure in figures}.isdisjoint(
        {reference.asset_id for reference in detached_source.media_refs}
    )
    media_bytes: dict[UUID, bytes] = {}
    for reference in source.media_refs:
        try:
            media_bytes[reference.asset_id] = await media_store.read(reference.asset_id)
        except EntityNotFoundError:
            continue
    workspace = tmp_path / "documented-protocol-workspace"
    _install_workspace(
        source,
        media_bytes,
        workspace,
        entrypoint="RENDERER/publication.typ",
    )
    pdf = _compile_real_typst_pdf(
        typst_binary=_typst_binary(),
        workspace_root=workspace,
        font_root=tmp_path / "documented-protocol-fonts",
        entrypoint="RENDERER/publication.typ",
    )
    pdf_path = tmp_path / "documented-protocol.pdf"
    pdf_path.write_bytes(pdf)
    process_result = _run_blocking_process(("pdftotext", str(pdf_path), "-"), 30)
    extracted_text = process_result.stdout.decode("utf-8", errors="replace")
    extracted_text = " ".join(extracted_text.split()).casefold().replace("\u2019", "'")
    assert "note de l'analyste" in extracted_text
    assert "1.1 apports de l'analyse" in extracted_text
    assert extracted_text.count("article_documented") == 0
    assert extracted_text.count("niveau de preuve") == 3
    # One occurrence in each figure caption (evidence caveat) plus one in each colour legend.
    assert extracted_text.count("documenté, non observé") == 6
    assert "voir les niveaux indiqués sur les nœuds et relations" not in extracted_text
    assert all(
        title in extracted_text
        for title in ("séquence réseau", "machine à états", "format de message")
    )
    assert "203.0.113.94" in extracted_text
    assert "203.0.113[.]94" in extracted_text
    assert long_hex.casefold() in "".join(extracted_text.split())

    audit_root = tmp_path / "protocol-typst-audit"
    audit_root.mkdir(parents=True, exist_ok=True)
    publication_pdf_path = audit_root / "publication.pdf"
    publication_pdf_path.write_bytes(pdf)

    preview_sequence = replace(
        figures[0],
        caption=(
            "Séquence du protocole\n"
            "Niveau de preuve : ARTICLE_DOCUMENTED — documenté, non observé. "
            "Aucun flux ni trame observés; offsets et valeurs d'octets inconnus."
        ),
        compiler_policy_version=(
            "diagram-d2-svg-v9-m21-opinion-render+protocol-diagram-projection-v4-m21-opinion-render"
        ),
    )
    preview_opinion = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        reviewer_id=None,
        protocol_figures=(preview_sequence, *figures[1:]),
        content_sha256="",
    )
    preview_source = TypstRenderer().render(
        TypstDraftPreviewDocument(
            base,
            preview_opinion,
            findings=citation_context.findings,
            evidence_observations=citation_context.observations,
        ),
        publication_bundle,
        document_label="Documented protocol preview test",
    )
    preview_media: dict[UUID, bytes] = {}
    for reference in preview_source.media_refs:
        preview_media[reference.asset_id] = await media_store.read(reference.asset_id)
    preview_workspace = tmp_path / "documented-protocol-preview-workspace"
    _install_workspace(
        preview_source,
        preview_media,
        preview_workspace,
        entrypoint="RENDERER/publication.typ",
    )
    preview_pdf = _compile_real_typst_pdf(
        typst_binary=_typst_binary(),
        workspace_root=preview_workspace,
        font_root=tmp_path / "documented-protocol-preview-fonts",
        entrypoint="RENDERER/publication.typ",
    )
    preview_pdf_path = audit_root / "preview.pdf"
    preview_pdf_path.write_bytes(preview_pdf)
    preview_pages = [
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(preview_pdf_path, strict=True).pages
    ]
    preview_text_result = _run_blocking_process(("pdftotext", str(preview_pdf_path), "-"), 30)
    preview_text = " ".join(preview_text_result.stdout.decode("utf-8", errors="replace").split())
    assert re.search(r"\[[FE]\d+\]", preview_text) is None
    assert re.search(r"Références\s*:", preview_text, flags=re.IGNORECASE) is None
    assert "ANNEXE DE TRAÇABILITÉ" not in preview_text
    assert "Références" in preview_text
    assert "provenance des figures d'avis" not in preview_text.casefold()
    assert "A" * 64 in preview_text
    assert (
        re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            preview_text,
            re.IGNORECASE,
        )
        is None
    )
    assert "vérification d'affirmation — étayé" in preview_text.casefold()
    assert "source d'affirmation" in preview_text.casefold()
    sha256_field = re.search(r"\b[a-z][a-z0-9_]*_sha256\b", preview_text, re.IGNORECASE)
    assert sha256_field is None
    text_without_content_hex = re.sub(re.escape(long_hex), "", preview_text, flags=re.IGNORECASE)
    text_without_content_hex = re.sub(
        r"\ba{40,}\b", "", text_without_content_hex, flags=re.IGNORECASE
    )
    assert (
        re.search(
            r"(?<![0-9a-f])[0-9a-f]{40,}(?![0-9a-f])",
            text_without_content_hex,
            re.IGNORECASE,
        )
        is None
    )
    assert "203.0.113.94" in preview_text
    assert "203.0.113[.]94" in preview_text
    missing_labels = [
        label
        for label in (
            long_sequence_label,
            long_state_label,
            long_field_label,
        )
        # Edges carry the trigger token or short message name only, and dense figures abbreviate
        # over-long labels (full text stays in the model): the readable prefix must still reach
        # the PDF.
        if " ".join(label.split()[:2]) not in preview_text
    ]
    assert missing_labels == [], missing_labels
    protocol_figure_pages = tuple(
        page
        for page in preview_pages
        if any(
            title in page
            for title in (
                "Séquence réseau du protocole",
                "Machine à états du protocole",
                "Format de message du protocole",
            )
        )
    )
    assert len(protocol_figure_pages) >= 3
    # Dense figures deliberately abbreviate over-long labels with an ellipsis (this fixture uses
    # 100+ character labels); compact models keep labels short enough to never need it.
    assert all(page.strip() for page in protocol_figure_pages)
    assert all("Rendu hérité" not in page for page in preview_pages)
    assert all("Séquence de négociation" not in page for page in preview_pages)
    assert all(
        "This view comes only from cited written facts" not in page for page in preview_pages
    )

    pdftotext = shutil.which("pdftotext")
    pdftoppm = shutil.which("pdftoppm")
    assert pdftotext is not None and pdftoppm is not None
    for label, path in (("publication", publication_pdf_path), ("preview", preview_pdf_path)):
        extracted = _run_blocking_process((pdftotext, "-layout", str(path), "-"), 30)
        pdf_text = " ".join(extracted.stdout.decode("utf-8", errors="replace").split())
        lowered = pdf_text.casefold()
        expected_proof_levels = 3
        assert pdf_text.count("Niveau de preuve") == expected_proof_levels
        # Evidence caveat in each figure caption plus the colour legend of each figure.
        assert pdf_text.casefold().count("documenté, non observé") == 2 * expected_proof_levels
        assert "ARTICLE_DOCUMENTED" not in pdf_text
        assert "offsets et valeurs d'octets inconnus" in lowered
        assert all(
            title in lowered
            for title in ("séquence réseau", "machine à états", "format de message")
        )
        assert "séquence réseau observée" not in lowered

        output_directory = audit_root / label
        output_directory.mkdir(parents=True, exist_ok=True)
        for existing in output_directory.glob("*.png"):
            existing.unlink()
        raster_prefix = output_directory / "page"
        _run_blocking_process(
            (pdftoppm, "-png", "-r", "110", str(path), str(raster_prefix)),
            30,
        )
        rasters = tuple(output_directory.glob("page-*.png"))
        assert len(rasters) == len(PdfReader(path, strict=True).pages)
        assert all(image.stat().st_size > 1024 for image in rasters)

    publication_pages = [
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(publication_pdf_path, strict=True).pages
    ]
    for figure_number, title in zip(
        (1, 2, 3),
        (
            "Séquence réseau du protocole",
            "Machine à états du protocole",
            "Format de message du protocole",
        ),
        strict=True,
    ):
        figure_page = next(page for page in publication_pages if title in page)
        assert f"Figure {figure_number} :" in figure_page
        assert "documenté, non observé" in figure_page.casefold()
        assert "observation étayée" in figure_page.casefold()
        preview_figure_page = next(page for page in preview_pages if title in page)
        assert f"Figure {figure_number} :" in preview_figure_page
        assert "documenté, non observé" in preview_figure_page.casefold()
        assert "offsets et valeurs d'octets inconnus" in preview_figure_page.casefold()
        assert "observation étayée" in preview_figure_page.casefold()
        assert "documenté, non observé : bleu" in preview_figure_page.casefold()

    mutool_binary = shutil.which("mutool")
    if mutool_binary is not None:
        measured_lines = _pdf_text_line_sizes(
            publication_pdf_path,
            mutool_binary=mutool_binary,
        )
        figure_lines = {
            page_number: lines
            for page_number, lines in measured_lines.items()
            if any(
                title in text
                for text, _size in lines
                for title in (
                    "Séquence réseau du protocole",
                    "Machine à états du protocole",
                    "Format de message du protocole",
                )
            )
        }
        sequence_sizes = [
            size
            for lines in figure_lines.values()
            for text, size in lines
            if re.match(r"^#\d+ Message \d{2}$", text)
        ]
        state_sizes = [
            size
            for lines in figure_lines.values()
            for text, size in lines
            if "State " in text or re.fullmatch(r"Message \d{2} Message \d{2}", text)
        ]
        field_sizes = [
            size
            for lines in figure_lines.values()
            for text, size in lines
            if re.match(r"^#\d{2} Field \d{2}$", text)
        ]
        long_sequence_sizes = [
            size
            for lines in figure_lines.values()
            for text, size in lines
            if "WebSocket" in text or "end to end" in text
        ]
        long_state_sizes = [
            size
            for lines in figure_lines.values()
            for text, size in lines
            if "Waiting" in text or "identity" in text
        ]
        long_field_sizes = [
            size
            for lines in figure_lines.values()
            for text, size in lines
            if "capability" in text or "compatibility metadata" in text
        ]
        assert len(sequence_sizes) >= 12
        assert state_sizes and field_sizes
        assert long_sequence_sizes and long_state_sizes
        assert long_field_sizes
        assert min(sequence_sizes) >= 8.0, f"sequence labels: {min(sequence_sizes):.2f} pt"
        assert min(state_sizes) >= 8.0, f"state labels: {min(state_sizes):.2f} pt"
        assert min(field_sizes) >= 8.0, f"field labels: {min(field_sizes):.2f} pt"
        assert min(long_sequence_sizes) >= 8.0
        assert min(long_state_sizes) >= 8.0
        assert min(long_field_sizes) >= 8.0


def test_protocol_figures_are_persisted_in_the_exact_opinion_version() -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    rendered_payloads = []
    for index, kind in enumerate(ProtocolDiagramKind, start=1):
        rendered_payloads.append(
            {
                "kind": kind.value,
                "model_sha256": "a" * 64,
                "spec_sha256": str(index) * 64,
                "asset_id": str(UUID(int=index)),
                "asset_sha256": "b" * 64,
                "preview_sha256": "c" * 64,
                "pin_sha256": "d" * 64,
                "compiler_name": "d2",
                "compiler_version": "0.9.0",
                "compiler_policy_version": "protocol-test-v1",
                "caption": f"{kind.value} synthetic view with status legend",
            }
        )
    figures = tuple(OpinionProtocolFigureV1.from_payload(item) for item in rendered_payloads)
    base = _base_v5()
    opinion, _context = _approved_opinion(base)
    with_figures = replace(opinion, protocol_figures=figures, content_sha256="")
    row = SimpleNamespace(
        opinion_id=with_figures.id,
        version=with_figures.version,
        investigation_id=with_figures.investigation_id,
        content=with_figures.to_payload(),
        content_sha256=with_figures.content_sha256,
        status=with_figures.status.value,
    )

    loaded = _opinion_from_row(row)

    assert loaded.protocol_figures == figures
    assert loaded.content_sha256 == with_figures.content_sha256
    assert "protocol_figures" not in opinion.content_payload()
    assert with_figures.content_payload()["protocol_figures"] == [
        figure.to_payload() for figure in figures
    ]
