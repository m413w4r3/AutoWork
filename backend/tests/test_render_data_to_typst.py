from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "render_data_to_typst.py"


def _load_renderer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_data_to_typst", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renderer: Any = _load_renderer()


def article(*, article_format: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "kind": "article",
        "number": "01",
        "title": "Example article",
        "anchor": "example-article",
        "subject_id": "9f9dfdd6-273c-4eef-b7ad-59bbde44b5b4",
        "article_format": article_format,
        "content_sections": [
            {
                "type": "references",
                "entries": [
                    {
                        "number": 1,
                        "display_date": "2 janvier 2026",
                        "text": "Source explanation",
                        "semantic_spans": [
                            {"style": "semantic-plain", "text": "Source explanation"}
                        ],
                        "source_urls": ["https://example.test/source"],
                    }
                ],
            },
            {
                "type": "chronology",
                "timeline": [
                    {
                        "display_date": "3 janvier 2026",
                        "text": "Observed event",
                        "semantic_spans": [{"style": "semantic-plain", "text": "Observed event"}],
                        "source_refs": [{"number": 1}],
                        "source_urls": [],
                    }
                ],
            },
            {"type": "synthesis", "blocks": [{"type": "paragraph", "text": "Summary."}]},
        ],
    }


def test_references_and_article_chronology_use_bullets_and_rail(tmp_path: Path) -> None:
    builder = renderer.Builder({}, tmp_path, tmp_path)

    _, output = builder.publication(article())

    assert '#ref-entry("2 janvier 2026"' in output
    assert "<a01-ref1>" in output
    assert "#rail[" in output
    assert '#rail-event("3 janvier 2026"' in output
    assert "#rf(1)" in output
    assert "#ev(" not in output
    assert "#set par(spacing: 1.4em)" in output


def test_structured_article_format_renders_overview_and_key_events(tmp_path: Path) -> None:
    article_format = {
        "overview": {
            "victimology": {"text": "Victims in several sectors.", "semantic_spans": None},
            "arsenal": "A compact toolset.",
            "main_objective": "Credential collection.",
        },
        "timeline_key_events": [
            {
                "display_date": "1 janvier 2026",
                "text": "First event",
                "semantic_spans": [{"style": "semantic-plain", "text": "First event"}],
                "source_urls": ["https://example.test/event"],
            }
        ],
    }
    builder = renderer.Builder({}, tmp_path, tmp_path)

    _, output = builder.publication(article(article_format=article_format))

    assert '#overview-item("Victimologie")[Victims in several sectors.]' in output
    assert '#overview-item("Arsenal")[A compact toolset.]' in output
    assert '#overview-item("Objectif principal")[Credential collection.]' in output
    assert '#rail-event("1 janvier 2026"' in output
    assert '#fn("https://example.test/event")' in output


def test_publication_level_article_overview_is_boxed(tmp_path: Path) -> None:
    pub = article()
    pub["article_overview"] = {"victimology": "Victims in several sectors.", "arsenal": None}
    builder = renderer.Builder({}, tmp_path, tmp_path)

    _, output = builder.publication(pub)

    assert "#overview-box[" in output
    assert '#overview-item("Victimologie")[Victims in several sectors.]' in output


def test_brief_body_uses_readable_paragraph_spacing(tmp_path: Path) -> None:
    pub = {
        "kind": "brief",
        "number": "04",
        "title": "Example brief",
        "anchor": "example-brief",
        "article_format": None,
        "content_sections": [
            {"type": "references", "entries": []},
            {"type": "synthesis", "blocks": [{"type": "paragraph", "text": "First paragraph."}]},
        ],
    }
    builder = renderer.Builder({}, tmp_path, tmp_path)

    _, output = builder.publication(pub)

    assert "#set par(spacing: 1.4em)" in output


def test_legacy_overview_groups_fields_across_an_analyst_observation(tmp_path: Path) -> None:
    blocks = [
        {"type": "section_heading", "text": "Vue d'ensemble"},
        {
            "type": "paragraph",
            "text": "Victimologie : Victims.",
            "semantic_spans": [{"style": "semantic-plain", "text": "Victimologie : Victims."}],
        },
        {
            "type": "paragraph",
            "text": "Arsenal : Tools.",
            "semantic_spans": [{"style": "semantic-plain", "text": "Arsenal : Tools."}],
        },
        {
            "type": "analyst_observation",
            "text": "Analyst note.",
            "text_spans": [{"text": "Analyst note.", "technical": False}],
        },
        {
            "type": "paragraph",
            "text": "Objectif principal : Collection.",
            "semantic_spans": [
                {"style": "semantic-plain", "text": "Objectif principal : Collection."}
            ],
        },
        {"type": "section_heading", "text": "Context"},
    ]
    builder = renderer.Builder({}, tmp_path, tmp_path)
    lines: list[str] = []

    builder.synthesis(
        {"blocks": blocks},
        "articles/article01",
        "article01",
        lines,
        structured_overview_rendered=False,
    )
    output = "\n".join(lines)

    assert output.count("#overview-box[") == 1
    assert output.count("#overview-item(") == 3
    assert "== Vue d'ensemble" not in output
    assert "#obs[" in output
    assert output.index('#overview-item("Objectif principal")') < output.index("#obs[")


def test_semantic_colors_short_source_links_and_tight_code_style() -> None:
    markup = renderer.spans_markup(
        [
            {"style": "semantic-malware", "text": "Malware"},
            {"style": "semantic-tool", "text": " Tool"},
            {"style": "semantic-product", "text": " Product"},
        ]
    )

    assert "#semantic-malware[Malware]" in markup
    assert "#semantic-tool[Tool]" in markup
    assert "#semantic-product[Product]" in markup
    assert "#let fn(url) = footnote[#better-link(url)[#url]]" in renderer.MANUEL_TYP
    assert "highlight(fill: luma(190), radius: 2pt)" in renderer.MANUEL_TYP
    assert "#overview-box(" in renderer.MANUEL_TYP or "#let overview-box(" in renderer.MANUEL_TYP
    assert "#tbl-caption(caption)" in renderer.MANUEL_TYP


def test_cover_keeps_the_centered_brand_logo(tmp_path: Path) -> None:
    data = {
        "edition": {
            "country": "Test",
            "bulletin_period": "janvier 2026",
            "bulletin_number": "1",
            "bulletin_identifier": "IR0001",
            "bulletin_title": "Bulletin de veille CTI",
            "country_code": "TT",
            "tlp": "CLEAR",
        },
        "file_ioc_count": 0,
    }

    main = renderer.Builder(data, tmp_path, tmp_path).main_typ([], [])

    assert '#image("UTILS/chap.png", width: 18%)' in main


def test_manual_opinion_renders_ioc_sections_and_hides_candidates(tmp_path: Path) -> None:
    pub = article()
    pub["content_sections"].append(
        {
            "type": "ioc",
            "source_indicators": [
                {
                    "heading": "Fichiers",
                    "items": [
                        {
                            "artifact_type": "hash",
                            "type_label": "Fichiers",
                            "value": {
                                "text": "source-hash-sentinel",
                                "break_chunks": [],
                            },
                        }
                    ],
                }
            ],
            "original_indicators": [
                {
                    "heading": "Domaines",
                    "items": [
                        {
                            "artifact_type": "domain",
                            "type_label": "Domaines",
                            "value": {"text": "original.example", "break_chunks": []},
                        }
                    ],
                }
            ],
            "candidate_indicators": [
                {
                    "heading": "Pistes non confirmées",
                    "items": [{"value": {"text": "candidate.example"}}],
                }
            ],
        }
    )
    pub["content_sections"].append(
        {"type": "analyst_opinion", "blocks": [], "unanchored_observations": []}
    )
    builder = renderer.Builder({}, tmp_path, tmp_path)

    _, output = builder.publication(pub)

    assert pub["subject_id"] not in output
    assert "[F]" not in output
    assert "[E]" not in output
    assert "source-hash-sentinel" in output
    assert "original.example" in output
    assert "candidate.example" not in output
    assert "#ioc-list" not in output
    assert "annexes CSV du dossier de livraison" not in output
    assert output.index('#render-section-heading("IOC")') < output.index(
        '#render-section-heading("IOC originaux")'
    )
    assert "code-font-families" in output


def test_manual_opinion_without_iocs_has_no_ioc_sections(tmp_path: Path) -> None:
    pub = article()
    pub["content_sections"].append(
        {
            "type": "ioc",
            "source_indicators": [],
            "original_indicators": [],
            "candidate_indicators": [
                {"heading": "Pistes non confirmées", "items": [{"value": "hidden-candidate"}]}
            ],
        }
    )
    pub["content_sections"].append(
        {"type": "analyst_opinion", "blocks": [], "unanchored_observations": []}
    )

    _, output = renderer.Builder({}, tmp_path, tmp_path).publication(pub)

    assert '#render-section-heading("IOC")' not in output
    assert '#render-section-heading("IOC originaux")' not in output
    assert "hidden-candidate" not in output


def test_ioc_reference_helpers_are_removed_from_both_renderers() -> None:
    repository_root = _SCRIPT.resolve().parents[2]
    publication_helpers = (
        repository_root / "chpTypst" / "RENDERER" / "publication_helpers.typ"
    ).read_text(encoding="utf-8")
    for renderer_text in (publication_helpers, renderer.MANUEL_TYP):
        assert "annexes CSV du dossier de livraison" not in renderer_text
        assert "render-opinion-ioc-reference" not in renderer_text
        assert "ioc-renvoi" not in renderer_text
    assert "render-opinion-iocs(content_section)" in publication_helpers
    assert "#let render-section-heading(title)" in renderer.MANUEL_TYP


def test_manual_renderer_numbers_only_figures_it_emits(tmp_path: Path) -> None:
    media_dir = tmp_path / "source-media"
    media_dir.mkdir()
    for name in ("first.svg", "second.png", "protocol.svg"):
        (media_dir / name).write_bytes(b"fixture media")

    pub = article()
    pub["content_sections"][1]["derived_timeline_figure"] = {"figure_number": 3}
    pub["content_sections"][2]["blocks"] = [
        {
            "type": "figure",
            "figure_number": 4,
            "media_path": "media/first.svg",
            "caption": "First visible figure",
        },
        {
            "type": "figure",
            "figure_number": 6,
            "media_path": "media/second.png",
            "caption": "Second visible figure",
        },
    ]
    pub["content_sections"].append(
        {
            "type": "analyst_opinion",
            "blocks": [],
            "unanchored_observations": [],
            "unresolved_points": [],
            "protocol_figures": [
                {
                    "type": "diagram",
                    "figure_number": 8,
                    "media_path": "media/protocol.svg",
                    "caption": "Protocol figure",
                }
            ],
        }
    )
    builder = renderer.Builder({}, media_dir, tmp_path / "manual-output")

    _relative_path, output = builder.publication(pub)

    assert "article01_figure1.svg" in output
    assert "article01_figure2.png" in output
    assert "article01_figure3.svg" in output
    assert "article01_figure4." not in output
    assert "article01_figure6." not in output
    assert "article01_figure8." not in output
    assert output.count("#fig(") == 2
    assert output.count("#diag(") == 1
