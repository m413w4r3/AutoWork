"""Generate equivalent historical-helper and current-renderer Typst inputs.

This uses only the standard library. The current projection intentionally
models the render-data surface and omits category, number, and overview,
which PublicationDocumentV4 does not carry. It is schema-shaped render data,
not output from the Python TypstRenderer or a canonical publication artifact.
The historical CSV projection intentionally includes only the two retained
test rows: its isNew field has no candidate/discarded status value.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FIXTURE = json.loads((ROOT / "fixture.json").read_text(encoding="utf-8"))
CHP_HELPERS = (
    "/home/nill/perso/work/AutoWork/docs/references/chpTypst/UTILS/helpers.typ"
)
PIPELINE_HELPERS = (
    "/home/nill/perso/work/AutoWork/docs/references/typstPipeline/"
    "pays-YYMM_template/UTILS/helpers.typ"
)


def typst_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def typst_content(value: str) -> str:
    return "[" + value.replace("\\", "\\\\").replace("]", "\\]") + "]"


def write_inputs() -> None:
    (ROOT / "current").mkdir(parents=True, exist_ok=True)
    (ROOT / "historical").mkdir(parents=True, exist_ok=True)
    (ROOT / "historical-pipeline").mkdir(parents=True, exist_ok=True)
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="520" height="150" viewBox="0 0 520 150">
  <rect x="10" y="35" width="180" height="80" rx="8" fill="#e8eef3" stroke="#5d7374" stroke-width="2"/>
  <text x="100" y="82" text-anchor="middle" font-family="sans-serif" font-size="18" fill="#17202a">Fixture input</text>
  <path d="M200 75 H305" stroke="#5d7374" stroke-width="3"/>
  <path d="M305 75 l-12 -8 v16 z" fill="#5d7374"/>
  <rect x="315" y="35" width="190" height="80" rx="8" fill="#f8fafc" stroke="#5d7374" stroke-width="2"/>
  <text x="410" y="82" text-anchor="middle" font-family="sans-serif" font-size="18" fill="#17202a">Fixture output</text>
</svg>
"""
    (ROOT / "current" / "fixture.svg").write_text(svg, encoding="utf-8")
    (ROOT / "historical" / "fixture.svg").write_text(svg, encoding="utf-8")
    (ROOT / "historical-pipeline" / "fixture.svg").write_text(svg, encoding="utf-8")

    statuses = FIXTURE["ioc_statuses"]
    table_rows = [
        [status["value"], status["status"] + " (test only)"]
        for status in statuses
    ]
    reference_section = {
        "type": "references",
        "show_heading": True,
        "entries": [
            {
                "number": 1,
                "display_date": FIXTURE["date"],
                "text": FIXTURE["event_text"],
                "source_urls": [FIXTURE["source_url"]],
                "target_id": "synthetic-source-1",
            }
        ],
        "blocks": [],
    }
    synthesis_section = {
        "type": "synthesis",
        "blocks": [
            {"type": "paragraph", "text": FIXTURE["lead"]},
            {
                "type": "table",
                "title": FIXTURE["table_title"],
                "columns": ["Indicator value", "Evidence level"],
                "column_weights": [1.2, 1.0],
                "rows": table_rows,
                "caption": FIXTURE["table_caption"],
            },
            {
                "type": "chart",
                "title": "Synthetic flow",
                "figure_number": 1,
                "caption": FIXTURE["figure_caption"],
                "media_path": "fixture.svg",
            },
            {"type": "analyst_observation", "text": FIXTURE["analyst_note"]},
        ],
    }

    def indicator_item(value: str, status: str) -> dict[str, object]:
        return {
            "value": value,
            "indicator_status_label": status + " (test only)",
            "relationship_status_label": "Synthetic fixture; no provenance.",
            "source_refs": [],
            "draft_refs": [],
        }

    ioc_section = {
        "type": "ioc",
        "source_indicators": [
            {
                "heading": "Documenté dans l'article",
                "items": [
                    indicator_item(statuses[0]["value"], "Documenté dans l'article"),
                    indicator_item(statuses[1]["value"], "Documenté dans l'article"),
                ],
            },
        ],
        "original_indicators": [],
        "candidate_indicators": [
            {
                "heading": "Piste non confirmée",
                "items": [indicator_item(statuses[2]["value"], "Piste non confirmée")],
            }
        ],
        "omitted_invalid_url_count": 0,
    }
    current_data = {
        "document_label": FIXTURE["render_document_label"],
        "document_header": FIXTURE["render_document_header"],
        "draft_watermark": FIXTURE["render_draft_watermark"],
        "title": FIXTURE["title"],
        "content_sections": [reference_section, synthesis_section, ioc_section],
    }
    (ROOT / "current" / "render-data.json").write_text(
        json.dumps(current_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    overview = ",\n".join(
        "    (" + typst_string(label) + ", " + typst_content(text) + ")"
        for label, text in FIXTURE["overview"]
    )
    table_cells = ["[Indicator value]", "[Evidence level]"]
    for row in table_rows:
        table_cells.extend(typst_content(value) for value in row)
    historical_ioc_lists = "\n".join(
        "  #ioc-list(title: "
        + typst_content(status["status"].title() + " (test only)")
        + ", ips: "
        + typst_content(status["value"])
        + ")"
        for status in statuses
    )

    def article_typst(helper_path: str, imports: str, ioc_block: str, prelude: str = "") -> str:
        return f'''#import {typst_string(helper_path)}: {imports}
{prelude}

// The surrounding page settings match the approved historical main.typ.
#set text(font: "Hanken Grotesk", size: 11pt, lang: "fr")
#set page(paper: "a4", margin: (top: 2.3cm, bottom: 2.3cm, left: 2.2cm, right: 2.2cm))

#article(
  category: {typst_string(FIXTURE["category"])},
  number: {typst_string(FIXTURE["number"])},
  title: {typst_string(FIXTURE["title"])},
  events: (
    ({typst_string(FIXTURE["date"])}, {typst_content(FIXTURE["event_text"])}, {typst_string(FIXTURE["source_url"])}),
  ),
  overview: (
{overview},
  ),
  body: [
    #text({typst_string(FIXTURE["lead"])})
    #parbreak()
    #text(weight: "bold"){typst_content(FIXTURE["table_title"])}
    #parbreak()
    #styled-table(
      (1.2fr, 1fr),
      cell-align: (left, left),
      {", ".join(table_cells)},
    )
    #parbreak()
    #text(fill: gray){typst_content(FIXTURE["table_caption"])}
    #parbreak()
    #figure(image("fixture.svg", width: 55%), caption: {typst_content(FIXTURE["figure_caption"])})
    #noteAnalyste{typst_content(FIXTURE["analyst_note"])}
{ioc_block}
  ],
)
'''

    historical = article_typst(
        CHP_HELPERS,
        "article, noteAnalyste, styled-table, ioc-list",
        historical_ioc_lists,
    )
    (ROOT / "historical" / "article.typ").write_text(historical, encoding="utf-8")

    pipeline_rows = [
        ("0", "ip", statuses[index]["value"])
        for index in (0, 1)
    ]
    pipeline_csv = (
        "[Synthetic actor|M01-T parity fixture — synthetic only|Synthetic publisher|"
        "https://example.invalid/reference]\n"
        "isNew,type,value\n"
        + "".join(",".join(row) + "\n" for row in pipeline_rows)
    )
    (ROOT / "historical-pipeline" / "article.csv").write_text(
        pipeline_csv,
        encoding="utf-8",
    )
    pipeline = article_typst(
        PIPELINE_HELPERS,
        "article, noteAnalyste, styled-table, load-article_csv, ioc-listv2",
        "  #ioc-listv2(article-data)",
        prelude='#let article-data = load-article_csv(path("./article.csv"))',
    )
    (ROOT / "historical-pipeline" / "article.typ").write_text(pipeline, encoding="utf-8")


if __name__ == "__main__":
    write_inputs()
