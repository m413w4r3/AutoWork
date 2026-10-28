#import "../UTILS/helpers.typ": section-title, timeline, ioc-list, semantic-text, semantic-or-plain, code-font-families, noteAnalyste
#import "../UTILS/colors.typ": accent, grey

#let has-text(value) = value != none and value != ""

#let publication-display-text(item) = {
  if type(item) == str {
    text(item)
  } else {
    let chunks = item.at("break_chunks", default: ())
    if chunks.len() == 0 {
      text(item.text)
    } else {
      let break-after = item.at("break_after", default: ())
      for (index, chunk) in chunks.enumerate() {
        text(chunk)
        if index < chunks.len() - 1 {
          let should-break = if break-after.len() == chunks.len() - 1 {
            break-after.at(index)
          } else {
            true
          }
          if should-break { h(0.01pt) }
        }
      }
    }
  }
}

#let publication-detection-code-line(item) = {
  let chunks = item.at("break_chunks", default: ())
  let break-after = item.at("break_after", default: ())
  if chunks.len() == 0 {
    text(item.text)
  } else {
    for (index, chunk) in chunks.enumerate() {
      text(chunk)
      if index < chunks.len() - 1 and break-after.len() == chunks.len() - 1 and break-after.at(index) {
        linebreak()
      }
    }
  }
  linebreak()
}

// The surrounding A4 renderer reserves 2.2cm on each horizontal edge. These
// context markers and their metadata occupy no layout space and are queried in
// the very workspace that produced the PDF.
#let render-target(target-id, body) = {
  if target-id == none {
    body
  } else {
    metadata((target_id: target-id, boundary: "start"))
    context {
      let location = here()
      let position = location.position()
      let size = measure(body, width: 470.55pt)
      metadata((
        target_id: target-id,
        boundary: "location_start",
        page: location.page(),
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
      ))
    }
    body
    context {
      let location = here()
      let position = location.position()
      metadata((
        target_id: target-id,
        boundary: "location_end",
        page: location.page(),
        x: position.x,
        y: position.y,
      ))
    }
    metadata((target_id: target-id, boundary: "end"))
  }
}

#let unique-urls(urls) = urls.fold((), (result, url) => {
  if url in result { result } else { result + (url,) }
})

#let render-publication-timeline(events, ordered: false, reference-scope: "") = {
  let all-source-urls = events.fold((), (all, event) => {
    event.at("source_urls", default: ()).fold(all, (result, url) => {
      if url in result { result } else { result + (url,) }
    })
  })
  for event in events {
    let role-label = event.at("role_label", default: none)
    let source-urls = unique-urls(event.at("source_urls", default: ()))
    let source-indices = source-urls.map(url => all-source-urls.position(candidate => candidate == url) + 1)
    let source-refs = event.at("source_refs", default: ())
    let body = [
      #if has-text(role-label) [
        #text(size: 8pt, fill: grey)[#role-label]
        #linebreak()
      ]
      #let display-date = event.at("display_date", default: none)
      #if has-text(display-date) [
        #text(weight: "bold")[#(display-date + " : ")]
      ]
      #semantic-or-plain(event.text, event.at("semantic_spans", default: none))
      #if source-refs.len() > 0 [
        #for (index, source-ref) in source-refs.enumerate() [
          #if index > 0 [, ]
          #let source-number = source-ref.at("number", default: index + 1)
          #let target-id = source-ref.at("target_id", default: none)
          #let source-url = source-ref.at("url", default: none)
          #if target-id != none [
            #link(label(reference-scope + target-id))[#super[#str(source-number)]]
          ] else if source-url != none [
            #super[#link(source-url)[#str(source-number)]]
          ] else [
            #super[#str(source-number)]
          ]
        ]
      ] else if source-indices.len() > 0 [
        #for (index, source-index) in source-indices.enumerate() [
          #if index > 0 [, ]
          #super[#link(source-urls.at(index))[#str(source-index)]]
        ]
      ]
    ]
    if ordered {
      enum.item(body)
    } else {
      list.item(body)
    }
  }
  let uses-internal-refs = events.any(event => event.at("source_refs", default: ()).len() > 0)
  if not ordered and all-source-urls.len() > 0 and not uses-internal-refs {
    block(above: 2pt, below: 0pt)[
      #for (index, url) in all-source-urls.enumerate() [
        #if index > 0 [#linebreak()]
        #text(size: 6pt, fill: grey)[#str(index + 1)]
        #h(0.25em)
        #link(url)[#text(size: 6pt, fill: grey)[#url]]
      ]
    ]
  }
}

#let render-publication-reference(entry, fallback-number, reference-scope: "") = {
  let number = entry.at("number", default: fallback-number)
  let display-date = entry.at("display_date", default: "Date de publication non précisée")
  let source-urls = entry.at("source_urls", default: ())
  let target-id = entry.at(
    "target_id",
    default: "publication-source-legacy-" + str(number),
  )
  let entry-text = entry.at("text", default: "")
  block(above: 4pt, below: 5pt)[
    #text(size: 8pt, fill: grey)[#number. #display-date]
    #if entry.at("role_label", default: none) != none [
      #h(0.5em)
      #text(size: 7pt, weight: "bold", fill: grey)[#entry.role_label]
    ]
    #linebreak()
    #v(2pt)
    #if source-urls.len() > 0 [
      #let primary-url = source-urls.first()
      #link(primary-url)[#semantic-or-plain(entry-text, entry.at("semantic_spans", default: none))]
      #if entry-text != primary-url [
        #linebreak()
        #text(size: 7pt, fill: grey)[#number ]
        #link(primary-url)[#text(size: 7pt, fill: grey)[#primary-url]]
      ]
      #for url in source-urls.slice(1) [
        #linebreak()
        #text(size: 7pt, fill: grey)[#number ]
        #link(url)[#text(size: 7pt, fill: grey)[#url]]
      ]
    ] else [
      #semantic-or-plain(entry-text, entry.at("semantic_spans", default: none))
    ]
    #label(reference-scope + target-id)
  ]
}

#let publication-styled-table(
  columns,
  cell-align: auto,
  title: none,
  breakable: false,
  inset: (x: 4pt, y: 3pt),
  ..content,
) = {
  let cells = content.pos()
  let column-count = columns.len()
  let header-cells = cells.slice(0, column-count).map(cell => [
    #set text(weight: "bold", fill: white)
    #cell
  ])
  let body-cells = cells.slice(column-count).enumerate().map(((index, cell)) => {
    let row = calc.floor(index / column-count)
    if calc.rem(row, 2) == 0 {
      [#block(sticky: true, breakable: false, above: 0pt, below: 0pt)[#cell]]
    } else {
      [#block(breakable: false, above: 0pt, below: 0pt)[#cell]]
    }
  })
  let body-row-count = calc.floor((cells.len() - column-count) / column-count)

  set text(size: 8pt)
  let rendered-table = align(center, table(
    columns: columns,
    inset: inset,
    align: (x, y) => {
      if y == 0 { left } else { cell-align.at(x) }
    },
    fill: (_, y) => if y == 0 { rgb("#5D7374") } else { none },
    stroke: 0.4pt + rgb("#AEB7C0"),
    table.header(repeat: true, ..header-cells),
    ..body-cells,
  ))

  if body-row-count <= 12 {
    block(
      breakable: breakable,
      above: if title == none { 0pt } else { 6pt },
    )[
      #if title != none [
        #title
        #v(4pt)
      ]
      #rendered-table
    ]
  } else {
    if title != none [
      #block(sticky: true, above: 6pt)[
        #title
        #v(4pt)
      ]
    ]
    rendered-table
  }
}

#let render-opinion-pipe-cell(cell) = {
  for segment in cell.segments {
    if segment.code {
      text(font: code-font-families, size: 8pt, hyphenate: false)[
        #publication-display-text(segment.display)
      ]
    } else {
      text(size: 8pt)[#publication-display-text(segment.display)]
    }
  }
}

#let render-opinion-pipe-table(pipe-table) = {
  let columns = pipe-table.column_weights.map(weight => weight * 1fr)
  let cell-align = pipe-table.alignment.map(value => {
    if value == "right" { right } else if value == "center" { center } else { left }
  })
  let header = pipe-table.header.map(cell => render-opinion-pipe-cell(cell))
  let rows = pipe-table.rows.map(row => row.map(cell => render-opinion-pipe-cell(cell))).flatten()
  publication-styled-table(
    columns,
    cell-align: cell-align,
    breakable: true,
    inset: (x: 4pt, y: 4pt),
    ..(header + rows),
  )
}

#let render-opinion-text(spans, fallback, pipe-table: none) = {
  if pipe-table != none {
    let prefix = pipe-table.at("prefix", default: "")
    let suffix = pipe-table.at("suffix", default: "")
    if pipe-table.wide [
      #if prefix != "" [#text(prefix) #parbreak()]
      #pagebreak(weak: true)
      #set page(flipped: true)
      #render-opinion-pipe-table(pipe-table)
      #pagebreak(weak: true)
      #set page(flipped: false)
      #if suffix != "" [#parbreak() #text(suffix)]
    ] else [
      #if prefix != "" [#text(prefix) #parbreak()]
      #render-opinion-pipe-table(pipe-table)
      #if suffix != "" [#parbreak() #text(suffix)]
    ]
  } else {
    block(width: 100%)[
      #set par(justify: false)
      #if spans == none [#text(fallback)] else [
        #for span in spans [
          #if span.at("technical", default: false) [
            #text(font: code-font-families, size: 8pt, hyphenate: false)[#publication-display-text(span)]
          ] else [#text(span.text)]
        ]
      ]
    ]
  }
}

#let render-table(item) = {
  let title = if has-text(item.title) {
    [#text(size: 12pt, weight: "bold")[#semantic-or-plain(item.title, item.at("semantic_title", default: none))]]
  } else {
    none
  }

  let semantic-columns = item.at("semantic_columns", default: none)
  let semantic-cells = item.at("semantic_cells", default: none)
  let card-display-cells = item.at("card_display_cells", default: ())
  if item.columns.len() > 2 {
    block(above: 6pt)[
      #if title != none [#title]
      #for (row-index, row) in item.rows.enumerate() [
        #block(
          width: 100%,
          above: 3pt,
          below: 4pt,
          fill: rgb("#F8FAFC"),
          stroke: 0.4pt + rgb("#D0D5DD"),
          inset: 6pt,
          radius: 3pt,
          breakable: true,
        )[
          #set text(hyphenate: false)
          #set par(justify: false, leading: 1.05em, spacing: 1pt)
          #box[#text(font: code-font-families, size: 9pt, weight: "bold", hyphenate: false)[
            #semantic-or-plain(
              row.first(),
              if semantic-cells == none { none } else { semantic-cells.at(row-index * item.columns.len()) },
            )
          ]]
          #for (column-index, column) in item.columns.slice(1).enumerate() [
            #linebreak()
            #text(size: 8pt, weight: "bold")[#column: ]
            #let cell-index = row-index * item.columns.len() + column-index + 1
            #let display-row = if card-display-cells.len() > row-index { card-display-cells.at(row-index) } else { () }
            #let display-cell = if display-row.len() > column-index + 1 { display-row.at(column-index + 1) } else { none }
            #if display-cell != none [
              #text(font: code-font-families, size: 8pt, hyphenate: false)[#publication-display-text(display-cell)]
            ] else [
              #semantic-or-plain(
                row.at(column-index + 1),
                if semantic-cells == none { none } else { semantic-cells.at(cell-index) },
              )
            ]
          ]
        ]
      ]
    ]
  } else {
    let weights = item.at("column_weights", default: item.columns.map(column => 1.0))
    let widths = weights.map(weight => weight * 1fr)
    let cell-alignments = item.columns.map(column => left)
  let header-cells = item.columns.enumerate().map(((index, cell)) => [
    #semantic-or-plain(cell, if semantic-columns == none { none } else { semantic-columns.at(index) })
  ])
  let body-cells = item.rows.flatten().enumerate().map(((index, cell)) => [
    #semantic-or-plain(
      cell,
      if semantic-cells == none { none } else { semantic-cells.at(index) },
    )
  ])
  publication-styled-table(
    widths,
    cell-align: cell-alignments,
    title: title,
    ..(header-cells + body-cells),
  )
  }

  v(4pt)
  if has-text(item.caption) [
    #block(above: 2pt, below: 4pt)[
      #text(size: 9pt, fill: grey)[#semantic-or-plain(item.caption, item.at("semantic_caption", default: none))]
    ]
  ]
}

#let render-opinion-references(references, reference-scope: "") = {
  for (index, reference) in references.enumerate() [
    #if index > 0 [ · ]
    #link(label(reference-scope + reference.target_id))[#reference.label]
  ]
}

#let render-source-references(references, reference-scope: "") = {
  for (index, source-ref) in references.enumerate() [
    #if index > 0 [, ]
    #let source-number = source-ref.at("number", default: index + 1)
    #let target-id = source-ref.at("target_id", default: none)
    #let source-url = source-ref.at("url", default: none)
    #if target-id != none [
      #link(label(reference-scope + target-id))[#str(source-number)]
    ] else if source-url != none [
      #link(source-url)[#str(source-number)]
    ] else [#str(source-number)]
  ]
}

#let render-publication-provenance(document-label) = block(
  width: 100%,
  above: 6pt,
  below: 0pt,
  fill: rgb("#F8FAFC"),
  stroke: 0.4pt + rgb("#D0D5DD"),
  inset: 6pt,
  radius: 3pt,
  breakable: false,
)[
  #text(size: 8pt, weight: "bold", fill: accent)[PROVENANCE DE PUBLICATION]
  #linebreak()
  #text(size: 7.5pt, fill: grey)[Identité complète : #document-label]
]

#let render-ioc-table(groups, reference-scope: "") = {
  for group in groups {
    let hash-only = group.rows.len() > 0 and group.rows.all(row => row.type == "hash")
    let header-cells = if hash-only {
      ([Empreinte de fichier — une valeur par ligne], [Statut, relation et provenance])
    } else {
      ([Type], [Valeur défanguée], [Statut, relation et contexte], [Source [#text[n]]])
    }
    let body-cells = if hash-only {
      group.rows.map(row => (
        [
          #box[#text(font: code-font-families, size: 8pt, hyphenate: false)[#row.value]]
        ],
        [
          #text(size: 7.5pt, weight: "bold")[Ind. #row.at("indicator_status_short_label", default: row.at("indicator_status_label", default: "")) · Rel. #row.at("relationship_status_short_label", default: row.at("relationship_status_label", default: row.status_label))]
          #let context-summary = row.at("hash_context_summary", default: row.at("context_summary", default: ""))
          #let source-refs = row.at("source_refs", default: ())
          #if context-summary != "" [
            #linebreak()
            #text(size: 6.5pt, fill: grey)[#context-summary]
          ]
          #if source-refs.len() > 0 [
            #if context-summary != "" [#text(size: 6.5pt, fill: grey)[ · ]]
            #text(size: 6.5pt, fill: grey)[src ]
            #text(size: 6.5pt, fill: grey)[#render-source-references(source-refs, reference-scope: reference-scope)]
          ]
          #let draft-refs = row.at("draft_refs", default: ())
          #if draft-refs.len() > 0 [
            #if context-summary != "" or source-refs.len() > 0 [#text(size: 6.5pt, fill: grey)[ · ]]
            #render-opinion-references(draft-refs, reference-scope: reference-scope)
          ]
        ],
      )).flatten()
    } else {
      group.rows.map(row => (
        [#text(size: 7pt, weight: "bold")[#row.type_label]],
        [#text(font: code-font-families, size: 7.5pt, hyphenate: false)[#semantic-or-plain(row.value, row.value_spans)]],
        [
          #text(size: 7pt, weight: "bold")[Indicateur : #row.at("indicator_status_label", default: "")]
          #linebreak()
          #text(size: 7pt, weight: "bold")[Relation : #row.at("relationship_status_label", default: row.status_label)]
          #let context-summary = row.at("context_summary", default: "")
          #if context-summary != "" [
            #linebreak()
            #text(size: 6.5pt, fill: grey)[#context-summary]
          ]
        ],
        [
          #let source-refs = row.at("source_refs", default: ())
          #if source-refs.len() > 0 [#render-source-references(source-refs, reference-scope: reference-scope)]
          #let draft-refs = row.at("draft_refs", default: ())
          #if draft-refs.len() > 0 [
            #linebreak()
            #render-opinion-references(draft-refs, reference-scope: reference-scope)
          ]
        ],
      )).flatten()
    }
    block(sticky: true, above: 3pt, below: 1pt)[
      #text(size: 9pt, weight: "bold")[#group.heading]
    ]
    publication-styled-table(
      if hash-only {
        (0.72, 0.28).map(weight => weight * 1fr)
      } else {
        (0.7, 1.4, 3.0, 0.9).map(weight => weight * 1fr)
      },
      cell-align: if hash-only { (left, left) } else { (left, left, left, left) },
      inset: (x: 3pt, y: 1pt),
      ..(header-cells + body-cells),
    )
  }
}

#let render-figure-caption(
  number,
  caption,
  semantic-spans,
  top-space: 0pt,
  bottom-space: 0pt,
) = block(
  above: 0pt,
  below: 4pt,
  [
    #v(top-space)
    #align(center)[
      #text(size: 9pt, fill: grey, style: "italic")[
        Figure #number : #semantic-or-plain(caption, semantic-spans)
      ]
    ]
    #v(bottom-space)
  ],
)

#let render-derived-timeline-figure(data, reference-scope: "") = {
  let header-cells = ([Date], [Événement et source])
  let body-cells = data.events.map(event => (
    [#text(size: 9pt, weight: "bold")[#event.date]],
    [
      #text(size: 9pt, hyphenate: false)[#event.text]
      #let source-refs = event.at("source_refs", default: ())
      #if source-refs.len() > 0 [
        #linebreak()
        #text(size: 7.5pt, fill: grey)[Sources : #render-source-references(source-refs, reference-scope: reference-scope)]
      ]
    ],
  )).flatten()
  block(breakable: true, above: 2pt)[
    #render-figure-caption(data.figure_number, data.title, none, top-space: 5pt)
    #if has-text(data.at("caption", default: none)) [
      #text(size: 8pt, fill: grey, style: "italic")[#data.caption]
      #v(4pt)
    ]
    #publication-styled-table(
      (1.15, 4.85).map(weight => weight * 1fr),
      cell-align: (left, left),
      breakable: true,
      ..(header-cells + body-cells),
    )
  ]
}

#let render-investigation-chain-figure(data, reference-scope: "") = {
  block(breakable: true)[
    #render-figure-caption(data.figure_number, data.title, none, bottom-space: 3pt)
    #for row in data.rows [
      #block(
        width: 100%,
        above: 3pt,
        below: 3pt,
        fill: rgb("#F8FAFC"),
        stroke: 0.4pt + rgb("#D0D5DD"),
        inset: 5pt,
        radius: 3pt,
        breakable: true,
      )[
        #let finding-refs = row.at("finding_refs", default: ())
        #let evidence-refs = row.at("evidence_refs", default: ())
        #if finding-refs.len() > 0 or evidence-refs.len() > 0 [
          #render-opinion-references(finding-refs, reference-scope: reference-scope)
          #if finding-refs.len() > 0 and evidence-refs.len() > 0 [ · ]
          #render-opinion-references(evidence-refs, reference-scope: reference-scope)
        ]
      ]
    ]
  ]
}

#let render-bounded-svg-image(media-path, max-height-fraction: 0.45) = layout(size => {
  let natural-image = image(media-path)
  let natural-size = measure(natural-image)
  let max-image-height = calc.max(1pt, size.height * max-height-fraction)
  let scale = calc.min(
    1,
    calc.min(size.width / natural-size.width, max-image-height / natural-size.height),
  )
  let rendered-image = if scale < 1 {
    image(media-path, width: natural-size.width * scale)
  } else {
    natural-image
  }
  align(center)[#rendered-image]
})

#let render-infrastructure-graph-figure(data, reference-scope: "") = {
  let header-cells = ([Relation canonique, sources et preuves],)
  let body-cells = data.edges.map(edge => [
    #text(size: 8pt, weight: "bold", font: code-font-families, hyphenate: false)[#publication-display-text(edge.at("source_value_spans", default: edge.source_value))]
    #h(0.35em)
    #text(size: 8pt, fill: accent)[#edge.direction_symbol]
    #h(0.35em)
    #text(size: 8pt, weight: "bold", font: code-font-families, hyphenate: false)[#publication-display-text(edge.at("target_value_spans", default: edge.target_value))]
    #linebreak()
    #text(size: 7pt, fill: grey)[#edge.direction_label · #edge.at("relation_label", default: edge.relation) · #edge.source_label · #edge.evidence_basis_label · confiance #edge.confidence_label]
    #if edge.at("observed_at", default: none) != none [
      #linebreak()
      #text(size: 7pt, fill: grey)[Observé le #edge.observed_at]
    ]
    #let finding-refs = edge.at("finding_refs", default: ())
    #let evidence-refs = edge.at("evidence_refs", default: ())
    #if finding-refs.len() > 0 or evidence-refs.len() > 0 [
      #linebreak()
      #render-opinion-references(finding-refs, reference-scope: reference-scope)
      #if finding-refs.len() > 0 and evidence-refs.len() > 0 [ · ]
      #render-opinion-references(evidence-refs, reference-scope: reference-scope)
    ]
  ])

  [
    #block(breakable: false, below: 5pt)[
      #render-figure-caption(data.figure_number, data.title, none, bottom-space: 6pt)
      #parbreak()
      #text(size: 7pt, fill: grey)[#data.caption]
      #parbreak()
      #if data.at("media_path", default: none) != none [
        #render-bounded-svg-image(data.media_path, max-height-fraction: 0.12)
      ] else [
        #text(size: 6pt, fill: grey)[Aperçu du graphe indisponible ; relations canoniques :]
      ]
    ]
    #v(2pt)
    #publication-styled-table(
      (1fr,),
      cell-align: (left,),
      title: none,
      breakable: true,
      inset: (x: 4pt, y: 1pt),
      ..(header-cells + body-cells),
    )
    #v(3pt)
    #block(breakable: false, above: 2pt, below: 3pt)[
      #text(size: 6.5pt, fill: grey)[Légende : ]
      #text(size: 6.5pt, fill: rgb("#15803D"))[fait observé · trait plein]
      #text(size: 6.5pt, fill: grey)[ · ]
      #text(size: 6.5pt, fill: rgb("#B45309"))[affirmation fournisseur · trait discontinu]
      #text(size: 6.5pt, fill: grey)[ · ]
      #text(size: 6.5pt, fill: rgb("#2563EB"))[affirmation article · trait discontinu]
      #text(size: 6.5pt, fill: grey)[ · ]
      #text(size: 6.5pt, fill: rgb("#7C3AED"))[inférence · pointillé]
      #text(size: 6.5pt, fill: grey)[ · les résultats vides et 404 ne forment pas d'arête.]
      #if data.omitted_count > 0 [
        #linebreak()
        #text(size: 6pt, weight: "bold")[#data.omitted_count autres omis selon le plafond déterministe.]
      ]
      #if data.omitted_edge_count > 0 [
        #linebreak()
        #text(size: 6pt, weight: "bold")[#data.omitted_edge_count relations supplémentaires omises.]
      ]
    ]
  ]
}

// Preserve intrinsic dimensions and scale down to the printable frame before
// laying out the image with its caption and source note.
#let render-publication-image(media-path, caption, annotation: [], max-height-fraction: 0.82) = layout(size => {
  let natural-image = image(media-path)
  let natural-size = measure(natural-image)
  let following-content = [
    #if caption != none [
      #v(4pt)
      #caption
    ]
    #annotation
  ]
  let max-image-height = calc.max(1pt, size.height * max-height-fraction)
  let scale = calc.min(
    1,
    calc.min(size.width / natural-size.width, max-image-height / natural-size.height),
  )
  let rendered-image = if scale < 1 {
    image(media-path, width: natural-size.width * scale)
  } else {
    natural-image
  }

  block(breakable: false, width: 100%)[
    #align(center)[#rendered-image]
    #following-content
  ]
})

#let render-diagram(item) = {
  let legacy-renderer = item.at("legacy_renderer", default: false)
  if not legacy-renderer {
  let caption = if legacy-renderer {
    "Rendu hérité"
  } else if has-text(item.caption) {
    item.caption
  } else {
    item.title
  }
  let semantic-caption = if legacy-renderer {
    none
  } else if has-text(item.caption) {
    item.at("semantic_caption", default: none)
  } else {
    item.at("semantic_title", default: none)
  }
  let title = item.at("title", default: none)
  let status-label = item.at("status_label", default: none)
  let caption-content = [
    #if has-text(title) and (has-text(status-label) or item.at("show_title", default: false)) [
      #block(above: 2pt, below: 2pt)[
        #text(size: 9pt, weight: "bold")[#title]
      ]
    ]
    #if has-text(status-label) [
      #block(above: 2pt, below: 2pt)[
        #text(size: 8pt, weight: "bold", fill: rgb("#315D7C"))[#status-label]
      ]
    ]
    #if has-text(caption) [
      #render-figure-caption(item.figure_number, caption, semantic-caption)
    ]
    #if has-text(item.at("status_legend", default: none)) [
      #block(above: 3pt, below: 2pt)[
        #text(size: 6.5pt, fill: grey)[#item.status_legend]
      ]
    ]
  ]
  if has-text(item.at("status_legend", default: none)) {
    render-publication-image(item.media_path, caption-content, max-height-fraction: 0.52)
  } else {
    render-publication-image(item.media_path, caption-content, max-height-fraction: 0.35)
  }
  }
}

#let render-figure(item) = {
  let caption-content = if has-text(item.caption) {
    render-figure-caption(
      item.figure_number,
      item.caption,
      item.at("semantic_caption", default: none),
    )
  } else {
    none
  }
  let render-figure-annotation(body) = block(above: 1pt, below: 5pt)[
    #set par(leading: 1.4em, spacing: 0pt)
    #text(size: 7pt, fill: grey)[#body]
  ]
  let annotation = [
    #render-figure-annotation[Documenté, non observé — visuel reproduit depuis une source.]
    #if has-text(item.source_note) [
      #render-figure-annotation[Source : #item.source_note]
    ]
    #let attribution = item.at("attribution", default: none)
    #if has-text(attribution) [
      #render-figure-annotation[Attribution : #attribution]
    ]
  ]
  render-publication-image(
    item.media_path,
    caption-content,
    annotation: annotation,
    max-height-fraction: 0.15,
  )
}

#let render-chart(item) = {
  let caption = if has-text(item.caption) { item.caption } else { item.title }
  let semantic-caption = if has-text(item.caption) {
    item.at("semantic_caption", default: none)
  } else {
    item.at("semantic_title", default: none)
  }
  let caption-content = if has-text(caption) {
    render-figure-caption(item.figure_number, caption, semantic-caption)
  } else {
    none
  }
  render-publication-image(item.media_path, caption-content, max-height-fraction: 0.55)
}

#let render-analyst-observation(item, reference-scope: "") = noteAnalyste[
  #render-opinion-text(item.at("text_spans", default: none), item.text)
  #let finding-refs = item.at("finding_refs", default: ())
  #let evidence-refs = item.at("evidence_refs", default: ())
  #if finding-refs.len() > 0 or evidence-refs.len() > 0 [
    #v(3pt)
    #text(size: 7pt, fill: grey)[Références : ]
    #render-opinion-references(finding-refs, reference-scope: reference-scope)
    #if finding-refs.len() > 0 and evidence-refs.len() > 0 [ · ]
    #render-opinion-references(evidence-refs, reference-scope: reference-scope)
  ]
]

#let render-ioc-item(item, reference-scope: "") = {
  list.item[
    #text(font: code-font-families, size: 8.5pt, hyphenate: false)[#publication-display-text(item.value)]
    #let indicator-status = item.at("indicator_status_label", default: none)
    #let relationship-status = item.at("relationship_status_label", default: none)
    #if has-text(indicator-status) or has-text(relationship-status) [
      #linebreak()
      #text(size: 7pt, fill: grey)[
        #if has-text(indicator-status) [Indicateur : #text(indicator-status)]
        #if has-text(indicator-status) and has-text(relationship-status) [ · ]
        #if has-text(relationship-status) [Relation : #text(relationship-status)]
      ]
    ]
    #let source-refs = item.at("source_refs", default: ())
    #let opinion-refs = item.at("draft_refs", default: ())
    #if source-refs.len() > 0 or opinion-refs.len() > 0 [
      #text(size: 7pt, fill: grey)[ · ]
      #render-source-references(source-refs, reference-scope: reference-scope)
      #if source-refs.len() > 0 and opinion-refs.len() > 0 [ · ]
      #render-opinion-references(opinion-refs, reference-scope: reference-scope)
    ]
  ]
}

#let render-ioc-groups(
  groups,
  skip-first-heading: false,
  skip-first-item: false,
  reference-scope: "",
) = {
  for (group-index, group) in groups.enumerate() {
    if not (skip-first-heading and skip-first-item and group-index == 0 and group.items.len() == 1) {
      block(above: 5pt, below: 2pt)[
        #if not (skip-first-heading and group-index == 0) [
          #text(size: 10pt, weight: "bold")[#group.heading]
        ]
      ]
    }
    for (item-index, item) in group.items.enumerate() {
      if not (skip-first-item and group-index == 0 and item-index == 0) {
        render-ioc-item(item, reference-scope: reference-scope)
      }
    }
  }
}

#let render-ioc-section(title, description, groups, reference-scope: "") = {
  if groups.len() == 0 {
    block(breakable: false)[
      #section-title[#title]
      #if description != none [
        #text(size: 8pt, fill: grey)[#description]
      #linebreak()
      ]
    ]
  } else {
    let first-group = groups.first()
    let first-item = first-group.items.first()
    block(breakable: false)[
      #section-title[#title]
      #if description != none [
        #text(size: 8pt, fill: grey)[#description]
      ]
      #v(6pt)
      #block(above: 4pt, below: 2pt)[
        #text(size: 10pt, weight: "bold")[#first-group.heading]
      ]
      #render-ioc-item(first-item, reference-scope: reference-scope)
    ]
    render-ioc-groups(
      groups,
      skip-first-heading: true,
      skip-first-item: true,
      reference-scope: reference-scope,
    )
  }
}

#let render-body-block(item, reference-scope: "") = {
  let body = if item.type == "section_heading" {
    block(sticky: true, above: 7pt, below: 5pt)[
      #heading(level: 3)[#item.text]
    ]
  } else if item.type == "paragraph" {
    [
      #block(above: 0pt, below: 3pt)[
        #semantic-or-plain(item.text, item.at("semantic_spans", default: none))
        #if item.at("anchor_target", default: none) != none [#label(item.anchor_target)]
      ]
      #parbreak()
    ]
  } else if item.type == "analyst_observation" {
    render-analyst-observation(item, reference-scope: reference-scope)
  } else if item.type == "table" {
    render-table(item)
  } else if item.type == "diagram" {
    render-diagram(item)
  } else if item.type == "figure" {
    render-figure(item)
  } else if item.type == "chart" {
    render-chart(item)
  } else {
    panic("unsupported publication body block type: " + item.type)
  }
  render-target(item.at("target_id", default: none), body)
}

#let render-publication-body(publication, reference-scope: "") = {
  for content_section in publication.content_sections {
    if content_section.type == "references" {
      let legacy-timeline = content_section.at("timeline", default: ())
      let has-entries = content_section.at("entries", default: none) != none
      let has-legacy-chronology = (
        not has-entries
        and legacy-timeline.len() > 0
        and legacy-timeline.all(
          event => event.at("number", default: none) == none
            and event.at("target_id", default: none) == none,
        )
      )
      let entries = if has-entries {
        content_section.entries
      } else if has-legacy-chronology {
        ()
      } else {
        legacy-timeline
      }
      let blocks = content_section.at("blocks", default: ())
      if entries.len() > 0 or has-legacy-chronology or blocks.len() > 0 [
        #if content_section.at("show_heading", default: true) [
          #section-title[RÉFÉRENCES]
        ]
        #if has-legacy-chronology [
          #render-publication-timeline(legacy-timeline, reference-scope: reference-scope)
        ] else [
          #for (index, entry) in entries.enumerate() [#render-publication-reference(entry, index + 1, reference-scope: reference-scope)]
        ]
        #for item in blocks {
          render-body-block(item, reference-scope: reference-scope)
        }
      ]
    } else if content_section.type == "synthesis" [
      #section-title[SYNTHÈSE]
      #for item in content_section.at("blocks", default: ()) {
        render-body-block(item, reference-scope: reference-scope)
      }
    ] else if content_section.type == "chronology" [
      #section-title[CHRONOLOGIE]
      #let derived-figure = content_section.at("derived_timeline_figure", default: none)
      #if derived-figure == none [
        #render-publication-timeline(
          content_section.timeline,
          ordered: true,
          reference-scope: reference-scope,
        )
      ] else [
        #render-derived-timeline-figure(derived-figure, reference-scope: reference-scope)
        // The derived figure only carries exact ISO-dated events.  Keep every
        // other canonical chronology entry visible instead of dropping it.
        #let figure-texts = derived-figure.at("events", default: ()).map(event => event.text)
        #let undated-entries = content_section.timeline.filter(
          entry => not figure-texts.contains(entry.text),
        )
        #if undated-entries.len() > 0 [
          #v(8pt)
          #render-publication-timeline(
            undated-entries,
            ordered: true,
            reference-scope: reference-scope,
          )
        ]
      ]
    ] else if content_section.type == "limitations" [
      #section-title[LIMITES DES PREUVES]
      #for item in content_section.items [
        #render-opinion-text(item.at("text_spans", default: none), item.text)
        #let source-refs = item.at("source_refs", default: ())
        #if source-refs.len() > 0 [
          #parbreak()
          #text(size: 7.5pt, fill: grey)[Sources : ]
          #for (index, source-ref) in source-refs.enumerate() [
            #if index > 0 [ · ]
            #link(source-ref.url)[#str(source-ref.number)]
          ]
        ]
        #parbreak()
      ]
      #let source-list = content_section.at("source_list", default: ())
      #if source-list.len() > 0 [
        #v(3pt)
        #text(size: 8pt, weight: "bold")[Sources]
        #for source in source-list [
          #linebreak()
          #text(size: 7pt, fill: grey)[#str(source.number) · ]
          #link(source.url)[#text(size: 7pt, fill: grey)[#source.url]]
        ]
      ]
    ] else if content_section.type == "analyst_opinion" [
      #let opinion-blocks = content_section.at("blocks", default: ())
      #let unanchored-observations = content_section.at("unanchored_observations", default: ())
      #let unresolved-points = content_section.at("unresolved_points", default: ())
      #let protocol-figures = content_section.at("protocol_figures", default: ())
      #let has-opinion-content = (
        opinion-blocks.len() > 0
        or content_section.at("has_anchored_observations", default: false)
        or unanchored-observations.len() > 0
        or unresolved-points.len() > 0
        or protocol-figures.len() > 0
        or content_section.at("investigation_chain_figure", default: none) != none
        or content_section.at("infrastructure_graph_figure", default: none) != none
      )
      #if has-opinion-content [
      #section-title[AVIS DE L'ANALYSTE]
      #for item in opinion-blocks [
        #heading(level: 3)[#item.number #text(item.heading)]
        #text(size: 8pt, weight: "bold")[#text(item.qualification_label)]
        #h(1em)
        #text(size: 8pt)[Confiance : #item.certainty_percent%]
        #parbreak()
        #render-opinion-text(
          none,
          item.text,
          pipe-table: item.at("pipe_table", default: none),
        )
        #parbreak()
        #if item.finding_refs.len() > 0 or item.evidence_refs.len() > 0 [
          #text(size: 7pt, fill: grey)[Références : ]
          #render-opinion-references(item.finding_refs, reference-scope: reference-scope)
          #if item.finding_refs.len() > 0 and item.evidence_refs.len() > 0 [ · ]
          #render-opinion-references(item.evidence_refs, reference-scope: reference-scope)
          #parbreak()
        ]
      ]
      #for figure in protocol-figures [
        #v(8pt)
        #render-diagram(figure)
      ]
      #if content_section.at("investigation_chain_figure", default: none) != none [
        #v(8pt)
        #render-investigation-chain-figure(content_section.investigation_chain_figure, reference-scope: reference-scope)
      ]
      #if content_section.at("infrastructure_graph_figure", default: none) != none [
        #v(8pt)
        #render-infrastructure-graph-figure(content_section.infrastructure_graph_figure, reference-scope: reference-scope)
      ]
      #if unresolved-points.len() > 0 [
        #heading(level: 3)[Points non résolus]
        #for point in unresolved-points [
          #list.item[#point]
        ]
      ]
      #for item in unanchored-observations [
        #v(6pt)
        #render-analyst-observation(item, reference-scope: reference-scope)
      ]
      ]
    ] else if content_section.type == "technical_materials" [
      #v(12pt)
      #section-title[#text(content_section.heading)]
      #text(size: 8pt)[ · Valeurs reproduites en clair telles qu’observées dans l’échantillon.]
      #if content_section.editorial_text != none [
        #v(5pt)
        #text(size: 8pt)[#text(content_section.editorial_text)]
      ]
      #v(5pt)
      #for item in content_section.items [
        #block(breakable: false, width: 100%, above: 3pt, below: 6pt)[
          #text(size: 8pt, weight: "bold")[#text(item.type_label)]
          #text(size: 8pt)[ · Rôle : #text(item.role)]
          #linebreak()
          #text(size: 7pt)[Valeur : ]
          #box[#text(font: code-font-families, size: 7pt, hyphenate: false)[#text(item.value)]]
          #linebreak()
          #text(size: 7pt)[Emplacement dans l’échantillon : ]
          #text(font: code-font-families, size: 7pt, hyphenate: false)[#text(item.sample_location)]
          #linebreak()
          #text(size: 7pt)[SHA-256 de l’échantillon : ]
          #text(font: code-font-families, size: 7pt, hyphenate: false)[#text(item.sample_sha256)]
          #linebreak()
          #text(size: 7pt)[Observation de preuve : ]
          #link(label(reference-scope + item.evidence_ref.target_id))[#text(item.evidence_ref.label)]
        ]
      ]
      #text(size: 7pt, fill: grey)[#text(content_section.attacker_identifier_note)]
    ] else if content_section.type == "detection" [
      #section-title[DÉTECTION]
      #block(
        width: 100%,
        fill: rgb("#fff8e8"),
        stroke: 0.5pt + rgb("#d6a84f"),
        inset: 8pt,
        radius: 4pt,
      )[
        #text(size: 9pt, weight: "bold")[#content_section.validation_banner]
        #if content_section.validation_reason != none [
          #parbreak()
          #text(size: 8pt)[Motif : #content_section.validation_reason]
        ]
      ]
      #parbreak()
      #text(size: 8pt, weight: "bold")[Règle YARA candidate · #content_section.version]
      #parbreak()
      #text(size: 7pt)[Tests : #content_section.positive_real_count positifs réels · #content_section.positive_synthetic_count positifs synthétiques · #content_section.goodware_tested_count fichiers bénins testés · #content_section.false_positive_count faux positifs]
      #parbreak()
      #if content_section.engine_version != none [
        #text(size: 7pt)[Moteur YARA : #content_section.engine_version]
        #parbreak()
      ]
      #text(size: 7pt)[Règle créée le #content_section.created_date]
      #if content_section.tested_date != none [
        #text(size: 7pt)[ · dernier test le #content_section.tested_date]
      ] else [
        #text(size: 7pt)[ · aucun test réalisé]
      ]
      #parbreak()
      #if content_section.coverage_statement != none [
        #text(size: 7pt, weight: "bold")[Couverture : ]#text(size: 7pt)[#content_section.coverage_statement]
        #parbreak()
      ]
      #for limitation in content_section.limitations [
        #text(size: 7pt, fill: grey)[Limite : #limitation]
        #parbreak()
      ]
      #if content_section.retrohunt_submitted == false [
        #text(size: 7pt)[Aucun Retrohunt soumis.]
        #parbreak()
      ] else if content_section.retrohunt_submitted == true [
        #text(size: 7pt)[Retrohunt soumis.]
        #parbreak()
      ]
      #heading(level: 3)[Code source]
      #block(
        width: 100%,
        fill: rgb("#f5f6f8"),
        stroke: 0.4pt + rgb("#d7dbe0"),
        inset: 7pt,
        radius: 3pt,
        breakable: true,
      )[
        #set text(font: code-font-families, size: 5.5pt)
        #set par(leading: 0.82em, spacing: 0pt)
        #for line in content_section.code_lines {
          publication-detection-code-line(line)
        }
      ]
    ] else if content_section.type == "ioc" [
      #v(16pt)
      #render-ioc-section(
        "IOC de l'article",
        none,
        content_section.source_indicators,
        reference-scope: reference-scope,
      )
      #if content_section.original_indicators.len() > 0 [
        #v(8pt)
        #render-ioc-section(
          "IOC originaux",
          "Découverts dans les sources de ce run et associés au sujet par l'extrait cité.",
          content_section.original_indicators,
          reference-scope: reference-scope,
        )
      ]
      #if content_section.candidate_indicators.len() > 0 [
        #v(8pt)
        #render-ioc-section(
          "Pistes non confirmées",
          "Éléments à examiner, sans promotion au statut d'IOC confirmé.",
          content_section.candidate_indicators,
          reference-scope: reference-scope,
        )
      ]
      #if content_section.omitted_invalid_url_count > 0 [
        #text(size: 8pt, fill: grey)[#content_section.omitted_invalid_url_count URL non valide(s) omise(s) des listes.]
      ]
    ] else if content_section.type == "technical_annex" [
      #pagebreak(weak: true)
      #set page(flipped: true)
      #let ioc_rows = content_section.at("ioc_rows", default: ())
      #let ioc_row_groups = content_section.at("ioc_row_groups", default: ())
      #let legacy_indicators = content_section.at("indicators", default: none)
      #let legacy_original_indicators = content_section.at("original_indicators", default: none)
      #let identity-provenance = content_section.at("publication_identity_provenance", default: none)
      #if has-text(identity-provenance) [
        #section-title[PROVENANCE DE PUBLICATION]
        #text(size: 7.5pt, fill: grey)[Identité de base : #identity-provenance]
        #v(5pt)
      ]
      #if ioc_rows.len() > 0 [
        #v(16pt)
        #section-title[ANNEXE TECHNIQUE — inventaire exhaustif des IOC]
        #render-ioc-table(ioc_row_groups, reference-scope: reference-scope)
      ] else if legacy_indicators != none [
        #ioc-list(
          title: [IOC],
          note: content_section.at("original_indicator_note", default: none),
          ips: legacy_indicators.at("ips", default: ()),
          domains: legacy_indicators.at("domains", default: ()),
          urls: legacy_indicators.at("urls", default: ()),
          emails: legacy_indicators.at("emails", default: ()),
          hashes: legacy_indicators.at("hashes", default: ()),
        )
      ]
      #let implant_identifier_rows = content_section.at("implant_identifier_rows", default: ())
      #if implant_identifier_rows.len() > 0 [
        #v(10pt)
        #section-title[IDENTIFIANT C2 DE L’IMPLANT — IOC]
        #text(size: 7pt, fill: grey)[Valeur observée dans l’échantillon; identifiant passif, sans authentification.]
        #for item in implant_identifier_rows [
          #block(breakable: false, width: 100%, above: 3pt, below: 5pt)[
            #text(size: 8pt, weight: "bold")[#text(item.type_label)]
            #text(size: 8pt)[ · Rôle : #text(item.role)]
            #linebreak()
            #text(size: 7pt)[Valeur : ]
            #box[#text(font: code-font-families, size: 7pt, hyphenate: false)[#text(item.value)]]
            #linebreak()
            #text(size: 7pt)[Échantillon : ]
            #text(font: code-font-families, size: 7pt, hyphenate: false)[#text(item.sample_sha256)]
            #text(size: 7pt)[ · Emplacement : ]
            #text(font: code-font-families, size: 7pt, hyphenate: false)[#text(item.sample_location)]
            #linebreak()
            #text(size: 7pt)[Preuve : ]
            #link(label(reference-scope + item.evidence_ref.target_id))[#text(item.evidence_ref.label)]
          ]
        ]
      ]
      #if ioc_rows.len() == 0 and legacy_indicators != none and legacy_original_indicators != none [
        #v(8pt)
        #ioc-list(
          title: [IOC originaux à lien non démontré],
          note: content_section.at("original_indicator_note", default: none),
          ips: legacy_original_indicators.at("ips", default: ()),
          domains: legacy_original_indicators.at("domains", default: ()),
          urls: legacy_original_indicators.at("urls", default: ()),
          emails: legacy_original_indicators.at("emails", default: ()),
          hashes: legacy_original_indicators.at("hashes", default: ()),
        )
      ]
      #let opinion_refs = content_section.at("opinion_refs", default: ())
      #if opinion_refs.len() > 0 [
        #v(10pt)
        #section-title[ANNEXE DE TRAÇABILITÉ]
        #text(size: 8pt, fill: grey)[Les renvois courts du corps désignent les identifiants canoniques suivants.]
        #let finding-refs = opinion_refs.filter(item => item.kind_label == "Finding")
        #let evidence-refs = opinion_refs.filter(item => item.kind_label == "Évidence")
        #for (group-title, refs) in (("Findings", finding-refs), ("Évidences", evidence-refs)) [
          #if refs.len() > 0 [
            #v(4pt)
            #text(size: 8pt, weight: "bold")[#group-title]
            #grid(
              columns: (1fr, 1fr),
              column-gutter: 8pt,
              row-gutter: 2pt,
              ..refs.map(item => [
                #block(breakable: false)[
                  #text(size: 7.5pt, weight: "bold")[#item.label #label(reference-scope + item.target_id)]
                  #h(0.35em)
                  #link(item.href)[
                    #text(font: code-font-families, size: 7.5pt, hyphenate: false)[
                      #publication-display-text(item.reference_id_spans)
                    ]
                  ]
                ]
              ]),
            )
          ]
        ]
      ]
      #let figure-provenance = content_section.at("protocol_figure_provenance", default: ())
      #if figure-provenance.len() > 0 [
        #v(10pt)
        #section-title[PROVENANCE DES FIGURES D'AVIS]
        #for figure in figure-provenance [
          #block(above: 3pt, below: 5pt, breakable: true)[
            #text(size: 8pt, weight: "bold")[#figure.kind_label]
            #linebreak()
            #text(size: 7pt)[Identifiant d'actif : ]
            #text(font: code-font-families, size: 7pt, hyphenate: false)[#figure.asset_id]
            #for field in ("model_sha256", "spec_sha256", "asset_sha256", "preview_sha256", "pin_sha256") [
              #if figure.at(field, default: none) != none [
                #linebreak()
                #text(size: 7pt)[#field : ]
                #text(font: code-font-families, size: 6.5pt, hyphenate: false)[#figure.at(field)]
              ]
            ]
          ]
        ]
      ]
      #render-publication-provenance(publication.at("document_label"))
      #set page(flipped: false)
    ] else {
      panic("unsupported publication content section type: " + content_section.type)
    }
  }
}

#let render-publication(publication) = {
  let document-label = publication.at("document_label", default: none)
  if not has-text(document-label) {
    panic("publication render-data requires document_label")
  }
  let has-technical-annex = publication.content_sections.any(
    content-section => content-section.type == "technical_annex",
  )
  [
    #render-target(
      publication.at("article_title_target_id", default: none),
      heading(level: 1)[#publication.title],
    )
    #let overview = publication.at("overview", default: none)
    #if overview != none [
      #block(
        width: 100%,
        above: 2pt,
        below: 8pt,
        fill: rgb("#F1F6F5"),
        stroke: 0.5pt + rgb("#CAD8D6"),
        inset: (x: 9pt, y: 7pt),
        radius: 4pt,
        breakable: false,
      )[
        #grid(
          columns: (1fr, auto),
          column-gutter: 12pt,
          [
            #text(size: 7pt, weight: "bold", fill: accent)[STATUT]
            #linebreak()
            #text(size: 8.5pt)[#overview.status_label]
          ],
          [
            #text(size: 7pt, weight: "bold", fill: accent)[SOURCES]
            #linebreak()
            #text(size: 8.5pt)[#str(overview.source_count)]
          ],
        )
      ]
    ]
    #render-publication-body(publication)
    #if not has-technical-annex [
      #render-publication-provenance(document-label)
    ]
  ]
}
