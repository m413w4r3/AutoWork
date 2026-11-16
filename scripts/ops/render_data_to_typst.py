#!/usr/bin/env python3
"""Convert an edition ``edition-render-data.json`` into a hand-editable Typst tree (offline, stdlib only).

The data-driven renderer keeps the bulletin text in JSON. This tool writes the same text as plain
Typst markup in the style of the CN2609 reference bulletin: ``main.typ`` (cover, summary, final
tables), ``articles/articleNN/articleNN.typ`` and ``breves/breveNN/breveNN.typ``. Semantic spans
become markup (technical terms in `backticks`, proper names in _italics_, malware/tool/product
names in *bold*). Existing ``UTILS`` (styles, header/footer, colours, fonts) are copied and
imported, and a thin ``UTILS/manuel.typ`` holds the few layout helpers the markup calls.

Internal identifiers (UUIDs, [F]/[E] labels, provenance hashes, technical media names) are never
written: media files are renamed ``articleNN_figureK.ext`` and reader-hidden sections (detection,
derived graphs, annex) are skipped, as in the reader PDF. Unsupported shapes (article_format,
pipe tables, code-block spans, ...) raise instead of being silently dropped.

Usage:
  render_data_to_typst.py --data DIR/edition-render-data.json --media-dir DIR/media \
      --utils-dir workspace/UTILS --fonts-dir fonts --output-dir typst_manuel
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any

MANUEL_TYP = r'''// Layout helpers called by the hand-editable markup (main.typ, articles/, breves/).
// They only compose the styles of helpers.typ / colors.typ / document_style.typ.
#import "helpers.typ": tag, better-link, noteAnalyste, code-font-families
#import "colors.typ": accent, dark, grey, purple, purple-dark, purple-light

// Footnote with a clickable URL (grey highlighted number, URL in the footer).
#let fn(url) = footnote[#better-link(url)[source]]

// Inline `code`: grey highlight, code font, with invisible break opportunities in long literals.
#let breakable-text(s) = {
  if s.len() <= 16 { return s }
  let out = ()
  let run = 0
  for c in s.clusters() {
    out.push(c)
    if c == " " { run = 0 }
    else if c in ("/", "\\", "-", "_", ".", ":", "?", "&", "=", ",", ";", "%") or run >= 7 {
      out.push(h(0.01pt))
      run = 0
    } else { run += 1 }
  }
  out.join()
}
#let inline-literal(it) = highlight(fill: luma(190), radius: 2pt)[#text(font: code-font-families, size: 10pt, hyphenate: false)[#breakable-text(it.text)]]
#let semantic-malware(body) = text(weight: "bold", fill: purple-dark)[#body]
#let semantic-tool(body) = text(weight: "bold", fill: accent)[#body]
#let semantic-product(body) = text(weight: "bold", fill: dark)[#body]
#let semantic-source(body) = text(size: 9pt, fill: accent)[#body]
#let semantic-proof(body) = text(size: 9pt, fill: purple-dark)[#body]

// ---- Article / brief header -------------------------------------------------------------
#let doc-head(title, number, kind: "Article", anchor: "") = [
  #pagebreak()
  #counter(footnote).update(0)
  #text(size: 16pt, weight: "regular", fill: purple)[#title] #label(anchor)
  #grid(
    columns: (1fr, auto),
    align: (left, right),
    [#tag(kind)],
    [#text(size: 11pt, weight: "bold", fill: purple)[#number]],
  )
  #if kind == "Article" [#v(10pt)] else [#v(2pt)]
]

// ---- References -------------------------------------------------------------------------
// One bullet per source; URLs remain clickable footnotes and article citations keep anchors.
#let ref-entry(date, urls: (), role: none, body) = block(
  above: 3pt,
  below: 6pt,
  breakable: false,
)[
  #set par(justify: false)
  #grid(
    columns: (10pt, 1fr),
    column-gutter: 4pt,
    align: (left, left),
    [#text(fill: purple)[•]],
    [
      #if date != none [#text(weight: "bold", fill: purple)[#date]#text(fill: purple)[ : ]]
      #body
      #if role != none [#h(0.25em)#text(size: 7pt, fill: grey)[(#role)]]
      #for u in urls [#h(0.15em)#fn(u)]
    ],
  )
]

// Superscript link(s) to numbered references of the same article.
#let source-ref(scope, ..numbers) = {
  for (i, n) in numbers.pos().enumerate() [
    #if i > 0 [, ]
    #link(label(scope + "-ref" + str(n)))[#super[#str(n)]]
  ]
}

// ---- Chronology -------------------------------------------------------------------------
// Briefs, articles and opinions share the same left-rail chronology.
#let rail(body) = block(
  width: 100%,
  inset: (left: 0pt, right: 0pt, top: 3pt, bottom: 3pt),
  stroke: (left: 1pt + accent),
  body,
)
#let rail-event(date, last: false, body) = [
  #block(breakable: false)[
    #grid(
      columns: (1pt, 1fr),
      column-gutter: 8pt,
      align: (top, left),
      [#circle(radius: 3pt, fill: accent)],
      [
        #if date != "" [
          #text(size: 9pt, weight: "bold", fill: accent)[#date]
          #linebreak()
        ]
        #text(size: 9.5pt)[#body]
      ],
    )
  ]
  #if not last [#v(8pt)]
]

// Compact overview box with colored bullets and the three standard labels.
#let overview-box(body) = block(
  width: 100%,
  above: 10pt,
  below: 15pt,
  fill: purple-light,
  inset: 12pt,
  radius: 4pt,
  breakable: false,
)[
  #text(size: 14pt, weight: "extrabold", fill: purple)[Vue d'ensemble]
  #v(4pt)
  #set par(leading: 0.65em, spacing: 0pt)
  #body
]
#let overview-item(label, body) = block(below: 6pt)[
  #grid(
    columns: (10pt, 1fr),
    align: (left, left),
    [#text(size: 10pt, fill: purple)[•]],
    [
      #if label != "" [#text(size: 10pt, weight: "bold", fill: purple)[#label: ]]
      #text(size: 10pt)[#body]
    ],
  )
]

// ---- Analyst note -----------------------------------------------------------------------
#let note(body) = noteAnalyste(title: [Note de l'analyste], breakable: true)[
  #set par(justify: false, spacing: 0.9em)
  #body
]
// Short observation inside the synthesis (grey frame, technical terms in small code font).
#let obs(body) = block(above: 10pt, below: 8pt)[
  #noteAnalyste[
    #set par(justify: false, spacing: 0.9em)
    #show raw.where(block: false): it => text(font: code-font-families, size: 8pt, hyphenate: false)[#breakable-text(it.text)]
    #body
  ]
]
#let qualif(label) = block(sticky: true, above: 0pt, below: 0.4em)[#text(size: 8pt, weight: "bold")[#label]]
#let ioc-renvoi() = {
  v(16pt)
  text(size: 9pt, fill: grey)[
    Les indicateurs de compromission de cet avis (fichiers et réseau) sont repris dans le
    tableau récapitulatif des IOC du bulletin.
  ]
}

// ---- Figures ----------------------------------------------------------------------------
#let fig-caption(number, body) = block(width: 100%, above: 0pt, below: 4pt)[
  #align(center)[#text(size: 9pt, fill: grey, style: "italic")[Figure #number : #body]]
]

// Image fitted to the frame (vector: fit; raster: enlarged at most 2.5x, never if < 100pt wide).
#let fig-image(media, vector, caption, max-height) = block(breakable: false, width: 100%, above: 10pt, below: 8pt)[
  #layout(size => {
    let natural-image = image(media)
    let natural-size = measure(natural-image)
    let following = [
      #if caption != none [
        #v(4pt)
        #caption
      ]
    ]
    let max-image-height = calc.max(1pt, size.height * max-height)
    let frame-scale = calc.min(size.width / natural-size.width, max-image-height / natural-size.height)
    let scale = if vector or frame-scale <= 1 {
      frame-scale
    } else {
      calc.min(frame-scale, if natural-size.width < 100pt { 1 } else { 2.5 })
    }
    let rendered = if scale != 1 { image(media, width: natural-size.width * scale) } else { natural-image }
    block(breakable: false, width: 100%)[
      #align(center)[#rendered]
      #following
    ]
  })
]

// Figure with a caption: #fig(path("../../media/x.png"), 2)[caption]
#let fig(media, number, vector: false, max-height: 0.36, caption) = fig-image(
  media, vector, fig-caption(number, caption), max-height,
)
// Diagram (protocol, chain...): optional bold title and status legend.
#let diag(media, number, title: none, legend: none, max-height: 0.35, caption) = fig-image(
  media,
  true,
  [
    #if title != none [#block(above: 2pt, below: 2pt)[#text(size: 9pt, weight: "bold")[#title]]]
    #fig-caption(number, caption)
    #if legend != none [#block(above: 3pt, below: 2pt)[#text(size: 8pt, fill: grey)[#legend]]]
  ],
  max-height,
)

// ---- Tables -----------------------------------------------------------------------------
#let tbl-title(title) = if title != none { text(size: 12pt, weight: "bold")[#title] } else { none }
#let tbl-caption(caption) = {
  v(4pt)
  if caption != none [#block(above: 2pt, below: 4pt)[#text(size: 9pt, fill: grey)[#caption]]]
}

// Publication table; match the typography, inset, header and grid of styled-table.
#let tbl(title, weights, header, caption: none, ..cells) = {
  let n = weights.len()
  let header-cells = header.map(c => [#set text(weight: "bold", fill: white); #c])
  let body-cells = cells.pos().enumerate().map(((index, cell)) => {
    let row = calc.floor(index / n)
    if calc.rem(row, 2) == 0 {
      [#block(sticky: true, breakable: false, above: 0pt, below: 0pt)[#cell]]
    } else {
      [#block(breakable: false, above: 0pt, below: 0pt)[#cell]]
    }
  })
  let body-row-count = calc.floor(cells.pos().len() / n)
  set text(size: 9pt)
  let rendered-table = align(center, table(
    columns: weights.map(w => w * 1fr),
    inset: (x: 5pt, y: 4pt),
    align: left,
    fill: (_, y) => if y == 0 { purple } else { none },
    stroke: 0.6pt + gray,
    table.header(repeat: true, ..header-cells),
    ..body-cells,
  ))
  let table-content = if body-row-count <= 12 {
    block(breakable: false, above: if title == none { 0pt } else { 6pt })[
      #if title != none [#tbl-title(title)#v(4pt)]
      #rendered-table
    ]
  } else {
    [
      #if title != none [#block(sticky: true, above: 6pt)[#tbl-title(title)#v(4pt)]]
      #rendered-table
    ]
  }
  [
    #table-content
    #tbl-caption(caption)
  ]
}

// 3+ columns: one card per row (first cell in bold code, then "column: value" lines).
#let cards(title, header, rows, caption: none) = {
  block(above: 10pt, below: 8pt)[
    #tbl-title(title)
    #for row in rows [
      #block(
        width: 100%, above: 3pt, below: 4pt,
        fill: rgb("#F8FAFC"), stroke: 0.4pt + rgb("#D0D5DD"),
        inset: 6pt, radius: 3pt, breakable: true,
      )[
        #set text(hyphenate: false)
        #set par(justify: false, leading: 1.05em, spacing: 1pt)
        #box[#text(font: code-font-families, size: 9pt, weight: "bold", hyphenate: false)[#row.first()]]
        #for (i, column) in header.slice(1).enumerate() [
          #linebreak()
          #text(size: 8pt, weight: "bold")[#column: ]
          #row.at(i + 1)
        ]
      ]
    ]
  ]
  tbl-caption(caption)
}
'''

STYLE_COLOURED = {
    "semantic-malware": "malware",
    "semantic-tool": "tool",
    "semantic-product": "product",
    "semantic-source": "source",
    "semantic-proof": "proof",
}
STYLE_EMPH = {
    "semantic-actor", "semantic-campaign", "semantic-proper-name", "semantic-english-term",
}
STYLE_RAW = {
    "semantic-technical", "semantic-technical-literal", "semantic-table-technical",
    "semantic-ioc", "semantic-table-ioc", "semantic-path", "semantic-table-path",
    "semantic-command", "semantic-table-command", "semantic-protocol-field",
}
SPECIAL = set("\\#$*_`@<[]~'\"")
OPEN_OK = set(" \t\n([{«“‘—–-:;,.!?/'\"")
CLOSE_OK = set(" \t\n)]}»”’—–-:;,.!?/'\"")
OVERVIEW_PREFIX = re.compile(r"^\s*(Victimologie|Arsenal|Objectif principal)\s*:\s*", re.IGNORECASE)
OVERVIEW_LABELS = {
    "victimologie": "Victimologie",
    "arsenal": "Arsenal",
    "objectif principal": "Objectif principal",
}


def esc(text: str) -> str:
    """Escape plain text for Typst markup (no smart quotes, no shorthands, no emphasis)."""
    out: list[str] = []
    for index, char in enumerate(text):
        nxt = text[index + 1] if index + 1 < len(text) else ""
        prev = text[index - 1] if index else ""
        if char == "\u00a0":
            out.append("~")
        elif char in SPECIAL:
            out.append("\\" + char)
        elif char == "/" and nxt == "/" and prev != ":":
            out.append("/\\")  # the second slash is escaped on the next iteration
        elif char == "/" and prev == "/" and out[-1].endswith("\\"):
            out.append("/")
        elif char == "-" and nxt in {"-", "?"}:
            out.append("\\-")
        elif char == "." and text[index : index + 3] == "...":
            out.append("\\.")
        elif unicodedata.category(char) in {"Cf", "Zs"} and char != " ":
            out.append(f"\\u{{{ord(char):x}}}")
        else:
            out.append(char)
    return "".join(out)


def escape_line_start(line: str) -> str:
    """Escape the characters that Typst treats as list/heading/term markers at line start."""
    stripped = line.lstrip(" ")
    pad = line[: len(line) - len(stripped)]
    if stripped[:1] in {"-", "+", "=", "/"}:
        return pad + "\\" + stripped
    match = re.match(r"\d+\.", stripped)
    if match:
        return pad + stripped[: match.end() - 1] + "\\" + stripped[match.end() - 1 :]
    return line


def tstr(value: str) -> str:
    """Typst string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def span_class(style: str) -> str:
    if style == "semantic-plain":
        return "plain"
    if style in STYLE_COLOURED:
        return STYLE_COLOURED[style]
    if style in STYLE_EMPH:
        return "emph"
    if style in STYLE_RAW:
        return "raw"
    raise ValueError(f"unsupported semantic style: {style}")


def raw_markup(text: str) -> str:
    if "`" in text or "\n" in text:
        return f"#raw({tstr(text)}, block: false);"
    return f"`{text}`"


def spans_markup(spans: list[dict[str, Any]] | None, fallback: str = "", *, technical_flag: bool = False) -> str:
    """Markup for a list of semantic spans (or a plain fallback string)."""
    if spans is None:
        items: list[tuple[str, str]] = [("plain", fallback)]
    elif technical_flag:
        items = [("raw" if s.get("technical") else "plain", s["text"]) for s in spans]
        if any(s.get("code_block") for s in spans):
            raise ValueError("code_block spans are not supported by this converter")
    else:
        items = [(span_class(s["style"]), s["text"]) for s in spans]
    merged: list[tuple[str, str]] = []
    for kind, text in items:
        if not text:
            continue
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1] + text)
        else:
            merged.append((kind, text))
    # Move edge whitespace of decorated spans outside, so delimiters hug the word.
    expanded: list[tuple[str, str]] = []
    for kind, text in merged:
        if kind == "plain":
            expanded.append((kind, text))
            continue
        core = text.strip(" ")
        lead, trail = text[: len(text) - len(text.lstrip(" "))], text[len(text.rstrip(" ")) :]
        if lead:
            expanded.append(("plain", lead))
        if core:
            expanded.append((kind, core))
        if trail:
            expanded.append(("plain", trail))
    pieces: list[tuple[str, str]] = []
    for kind, text in expanded:
        if pieces and pieces[-1][0] == "plain" and kind == "plain":
            pieces[-1] = ("plain", pieces[-1][1] + text)
        else:
            pieces.append((kind, text))

    def plain_edge(index: int, step: int) -> str:
        j = index + step
        if j < 0 or j >= len(pieces):
            return " "
        text = pieces[j][1]
        if not text:
            return " "
        char = text[-1] if step < 0 else text[0]
        return char if pieces[j][0] == "plain" else "x"

    out: list[str] = []
    for index, (kind, text) in enumerate(pieces):
        if kind == "plain":
            out.append(esc(text))
            continue
        if kind == "raw":
            out.append(raw_markup(text))
            continue
        before, after = plain_edge(index, -1), plain_edge(index, +1)
        body = esc(text)
        if kind in STYLE_COLOURED.values():
            out.append(f"#semantic-{kind}[{body}]")
            continue
        delim = "_" if kind == "emph" else "*"
        if before in OPEN_OK and after in CLOSE_OK and "\n" not in text:
            out.append(f"{delim}{body}{delim}")
        else:
            func = "emph" if kind == "emph" else "strong"
            out.append(f"#{func}[{body}];")
    joined = "".join(out)
    if "\n" in joined:
        joined = " \\\n".join(escape_line_start(part) for part in joined.split("\n"))
    return escape_line_start(joined)


def unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def tuple_literal(values: list[str]) -> str:
    return "(" + "".join(tstr(v) + ", " for v in values) + ")"


class Builder:
    def __init__(self, data: dict[str, Any], media_dir: Path, out: Path) -> None:
        self.data = data
        self.media_dir = media_dir
        self.out = out
        self.warnings: list[str] = []

    # -- media ---------------------------------------------------------------------------
    def copy_media(self, block: dict[str, Any], folder: str, prefix: str) -> tuple[str, bool]:
        source = self.media_dir / Path(block["media_path"]).name
        if not source.is_file():
            raise FileNotFoundError(source)
        suffix = source.suffix.lower()
        name = f"{prefix}_figure{block['figure_number']}{suffix}"
        target = self.out / "media" / name
        if target.exists():
            raise ValueError(f"duplicate figure name {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return f"path({tstr('../../media/' + name)})", suffix == ".svg"

    # -- blocks --------------------------------------------------------------------------
    def table(self, block: dict[str, Any]) -> str:
        columns = block["columns"]
        cells = block.get("semantic_cells")
        flat = [cell for row in block["rows"] for cell in row]

        def cell(index: int) -> str:
            spans = cells[index] if cells else None
            return "[" + spans_markup(spans, flat[index]) + "]"

        header_spans = block.get("semantic_columns")
        header = [
            "[" + spans_markup(header_spans[i] if header_spans else None, columns[i]) + "]"
            for i in range(len(columns))
        ]
        title = block.get("title")
        title_markup = "none" if not title else "[" + spans_markup(block.get("semantic_title"), title) + "]"
        caption = block.get("caption")
        caption_markup = (
            "none" if not caption else "[" + spans_markup(block.get("semantic_caption"), caption) + "]"
        )
        rows = [[cell(r * len(columns) + c) for c in range(len(columns))] for r in range(len(block["rows"]))]
        weights = block.get("column_weights") or [1.0] * len(columns)
        lines = [f"#tbl({title_markup}, ({', '.join(str(w) for w in weights)},), ({', '.join(header)},), caption: {caption_markup},"]
        lines += ["  " + ", ".join(row) + "," for row in rows]
        lines.append(")")
        return "\n".join(lines)

    def figure_like(self, block: dict[str, Any], folder: str, prefix: str, *, protocol: bool = False) -> str:
        media, vector = self.copy_media(block, folder, prefix)
        caption = block.get("caption") or block.get("title") or ""
        if not caption:
            raise ValueError("figure without caption")
        semantic = block.get("semantic_caption") if block.get("caption") else block.get("semantic_title")
        caption_markup = spans_markup(semantic, caption)
        number = block["figure_number"]
        if block["type"] == "figure":
            return f"#fig({media}, {number}, vector: {str(vector).lower()})[{caption_markup}]"
        if block["type"] == "diagram":
            if block.get("legacy_renderer"):
                raise ValueError("legacy renderer diagrams are not supported")
            args = [media, str(number)]
            if block.get("title") and (block.get("status_label") or block.get("show_title")):
                args.append(f"title: [{spans_markup(None, block['title'])}]")
            if block.get("status_label"):
                raise ValueError("diagram status_label is not supported")
            legend = block.get("status_legend")
            if legend:
                args.append(f"legend: {tstr(legend)}")
            height = 0.82 if protocol else (0.52 if legend else 0.35)
            args.append(f"max-height: {height}")
            return f"#diag({', '.join(args)})[{caption_markup}]"
        raise ValueError(f"unsupported figure type {block['type']}")

    def overview_item(self, block: dict[str, Any]) -> tuple[str, str] | None:
        text = block.get("text", "")
        match = OVERVIEW_PREFIX.match(text)
        if match is None:
            return None
        label = OVERVIEW_LABELS[match.group(1).casefold()]
        spans = block.get("semantic_spans")
        if spans is None:
            body = spans_markup(None, text[match.end() :])
        else:
            if "".join(span["text"] for span in spans) != text:
                raise ValueError("overview semantic spans do not match paragraph text")
            remaining = match.end()
            body_spans: list[dict[str, Any]] = []
            for span in spans:
                span_text = span["text"]
                if remaining >= len(span_text):
                    remaining -= len(span_text)
                    continue
                body_spans.append({**span, "text": span_text[remaining:]})
                remaining = 0
            if remaining:
                raise ValueError("overview semantic spans do not contain the label prefix")
            body = spans_markup(body_spans)
        return label, body

    def legacy_overview(
        self, blocks: list[dict[str, Any]]
    ) -> tuple[int | None, list[tuple[str, str]], set[int]]:
        for index, block in enumerate(blocks):
            if block.get("type") != "section_heading" or block.get("text", "").strip().casefold() != "vue d'ensemble":
                continue
            skip = {index}
            items: list[tuple[str, str]] = []
            for item_index in range(index + 1, len(blocks)):
                item = blocks[item_index]
                if item.get("type") == "section_heading":
                    break
                if item.get("type") == "paragraph":
                    overview_item = self.overview_item(item)
                    if overview_item is not None:
                        items.append(overview_item)
                        skip.add(item_index)
            return index, items, skip
        return None, [], set()

    @staticmethod
    def append_overview(items: list[tuple[str, str]], lines: list[str]) -> bool:
        if not items:
            return False
        lines.append("#overview-box[")
        for label, body in items:
            lines.append(f"  #overview-item({tstr(label)})[{body}]")
        lines += ["]", ""]
        return True

    def structured_overview(self, overview: dict[str, Any], lines: list[str]) -> bool:
        fields = (
            ("Victimologie", overview.get("victimology")),
            ("Arsenal", overview.get("arsenal")),
            ("Objectif principal", overview.get("main_objective")),
        )
        items: list[tuple[str, str]] = []
        for label, field in fields:
            if field is None:
                continue
            if isinstance(field, str):
                body = spans_markup(None, field)
            else:
                body = spans_markup(field.get("semantic_spans"), field.get("text", ""))
            items.append((label, body))
        return self.append_overview(items, lines)

    def synthesis(
        self,
        section: dict[str, Any],
        folder: str,
        stem: str,
        lines: list[str],
        *,
        structured_overview_rendered: bool,
        paragraph_spacing: str = "0.9em",
    ) -> None:
        blocks = section["blocks"]
        overview_index, legacy_items, skip = self.legacy_overview(blocks)
        if structured_overview_rendered:
            # The structured article_format is canonical when present; avoid duplicating legacy fields.
            skip.update(
                index
                for index, block in enumerate(blocks)
                if block.get("type") == "paragraph" and self.overview_item(block)
            )
        lines += ["= Synthèse", f"#set par(spacing: {paragraph_spacing})", ""]
        for index, block in enumerate(blocks):
            if index == overview_index:
                if not structured_overview_rendered and not self.append_overview(legacy_items, lines):
                    lines.append("== Vue d'ensemble")
                continue
            if index in skip:
                continue
            lines.append(self.body_block(block, folder, stem))
            lines.append("")

    def body_block(self, block: dict[str, Any], folder: str, prefix: str) -> str:
        kind = block["type"]
        if kind == "paragraph":
            return spans_markup(block.get("semantic_spans"), block["text"])
        if kind == "section_heading":
            return "== " + spans_markup(None, block["text"])
        if kind == "table":
            return self.table(block)
        if kind in {"figure", "diagram"}:
            return self.figure_like(block, folder, prefix)
        if kind == "analyst_observation":
            return "#obs[\n" + spans_markup(block.get("text_spans"), block["text"], technical_flag=True) + "\n]"
        raise ValueError(f"unsupported block type {kind}")

    # -- publications --------------------------------------------------------------------
    def publication(self, pub: dict[str, Any]) -> tuple[Path, str]:
        article_format = pub.get("article_format") or {}
        if not isinstance(article_format, dict):
            raise ValueError("article_format must be an object")
        article_events = article_format.get("timeline_key_events") or []
        article_overview = article_format.get("overview")
        brief = pub["kind"] == "brief"
        number = pub["number"]
        stem = ("breve" if brief else "article") + number
        folder = ("breves" if brief else "articles") + f"/{stem}"
        sections = {s["type"]: s for s in pub["content_sections"]}
        opinion = "analyst_opinion" in sections
        scope = f"a{number}"
        lines: list[str] = []
        add = lines.append
        add('#import "../../UTILS/manuel.typ": *')
        add("")
        if not brief:
            add(f'#let rf = source-ref.with("{scope}")')
            add("")
        add(
            f'#doc-head([{spans_markup(None, pub["title"])}], "{number}", '
            f'kind: {tstr("Brève" if brief else "Article")}, anchor: {tstr(pub["anchor"])})'
        )
        if brief:
            add("#set text(size: 10pt)")
        add("")
        chronology_rendered = False
        overview_rendered = False
        for section in pub["content_sections"]:
            kind = section["type"]
            if kind == "references":
                self.references(section, opinion, "" if brief else scope, lines)
            elif kind == "chronology":
                events = article_events or section.get("timeline") or []
                self.chronology({"timeline": events}, brief, scope, lines)
                chronology_rendered = True
                if article_overview is not None:
                    overview_rendered = self.structured_overview(article_overview, lines)
            elif kind == "synthesis":
                if article_events and not chronology_rendered:
                    self.chronology({"timeline": article_events}, brief, scope, lines)
                    chronology_rendered = True
                if article_overview is not None and not overview_rendered:
                    overview_rendered = self.structured_overview(article_overview, lines)
                self.synthesis(
                    section,
                    folder,
                    stem,
                    lines,
                    structured_overview_rendered=overview_rendered,
                    paragraph_spacing="1.2em" if brief else "0.9em",
                )
            elif kind == "analyst_opinion":
                self.opinion(section, stem, folder, lines)
            elif kind == "ioc":
                if section.get("source_indicators") and any(g.get("items") for g in section["source_indicators"]):
                    if brief:
                        raise ValueError("brief IOC groups are not supported")
                if opinion:
                    add("#ioc-renvoi()")
                    add("")
            elif kind in {"detection", "technical_annex", "provenance", "publication_provenance"}:
                continue  # delivered as annex files / hidden from readers
            else:
                raise ValueError(f"unsupported content section {kind}")
        return Path(folder) / f"{stem}.typ", "\n".join(lines).rstrip() + "\n"

    def references(self, section: dict[str, Any], opinion: bool, scope: str, lines: list[str]) -> None:
        if section.get("blocks"):
            raise ValueError("reference blocks are not supported")
        entries = [entry for entry in section["entries"] if entry.get("text") or entry.get("title")]
        if not entries:
            return
        lines += ["= Références", ""]
        for index, entry in enumerate(entries, 1):
            text = entry.get("text")
            body = spans_markup(entry.get("semantic_spans"), text) if text else spans_markup(None, entry["title"])
            urls = unique(([entry["url"]] if entry.get("url") else []) + list(entry.get("source_urls") or []))
            if opinion and not urls:
                raise ValueError("numbered reference without URL")
            date = entry.get("display_date") or "Date de publication non précisée"
            number = entry.get("number", index)
            role = tstr(entry["role_label"]) if entry.get("role_label") else "none"
            reference_label = f" <{scope}-ref{number}>" if scope else ""
            lines += [
                f"#ref-entry({tstr(date)}, urls: {tuple_literal(urls)}, role: {role})[{body}]{reference_label}",
                "",
            ]

    def chronology(self, section: dict[str, Any], brief: bool, scope: str, lines: list[str]) -> None:
        events = section.get("timeline") or []
        if not events:
            return
        lines += ["= Chronologie", ""]
        lines.append("#rail[")
        for index, event in enumerate(events):
            urls = unique(event.get("source_urls") or [])
            refs = event.get("source_refs") or []
            if brief and not urls:
                raise ValueError("brief chronology event without URL footnote")
            body = spans_markup(event.get("semantic_spans"), event["text"])
            if refs and not brief:
                numbers = [
                    str(ref.get("number", ref_index + 1) if isinstance(ref, dict) else ref)
                    for ref_index, ref in enumerate(refs)
                ]
                notes = f' #rf({", ".join(numbers)})'
            else:
                notes = "".join(f" #fn({tstr(url)})" for url in urls)
            last = ", last: true" if index == len(events) - 1 else ""
            date = event.get("display_date_fr") or event.get("display_date", "")
            lines.append(f'  #rail-event({tstr(date)}{last})[{body}{notes}]')
        lines += ["]", ""]

    def opinion(self, section: dict[str, Any], stem: str, folder: str, lines: list[str]) -> None:
        blocks = section.get("blocks") or []
        unanchored = section.get("unanchored_observations") or []
        points = section.get("unresolved_point_render_data") or section.get("unresolved_points") or []
        if unanchored:
            raise ValueError("unanchored observations are not supported")
        if any(b.get("pipe_table") for b in blocks):
            raise ValueError("opinion pipe tables are not supported")
        if blocks or points:
            lines.append("#note[")
            for block in blocks:
                heading = f'{block["number"]} {block["heading"]}'
                lines += [
                    f"=== {spans_markup(None, heading)}",
                    f'#qualif[{spans_markup(None, block["qualification_label"])}]',
                    "",
                    spans_markup(block.get("text_spans"), block["text"], technical_flag=True),
                    "",
                ]
            if points:
                lines += ["=== Points non résolus", ""]
                for point in points:
                    if isinstance(point, str):
                        lines.append("- " + spans_markup(None, point))
                    else:
                        lines.append("- " + spans_markup(point.get("text_spans"), point["text"], technical_flag=True))
                lines.append("")
            lines += ["]", ""]
        for figure in section.get("protocol_figures") or []:
            lines += [
                "#pagebreak(weak: true)",
                "#v(8pt)",
                self.figure_like(figure, folder, stem, protocol=True),
                "",
            ]

    # -- main ----------------------------------------------------------------------------
    def main_typ(self, articles: list[dict[str, Any]], briefs: list[dict[str, Any]]) -> str:
        e = self.data["edition"]
        lines = [
            '#import "UTILS/document_style.typ": apply-document-style',
            '#import "UTILS/colors.typ": purple',
            '#import "UTILS/helpers.typ": article-indexing, better-link, styled-table, tag',
            '#import "UTILS/manuel.typ": inline-literal',
            "",
            "#show: apply-document-style.with(",
            f'  country: {tstr(e["country"])}, period: {tstr(e["bulletin_period"])},',
            f'  bulletin-number: {tstr(e["bulletin_number"])}, bulletin-identifier: {tstr(e["bulletin_identifier"])},',
            ")",
            "#show raw.where(block: false): inline-literal",
            "",
            "// ---- Couverture ----",
            "#align(center)[",
            '  #image("UTILS/chap.png", width: 18%)',
            "  #v(14pt)",
            f'  #text(size: 20pt, weight: "extrabold", fill: purple)[{esc(e["bulletin_title"])}]',
            "  #v(10pt)",
            f'  #text(size: 14pt, weight: "bold")[{esc(e["country"])}]',
            "  #linebreak()",
            f'  #text(size: 10pt)[{esc(e["country_code"])}]',
            "  #v(8pt)",
            f'  #text(size: 12pt)[{esc(e["bulletin_period"])}]',
            "  #v(5pt)",
            f'  #text(size: 9pt, weight: "bold", fill: purple)[{esc(self.data.get("document_header", "Bulletin de veille CTI"))}]',
            "  #v(6pt)",
            f'  #text(size: 11pt)[Bulletin n° {esc(e["bulletin_number"])}]',
            "  #linebreak()",
            f'  #text(size: 10pt)[Identifiant : {esc(e["bulletin_identifier"])}]',
            "  #v(6pt)",
            f'  #text(size: 10pt)[Classification : {esc(e["tlp"])}]',
            "]",
            "",
            "#v(16pt)",
            "= Résumé de l'actualité #tag(\"ARTICLES\")",
            "#article-indexing((",
        ]
        for pub in articles:
            lines.append(f'  ("{pub["number"]}", better-link(<{pub["anchor"]}>)[{spans_markup(None, pub["title"])}]),')
        lines += ["))", "", "#v(16pt)", '= Autres brèves relevées #tag("BRÈVES")']
        if briefs:
            lines.append("#article-indexing((")
            for pub in briefs:
                lines.append(f'  ("{pub["number"]}", better-link(<{pub["anchor"]}>)[{spans_markup(None, pub["title"])}]),')
            lines.append("))")
        else:
            lines.append("Aucune brève relevée dans cette édition.")
        ident = e["bulletin_identifier"]
        lines += [
            "",
            "#v(16pt)",
            "= Règles Suricata",
            "- Aucune règle Suricata n'est projetée dans ce rendu d'édition.",
            "",
            "#v(12pt)",
            "= Suivi des indicateurs",
            f"- Échantillons relevés : {self.data['file_ioc_count']}",
            "",
            "#v(16pt)",
            "#styled-table(",
            "  (1fr, 4fr),",
            "  cell-align: (center, left),",
            "  [Fichier intégré], [Description],",
            "  [Article CSV], [Un fichier `article.csv` par publication, avec les IOC publiés et ceux identifiés lors de l'analyse.],",
            f"  [Export IOC], [Archive `iocs_{ident}.zip` : CSV, manifeste JSON et `legacy_iocs_{ident}.txt`.],",
            "  [STIX], [Génération déléguée en aval ; aucun fichier STIX n'est intégré à cette édition.],",
            "  [Excel], [Génération déléguée en aval ; aucun tableur Excel n'est intégré à cette édition.],",
            "  [ZIP des annexes], [Fournis séparément avec le bulletin : règles YARA et Suricata, notices et CSV d'IOC.],",
            ")",
            "",
            "// ---- Articles puis brèves (un fichier par publication) ----",
        ]
        for pub in articles + briefs:
            stem = ("breve" if pub["kind"] == "brief" else "article") + pub["number"]
            folder = "breves" if pub["kind"] == "brief" else "articles"
            lines.append(f'#include "{folder}/{stem}/{stem}.typ"')
        return "\n".join(lines) + "\n"


COMPILE_SH = """#!/bin/sh
# Recompile le bulletin depuis les sources Typst rédigées à la main (Typst 0.15.x).
# Usage : ./compile.sh [sortie.pdf]   (variable TYPST pour indiquer le binaire)
set -eu
cd "$(dirname "$0")"
OUT="${1:-bulletin_manuel.pdf}"
TYPST="${TYPST:-typst}"
LANG=C.UTF-8 LC_ALL=C.UTF-8 "$TYPST" compile --root . --font-path fonts --ignore-system-fonts \\
  --creation-timestamp 0 main.typ "$OUT"
echo "écrit : $OUT"
"""

READ_ME = """# Bulletin {ident} — sources Typst rédigées à la main

Arborescence autonome, dans l'esprit du bulletin CN2609 : le texte est écrit **directement en
markup Typst** (et non dans un fichier de données). Générée par
`scripts/ops/render_data_to_typst.py` à partir de `edition-render-data.json` (composition v15).

## Contenu
- `main.typ` : couverture, sommaire (liens bleus vers chaque publication), règles/indicateurs,
  puis `#include` des publications dans l'ordre du bulletin.
- `articles/articleNN/articleNN.typ`, `breves/breveNN/breveNN.typ` : un fichier par publication.
- `media/` : figures (`articleNN_figureK.ext`, `breveNN_figureK.ext`).
- `UTILS/` : styles existants (`document_style.typ`, `helpers.typ`, `colors.typ`,
  `header_footer.typ`, logo) + `manuel.typ` (petites macros de mise en page appelées par le texte).
- `fonts/` : Hanken Grotesk et Cascadia (nécessaires pour reproduire la composition).

## Recompiler
Installer Typst 0.15.x puis `./compile.sh [sortie.pdf]` (ou `TYPST=/chemin/typst ./compile.sh`).
Équivaut à `typst compile --root . --font-path fonts --ignore-system-fonts --creation-timestamp 0 main.typ sortie.pdf`.
Les avertissements « label … is not attached to anything » sont normaux.

## Comment éditer
- **Texte courant** : modifier le paragraphe directement. Conventions : `code` entre accents
  graves (termes techniques, chemins, IOC, commandes), `_italique_` (acteurs, campagnes, noms
  propres), `*gras*` (malwares, outils, produits). Caractères spéciaux : `\\#`, `\\$`, `\\_`, `\\*`, `\\@`, `\\[`, `\\]`.
- **Notes de bas de page / sources** : `#fn("https://…")` ; **appel de référence d'un avis** : `#rf(2)`.
- **Titres** : `=` (section), `==` (sous-section), `===` (dans la note de l'analyste).
- **Note de l'analyste** : bloc `#note[ … ]` en fin d'avis ; observations courtes : `#obs[ … ]`.
- **Figures** : `#fig(path("../../media/…"), N)[légende]`, `#diag(…)` pour les diagrammes.
- **Tableaux** : `#tbl(…)` reproduit la grille publiée ; `#cards(…)` reste disponible pour une
  fiche compacte rédigée manuellement.
- **Mise en page** : `UTILS/*.typ` (styles généraux) et `UTILS/manuel.typ` (macros de cette arborescence).

## Écarts connus par rapport à `IR2610_bulletin.pdf`
- Pas d'identité pixel à pixel : coupures de ligne/de page et nombre de pages peuvent différer
  (le nombre de pages peut varier légèrement avec les coupures et les notes de bas de page).
- Sommaire : liens bleus vers les publications, sans numéros de page (comme dans l'original).
- Les références, frises chronologiques, encadrés « Vue d'ensemble », couleurs sémantiques,
  tableaux et notes de l'analyste suivent les styles du PDF de référence.
- Les termes techniques et IOC gardent leur surlignage gris serré ; les paragraphes sont séparés
  par un espacement de 0,9 à 1,2 em.
- Les points d'ancrage invisibles de césure des littéraux longs sont recalculés par une règle
  générique (`breakable-text`), pas repris de l'original.
- Non reproduits (volontairement, comme dans le PDF lecteur) : règles de détection, graphes dérivés,
  annexes techniques, identifiants internes (UUID, étiquettes de preuve, empreintes de provenance).
- Les modifications faites ici n'affectent pas les données canoniques d'AutoWork.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, type=Path, help="edition-render-data.json")
    parser.add_argument("--media-dir", required=True, type=Path, help="directory holding the media files")
    parser.add_argument("--utils-dir", required=True, type=Path, help="existing UTILS (styles, logo)")
    parser.add_argument("--fonts-dir", required=True, type=Path, help="fonts to copy next to the sources")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    data = json.loads(args.data.read_text(encoding="utf-8"))
    out: Path = args.output_dir
    if out.exists() and any(out.iterdir()):
        print(f"refusing to write into non-empty {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    shutil.copytree(args.utils_dir, out / "UTILS")
    shutil.copytree(args.fonts_dir, out / "fonts")
    (out / "UTILS" / "manuel.typ").write_text(MANUEL_TYP, encoding="utf-8")

    builder = Builder(data, args.media_dir, out)
    publications = data["publications"]
    articles = [p for p in publications if p["kind"] == "article"]
    briefs = [p for p in publications if p["kind"] == "brief"]
    for pub in articles + briefs:
        rel, text = builder.publication(pub)
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (out / "main.typ").write_text(builder.main_typ(articles, briefs), encoding="utf-8")
    compile_sh = out / "compile.sh"
    compile_sh.write_text(COMPILE_SH, encoding="utf-8")
    compile_sh.chmod(0o755)
    (out / "LISEZ-MOI.md").write_text(
        READ_ME.format(ident=data["edition"]["bulletin_identifier"]), encoding="utf-8"
    )
    print(f"wrote {len(articles)} articles, {len(briefs)} briefs to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
