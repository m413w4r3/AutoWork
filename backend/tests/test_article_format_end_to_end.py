from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from cti_app.application.production_normalization import normalize_publication_indicator_value
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
    extraction_evidence_refs_v1,
)
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
)
from cti_app.application.typst_compilation import (
    TypstCompileRequest,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import TypstRenderer, load_template_bundle
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
)
from cti_app.domain.production_synthesis import (
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
)
from cti_app.domain.publication import (
    ArtifactType,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
)
from cti_app.domain.publication_document import (
    PublicationIOCDetailV1,
    PublicationIOCDisposition,
    publication_document_v5_sha256,
)
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.test_article_format import _routed_v8
from tests.test_publication_builder_v4 import _canonical_inputs, _enrichment_with

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
_TEMPLATE_ROOT = _REPO / "chpTypst"
_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "typst" / "article_format"
_L3_OVERVIEW = (
    "Victimologie\u00a0: Les services publics documentés par le rapport.",
    "Arsenal\u00a0: Un chargeur, un module secondaire et une infrastructure distante.",
    "Objectif principal\u00a0: Collecter des informations depuis les systèmes ciblés.",
)
_FORBIDDEN = re.compile(
    r"PROVENANCE DE PUBLICATION|Indicateur\s*:|Relation\s*:|"
    r"contextual indicator|Candidat non confirmé|Pistes non confirmées|"
    r"IOC de l.article|ANNEXE TECHNIQUE",
    re.IGNORECASE,
)


@pytest.fixture(scope="session")
def typst_binary() -> str:
    binary = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    if binary is None:
        pytest.fail("real Typst is required; set TYPST_BINARY to the pinned 0.15.1 binary")
    version = subprocess.run(
        (binary, "--version"), capture_output=True, check=True, text=True, timeout=10
    ).stdout.strip()
    assert version.startswith("typst 0.15.1"), version
    return binary


@pytest.fixture(scope="session")
def template_bundle():
    return load_template_bundle(_TEMPLATE_ROOT)


def _structured_l3_article() -> tuple[Any, Any]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    source = extraction.sources[0]
    added_indicators = tuple(
        ExtractionIndicatorV1(
            value=value,
            artifact_type=artifact_type,
            indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
            context="Valeur synthétique citée dans la source de test.",
            evidence_quote=f"Le rapport cite {value}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source.source_document_id,),
        )
        for artifact_type, value in (
            (ArtifactType.IP, "203.0.113.23"),
            (ArtifactType.URL, "https://updates.example.test/c2"),
            (ArtifactType.DOMAIN, "cdn.example.test"),
            (ArtifactType.HASH, "a" * 64),
            (ArtifactType.FILENAME, "payload.bin"),
            (ArtifactType.FILEPATH, "/usr/lib/payload.bin"),
        )
    )
    extraction = replace(
        extraction,
        sources=(
            replace(source, indicators=(*source.indicators, *added_indicators)),
            *extraction.sources[1:],
        ),
    )
    evidence_refs = extraction_evidence_refs_v1(extraction)
    assert evidence_refs
    primary_ref = evidence_refs[0]
    synthesis = replace(
        synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
        title="[Example actor] Activité et infrastructure documentées",
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.OVERVIEW,
                "Vue d\u2019ensemble",
                tuple(SynthesisParagraphV1(text, (primary_ref,)) for text in _L3_OVERVIEW),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Séquence d\u2019intrusion",
                (
                    SynthesisParagraphV1(
                        "Le rapport décrit une première exécution suivie du "
                        "chargement d\u2019un module.",
                        (primary_ref,),
                    ),
                ),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Infrastructure et protocole",
                (
                    SynthesisParagraphV1(
                        "Les échanges documentés passent par un domaine et une URL distincts.",
                        (primary_ref,),
                    ),
                ),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Persistance et exécution",
                (
                    SynthesisParagraphV1(
                        "Les éléments disponibles décrivent l\u2019exécution du module sans "
                        "établir toutes les étapes ultérieures.",
                        (primary_ref,),
                    ),
                ),
            ),
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                "Pivots de détection",
                (
                    SynthesisParagraphV1(
                        "Les noms de fichiers, l\u2019adresse IP et le domaine constituent des "
                        "pivots à comparer aux preuves locales.",
                        (primary_ref,),
                    ),
                ),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(
                date(2025, 1, 12),
                "12 janvier 2025",
                "Le premier composant apparaît dans la chronologie source.",
                (primary_ref,),
            ),
            SynthesisTimelineEntryV1(
                date(2025, 1, 27),
                "27 janvier 2025",
                "Une communication distante est ensuite documentée.",
                (primary_ref,),
            ),
        ),
    )
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    base = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    source_id = extraction.sources[0].source_document_id
    indicators = []
    for artifact_type, value in (
        (ArtifactType.IP, "203.0.113.23"),
        (ArtifactType.URL, "https://updates.example.test/c2"),
        (ArtifactType.DOMAIN, "cdn.example.test"),
        (ArtifactType.HASH, "a" * 64),
    ):
        normalized = normalize_publication_indicator_value(value, artifact_type)
        indicators.append(
            PublicationIndicatorGroupV1(
                artifact_type,
                (
                    PublicationIndicatorV1(
                        value=value,
                        normalized_value=normalized,
                        artifact_type=artifact_type,
                        source_document_ids=(source_id,),
                    ),
                ),
            )
        )
    file_details = tuple(
        PublicationIOCDetailV1(
            artifact_type=artifact_type,
            value=value,
            normalized_value=normalize_publication_indicator_value(value, artifact_type),
            source_role=extraction.sources[0].role,
            context="Valeur de fichier indiquée dans le rapport exemple.",
            evidence_quote=f"Le rapport cite {value}.",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(source_id,),
            disposition=PublicationIOCDisposition.EXCLUDED,
            disposition_reason="unsupported_artifact_type",
        )
        for artifact_type, value in (
            (ArtifactType.FILENAME, "payload.bin"),
            (ArtifactType.FILEPATH, "/usr/lib/payload.bin"),
        )
    )
    base = replace(
        base,
        document=replace(base.document, indicators=tuple(indicators)),
        ioc_details=file_details,
    )
    return _routed_v8(base, build_article_format_v1(base, extraction)), base


def _build_workspace(
    workspace: Path,
    *,
    template_bundle,
    render_data: dict[str, Any],
    assets: dict[str, bytes] | None = None,
) -> None:
    for template_file in template_bundle.files:
        destination = workspace / template_file.relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(template_file.content)
    renderer_root = workspace / "RENDERER"
    renderer_root.mkdir(parents=True, exist_ok=True)
    (renderer_root / "render-data.json").write_text(
        json.dumps(render_data, ensure_ascii=False), encoding="utf-8"
    )
    media_root = renderer_root / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    media_root.joinpath("attack-chain.svg").write_bytes(
        (_FIXTURE_ROOT / "attack-chain.svg").read_bytes()
    )
    for relative_path, content in (assets or {}).items():
        destination = workspace / "RENDERER" / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _prepare_workspace_paths(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


async def _compile(
    *,
    tmp_path: Path,
    typst_binary: str,
    template_bundle,
    render_data: dict[str, Any],
    assets: dict[str, bytes] | None = None,
) -> bytes:
    workspace = _prepare_workspace_paths(tmp_path)
    _build_workspace(
        workspace,
        template_bundle=template_bundle,
        render_data=render_data,
        assets=assets,
    )
    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", _TEMPLATE_ROOT))
    fonts = load_font_bundle_snapshot(font_root, _REPO / "infra" / "typst-fonts.lock")
    font_paths = materialize_font_bundle(fonts, tmp_path / "font-snapshot")
    compiled = await TypstSubprocessCompiler(binary=typst_binary).compile(
        TypstCompileRequest(
            workspace_root=workspace,
            entrypoint_relative_path="RENDERER/publication.typ",
            font_paths=font_paths,
        )
    )
    assert compiled.media_type == "application/pdf"
    assert compiled.compiler_version == "0.15.1"
    return compiled.content


def _pdf_text(pdf_bytes: bytes, path: Path) -> tuple[str, PdfReader]:
    path.write_bytes(pdf_bytes)
    result = subprocess.run(
        ("pdftotext", "-layout", str(path), "-"),
        capture_output=True,
        check=True,
        text=True,
        timeout=15,
    )
    return result.stdout, PdfReader(path, strict=True)


def _write_pdf_examples(output_root: Path, article_pdf: bytes, brief_pdf: bytes) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "article-riche.pdf").write_bytes(article_pdf)
    (output_root / "breve.pdf").write_bytes(brief_pdf)


@pytest.mark.asyncio
async def test_l3_article_builds_v8_projects_v34_and_compiles_end_to_end(
    tmp_path: Path,
    typst_binary: str,
    template_bundle,
) -> None:
    article, base = _structured_l3_article()
    render_data_bytes = (
        TypstRenderer()
        .render(
            article,
            template_bundle,
            document_label=f"e2e-{publication_document_v5_sha256(base)}",
        )
        .render_data_bytes
    )
    render_data = json.loads(render_data_bytes)
    assert render_data["schema_version"] == "typst-publication-model-v36-m19-polish"
    assert (
        render_data["article_format"]["overview"]["victimology"]["text"]
        == _L3_OVERVIEW[0].split(":", 1)[1].strip()
    )

    pdf_bytes = await _compile(
        tmp_path=tmp_path,
        typst_binary=typst_binary,
        template_bundle=template_bundle,
        render_data=render_data,
    )
    text, _reader = _pdf_text(pdf_bytes, tmp_path / "article-l3-v8.pdf")
    normalized = " ".join(text.split()).casefold().replace("\u2019", "'")
    assert all(
        label.casefold() in normalized
        for label in ("Victimologie", "Arsenal", "Objectif principal")
    )
    assert "12 janvier 2025" in normalized and "27 janvier 2025" in normalized
    assert "example publisher publie" in normalized
    assert "https://example.com/report" in re.sub(r"\s+", "", text)
    assert (
        normalized.index("références")
        < normalized.index("chronologie")
        < normalized.index("vue d'ensemble")
        < normalized.index("synthèse")
        < normalized.rindex("pivots de détection")
        < normalized.index("adresses ip :")
    )
    for category in ("Adresses IP :", "URL :", "Noms de domaine :", "Fichiers :"):
        assert category.casefold() in normalized, category
    assert normalized.count("vue d'ensemble") == 1
    assert _FORBIDDEN.search(text) is None


@pytest.mark.asyncio
async def test_m18_visual_article_and_brief_examples_compile_to_tmp(
    tmp_path: Path,
    typst_binary: str,
    template_bundle,
) -> None:
    rich_article = json.loads((_FIXTURE_ROOT / "article-rich.json").read_text(encoding="utf-8"))
    synthesis = next(
        section for section in rich_article["content_sections"] if section["type"] == "synthesis"
    )
    synthesis["blocks"].extend(
        [
            {"type": "section_heading", "text": "Chaîne d\u2019infection"},
            {
                "type": "diagram",
                "figure_number": 2,
                "title": "Flux entre le chargeur et le module",
                "caption": "Diagramme 1 : Séquence illustrative à partir des événements datés.",
                "media_path": "media/protocol-flow.svg",
                "show_title": True,
            },
            {"type": "section_heading", "text": "Exécution et persistance"},
            {
                "type": "paragraph",
                "text": (
                    "Dans cet exemple de mise en page, le chargeur vérifie "
                    "d\u2019abord la présence des composants attendus, puis transmet le contrôle "
                    "au module secondaire. Le rapport décrit chaque étape séparément et attribue "
                    "les noms de fichiers à des observations précises. Cette présentation permet "
                    "de distinguer une action confirmée d\u2019une hypothèse sur le comportement "
                    "ultérieur. Les valeurs réservées servent à illustrer les zones de texte et "
                    "les retours à la ligne du bulletin."
                ),
            },
            {
                "type": "paragraph",
                "text": (
                    "Les opérateurs déposent le composant dans un répertoire décrit par la source. "
                    "Le lancement dépend ensuite d\u2019un mécanisme documenté, tandis que la "
                    "durée de présence sur l\u2019hôte demeure inconnue. Les détails disponibles "
                    "ne justifient pas de conclure à une tâche planifiée, une clé de démarrage ou "
                    "de service. Un encadré ou un tableau regroupe les faits comparables sans "
                    "étendre la description au-delà des éléments rapportés."
                ),
            },
            {"type": "section_heading", "text": "Infrastructure et protocole C2"},
            {
                "type": "paragraph",
                "text": (
                    "Le domaine et l\u2019adresse du serveur sont présentés comme des "
                    "indicateurs distincts. La frise conserve les deux dates exactes et ne suggère "
                    "aucune activité entre ces observations. Le diagramme résume le passage du "
                    "chargeur au module, puis l\u2019échange réseau cité dans le rapport. Il ne "
                    "représente ni résolution DNS non observée ni connexion supplémentaire."
                ),
            },
            {
                "type": "paragraph",
                "text": (
                    "Dans un bulletin réel, chaque flèche doit correspondre à une preuve attribuée "
                    "et les nœuds doivent reprendre les noms donnés par les analystes. Les "
                    "légendes françaises restent courtes pour rester lisibles à l\u2019impression. "
                    "Les URL sont "
                    "défanguées dans la section IOC et les références restent cliquables. Les "
                    "catégories distinguent l\u2019adresse IP, l\u2019URL, le domaine et les "
                    "fichiers "
                    "de faciliter la reprise des pivots de chasse."
                ),
            },
            {"type": "section_heading", "text": "Pivots de détection"},
            {
                "type": "paragraph",
                "text": (
                    "Les pivots proposés ici se limitent aux valeurs citées : nom de fichier, "
                    "chemin, empreinte et destination réseau. Les équipes peuvent les confronter "
                    "aux journaux disponibles et documenter séparément la fenêtre temporelle. La "
                    "présence d\u2019un indicateur dans une source ne suffit pas à établir une "
                    "sur un hôte. Les observations négatives et les angles morts doivent rester "
                    "visibles dans l\u2019analyse plutôt que d\u2019être comblés par une hypothèse."
                ),
            },
            {
                "type": "paragraph",
                "text": (
                    "La sélection des événements clés vise à faciliter une lecture rapide sans "
                    "remplacer la chronologie canonique. Le texte principal apporte le contexte, "
                    "le tableau compare les composants, la figure source conserve son attribution "
                    "et le diagramme montre une relation technique ciblée. Cette combinaison donne "
                    "un exemple de densité éditoriale utilisable pour vérifier la pagination, les "
                    "légendes et l\u2019équilibre des espaces blancs sur plusieurs pages A4."
                ),
            },
        ]
    )
    protocol_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="180" '
        'viewBox="0 0 640 180">'
        '<rect x="12" y="45" width="170" height="82" rx="8" fill="#E8F0F0" '
        'stroke="#5D7374"/>'
        '<text x="97" y="92" text-anchor="middle" font-family="sans-serif" '
        'font-size="18">Chargeur</text>'
        '<path d="M182 86 H430" stroke="#5D7374" stroke-width="4" marker-end="url(#a)"/>'
        '<rect x="430" y="45" width="195" height="82" rx="8" fill="#F1F1EF" '
        'stroke="#5D7374"/>'
        '<text x="527" y="92" text-anchor="middle" font-family="sans-serif" '
        'font-size="18">Module réseau</text>'
        '<defs><marker id="a" markerWidth="10" markerHeight="8" refX="8" refY="4" '
        'orient="auto"><path d="M0 0 L10 4 L0 8" fill="#5D7374"/></marker></defs></svg>'
    ).encode()
    article_pdf = await _compile(
        tmp_path=tmp_path / "rich-article",
        typst_binary=typst_binary,
        template_bundle=template_bundle,
        render_data=rich_article,
        assets={"media/protocol-flow.svg": protocol_svg},
    )
    article_text, article_reader = _pdf_text(article_pdf, tmp_path / "article-rich.pdf")
    assert len(article_reader.pages) >= 2
    assert "Composants documentés" in article_text
    assert "Flux entre le chargeur et le module" in article_text
    assert "Chronologie" in article_text
    assert "Adresses IP :" in article_text and "Fichiers :" in article_text

    brief = json.loads((_FIXTURE_ROOT / "brief.json").read_text(encoding="utf-8"))
    brief_pdf = await _compile(
        tmp_path=tmp_path / "brief",
        typst_binary=typst_binary,
        template_bundle=template_bundle,
        render_data=brief,
    )
    brief_text, _brief_reader = _pdf_text(brief_pdf, tmp_path / "brief-example.pdf")
    assert "Chronologie" in brief_text and "8 septembre 2026" in brief_text
    assert "Pistes non confirmées" not in brief_text
    assert "if opinion-publication" not in brief_text
    assert "render-opinion-technical-annex" not in brief_text
    assert _FORBIDDEN.search(brief_text) is None

    output = os.environ.get("M18_LI_ARTIFACT_DIR")
    if output:
        _write_pdf_examples(Path(output), article_pdf, brief_pdf)
