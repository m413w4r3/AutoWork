#import "../UTILS/helpers.typ": section-title, timeline, ioc-list, semantic-text, semantic-or-plain, code-font-families
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

#let render-table(item) = {
  let title = if has-text(item.title) {
    [#text(size: 12pt, weight: "bold")[#semantic-or-plain(item.title, item.at("semantic_title", default: none))]]
  } else {
    none
  }

  let weights = item.at("column_weights", default: item.columns.map(column => 1.0))
  let widths = weights.map(weight => weight * 1fr)
  let cell-alignments = item.columns.map(column => left)
  let semantic-columns = item.at("semantic_columns", default: none)
  let semantic-cells = item.at("semantic_cells", default: none)
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

  v(4pt)
  if has-text(item.caption) [
    #block(above: 2pt, below: 4pt)[
      #text(size: 9pt, fill: grey)[#semantic-or-plain(item.caption, item.at("semantic_caption", default: none))]
    ]
  ]
}

#let render-ioc-table(groups, reference-scope: "") = {
  let header-cells = (
    [Type],
    [Valeur défanguée],
    [Contexte et statut],
    [Source [n]],
  )
  for group in groups {
    let body-cells = group.rows.map(row => (
      [#text(size: 7.5pt)[#row.type_label]],
      [#text(font: code-font-families, size: 7pt)[#semantic-or-plain(row.value, row.value_spans)]],
      [#text(size: 7pt)[#row.context_summary]],
      [
        #for (index, source-ref) in row.at("source_refs", default: ()).enumerate() [
          #if index > 0 [, ]
          #let source-number = source-ref.at("number", default: index + 1)
          #let target-id = source-ref.at("target_id", default: none)
          #let source-url = source-ref.at("url", default: none)
          #if target-id != none [
            #link(label(reference-scope + target-id))[#text(size: 7pt, weight: "bold")[#str(source-number)]]
          ] else if source-url != none [
            #link(source-url)[#text(size: 7pt, weight: "bold")[#str(source-number)]]
          ] else [
            #text(size: 7pt, weight: "bold")[#str(source-number)]
          ]
        ]
      ],
    )).flatten()
    block(sticky: true, above: 5pt, below: 2pt)[
      #text(size: 9pt, weight: "bold")[#group.heading]
    ]
    publication-styled-table(
      (0.8, 2.2, 2.5, 0.7).map(weight => weight * 1fr),
      cell-align: (left, left, left, left),
      inset: (x: 3pt, y: 2pt),
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

#let render-derived-timeline-figure(data) = {
  [
    #render-figure-caption(data.figure_number, data.title, none, top-space: 10pt)
    #text(size: 7pt, fill: grey)[Unité : #data.axis_unit. Dates sans heure ni fuseau.]
    #for (event-index, event) in data.events.enumerate() [
      #enum.item[
        #text(size: 9pt, weight: "bold")[#(event.date + " : ")]
        #text(hyphenate: false)[#event.text]
        #linebreak()
        #text(size: 8pt, fill: grey)[Jour #event.offset_days]
        #if event.sources.len() > 0 [
          #linebreak()
          #text(size: 7.5pt, fill: grey)[Sources : ]
          #for (source-index, source) in event.sources.enumerate() [
            #if source-index > 0 [ · ]
            #link(source.url)[Source #str(source-index + 1)]
            #linebreak()
            #text(size: 5.5pt, fill: grey)[Source SHA-256 : #publication-display-text(source.source_sha256_spans)]
            #linebreak()
            #text(size: 5.5pt, fill: grey)[Preuve SHA-256 : #publication-display-text(source.evidence_sha256_spans)]
          ]
        ]
      ]
    ]
    #text(size: 5.5pt, fill: grey)[Publication V5 SHA-256 : #publication-display-text(data.publication_sha256_spans)]
    #text(size: 5.5pt, fill: grey)[Identité de figure : #publication-display-text(data.figure_id_spans)]
  ]
}

#let render-investigation-chain-figure(data) = {
  let header-cells = ([Paragraphe cible], [Relation], [Observation et citations])
  let body-cells = data.rows.map(row => (
    [
      #text(size: 6.5pt, weight: "bold")[#row.anchor]
      #parbreak()
      #text(size: 6pt, fill: grey)[#row.anchor_text]
    ],
    [#text(size: 8pt, fill: accent)[→ #row.relation]],
    [
      #text(size: 6pt, fill: grey)[Observation #row.state · #row.observation_id]
      #parbreak()
      #row.text
      #if row.finding_refs.len() > 0 [
        #parbreak()
        #text(size: 6pt, fill: grey)[Findings : ]
        #for (index, reference) in row.finding_refs.enumerate() [
          #if index > 0 [ · ]
          #link(reference.href)[#reference.id]
        ]
      ]
      #if row.evidence_refs.len() > 0 [
        #parbreak()
        #text(size: 6pt, fill: grey)[Évidences : ]
        #for (index, reference) in row.evidence_refs.enumerate() [
          #if index > 0 [ · ]
          #link(reference.href)[#reference.id]
        ]
      ]
    ],
  )).flatten()

  [
    #render-figure-caption(data.figure_number, data.title, none, bottom-space: 6pt)
    #publication-styled-table(
      (1.35, 0.75, 3.4).map(weight => weight * 1fr),
      cell-align: (left, center, left),
      title: none,
      ..(header-cells + body-cells),
    )
    #text(size: 5.5pt, fill: grey)[SHA-256 de l'avis #data.opinion_id, version #data.opinion_version : #publication-display-text(data.opinion_sha256_spans)]
    #text(size: 5.5pt, fill: grey)[Publication V5 SHA-256 : #publication-display-text(data.base_publication_sha256_spans)]
    #text(size: 5.5pt, fill: grey)[Identité de figure : #publication-display-text(data.figure_id_spans)]
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

#let render-infrastructure-graph-figure(data) = {
  let header-cells = ([Relation canonique, sources et preuves],)
  let body-cells = data.edges.map(edge => [
    #text(size: 5pt, weight: "bold")[#publication-display-text(edge.at("source_value_spans", default: edge.source_value))]
    #h(0.35em)
    #text(size: 6pt, fill: accent)[#edge.direction_symbol]
    #h(0.35em)
    #text(size: 5pt, weight: "bold")[#publication-display-text(edge.at("target_value_spans", default: edge.target_value))]
    #linebreak()
    #text(size: 4.8pt, fill: grey)[#edge.direction_label · #edge.relation · #edge.source_label · #edge.evidence_basis_label · confiance #edge.confidence_label]
    #if edge.at("observed_at", default: none) != none [
      #linebreak()
      #text(size: 4.5pt, fill: grey)[Collecte ou résolution : #edge.observed_at]
    ]
    #for reference in edge.at("evidence_refs", default: ()) [
      #linebreak()
      #if reference.at("observation_id", default: none) != none [
        #text(size: 4.5pt, fill: grey)[#reference.source_name · observation #reference.observation_id · SHA-256 #publication-display-text(reference.at("sha256_spans", default: reference.sha256))]
      ] else [
        #text(size: 4.5pt, fill: grey)[#reference.source_name · source #reference.source_document_id · SHA-256 #publication-display-text(reference.at("sha256_spans", default: reference.sha256))]
      ]
      #if reference.at("source_version", default: none) != none [
        #text(size: 4.5pt, fill: grey)[ · version #reference.source_version]
      ]
    ]
  ])

  [
    #block(breakable: false, below: 5pt)[
      #render-figure-caption(data.figure_number, data.title, none, bottom-space: 6pt)
      #parbreak()
      #text(size: 7pt, fill: grey)[#data.caption]
      #parbreak()
      #if data.at("media_path", default: none) != none [
        #render-bounded-svg-image(data.media_path, max-height-fraction: 0.08)
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
      #text(size: 5.5pt, fill: grey)[Légende : ]
      #text(size: 5.5pt, fill: rgb("#15803D"))[fait observé · trait plein]
      #text(size: 5.5pt, fill: grey)[ · ]
      #text(size: 5.5pt, fill: rgb("#B45309"))[affirmation fournisseur · trait discontinu]
      #text(size: 5.5pt, fill: grey)[ · ]
      #text(size: 5.5pt, fill: rgb("#2563EB"))[affirmation article · trait discontinu]
      #text(size: 5.5pt, fill: grey)[ · ]
      #text(size: 5.5pt, fill: rgb("#7C3AED"))[inférence · pointillé]
      #text(size: 5.5pt, fill: grey)[ · les résultats vides et 404 ne forment pas d'arête.]
      #if data.omitted_count > 0 [
        #linebreak()
        #text(size: 6pt, weight: "bold")[#data.omitted_count autres omis selon le plafond déterministe.]
      ]
      #if data.omitted_edge_count > 0 [
        #linebreak()
        #text(size: 6pt, weight: "bold")[#data.omitted_edge_count relations supplémentaires omises.]
      ]
    ]
    #block(above: 2pt)[
      #text(size: 3.5pt, fill: grey)[
        Graphe SHA-256 : #publication-display-text(data.graph_sha256_spans) ·
        Pub V5 : #publication-display-text(data.publication_sha256_spans) ·
        Identité de figure : #publication-display-text(data.figure_id_spans)
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
  let caption = if has-text(item.caption) { item.caption } else { item.title }
  let semantic-caption = if has-text(item.caption) {
    item.at("semantic_caption", default: none)
  } else {
    item.at("semantic_title", default: none)
  }
  let title = item.at("title", default: none)
  let status-label = item.at("status_label", default: none)
  let caption-content = [
    #if has-text(status-label) and has-text(title) [
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
  render-publication-image(item.media_path, caption-content, max-height-fraction: 0.35)
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
    #let sha256 = item.at("sha256", default: none)
    #let sha256-spans = item.at("sha256_spans", default: none)
    #if sha256 != none and has-text(sha256) [
      #render-figure-annotation[
        SHA-256 archivé : #if sha256-spans == none [#sha256] else [#publication-display-text(sha256-spans)]
      ]
    ]
    #let byte-size = item.at("byte_size", default: none)
    #if byte-size != none [
      #render-figure-annotation[Taille : #byte-size octets]
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

#let render-analyst-observation(item, unanchored: false) = block(
  width: 100%,
  fill: rgb("#eeeeee"),
  inset: 8pt,
  radius: 4pt,
  breakable: true,
)[
  #text(size: 11pt, weight: "extrabold", fill: rgb("#394b4b"))[
    #if unanchored [Avis de l'analyste — passage non ancré] else [Note de l'analyste]
  ]
  #parbreak()
  #text(item.text)
  #parbreak()
  #text(size: 8pt, fill: grey)[État : #item.state_label · relation : #item.relation_label]
  #if not unanchored [
    #parbreak()
    #text(size: 8pt, fill: grey)[Passage annoté : #item.anchor]
    #parbreak()
    #text(size: 7pt, fill: grey)[SHA-256 du passage : #publication-display-text(item.anchor_text_sha256)]
  ]
  #if item.finding_refs.len() > 0 [
    #parbreak()
    #text(size: 8pt, fill: grey)[Constats : ]
    #for (index, reference) in item.finding_refs.enumerate() [
      #if index > 0 [ · ]
      #link(reference.href)[#reference.label]
    ]
  ]
  #if item.evidence_refs.len() > 0 [
    #parbreak()
    #text(size: 8pt, fill: grey)[Évidences : ]
    #for (index, reference) in item.evidence_refs.enumerate() [
      #if index > 0 [ · ]
      #link(reference.href)[#reference.label]
    ]
  ]
]

#let render-ioc-item(item, reference-scope: "") = {
  list.item[
    #text(font: code-font-families, size: 8.5pt, hyphenate: false)[#publication-display-text(item.value)]
    #block(above: 1pt, below: 2pt)[
      #text(size: 8pt, fill: grey)[Source : ]
      #for (index, source-ref) in item.at("source_refs", default: ()).enumerate() [
        #if index > 0 [, ]
        #let source-number = source-ref.at("number", default: index + 1)
        #let target-id = source-ref.at("target_id", default: none)
        #let source-url = source-ref.at("url", default: none)
        #if target-id != none [
          #link(label(reference-scope + target-id))[#text(size: 8pt, fill: grey)[#str(source-number)]]
        ] else if source-url != none [
          #link(source-url)[#text(size: 8pt, fill: grey)[#str(source-number)]]
        ] else [
          #text(size: 8pt, fill: grey)[#str(source-number)]
        ]
      ]
      #text(size: 8pt, fill: grey)[ · #item.qualification]
    ]
    #for evidence in item.evidence [
      #block(above: 0pt, below: 1pt)[
        #text(size: 7pt, fill: grey)[
          #if evidence.at("source_number", default: none) != none [Source #evidence.source_number · ]
          #if evidence.at("context", default: "") != "" [#evidence.context · ]
          « #evidence.quote »
        ]
      ]
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

#let render-body-block(item) = {
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
    render-analyst-observation(item)
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
          render-body-block(item)
        }
      ]
    } else if content_section.type == "synthesis" [
      #section-title[SYNTHÈSE]
      #for item in content_section.at("blocks", default: ()) {
        render-body-block(item)
      }
    ] else if content_section.type == "chronology" [
      #section-title[CHRONOLOGIE]
      #if content_section.at("derived_timeline_figure", default: none) == none [
        #render-publication-timeline(
          content_section.timeline,
          ordered: true,
          reference-scope: reference-scope,
        )
      ]
      #if content_section.at("derived_timeline_figure", default: none) != none [
        #render-derived-timeline-figure(content_section.derived_timeline_figure)
      ]
    ] else if content_section.type == "limitations" [
      #section-title[LIMITES DES PREUVES]
      #for item in content_section.items [
        #item.text
        #if item.source_urls.len() > 0 [
          #parbreak()
          #text(size: 8pt, fill: grey)[Sources : ]
          #for (index, url) in item.source_urls.enumerate() [
            #if index > 0 [ · ]
            #link(url)[#url]
          ]
        ]
        #parbreak()
      ]
    ] else if content_section.type == "analyst_opinion" [
      #section-title[AVIS DE L'ANALYSTE]
      #let opinion-blocks = content_section.at("blocks", default: ())
      #if (
        opinion-blocks.len() == 0
        and not content_section.at("has_anchored_observations", default: false)
        and content_section.at("unanchored_observations", default: ()).len() == 0
      ) [
        #text(size: 9pt, fill: grey, style: "italic")[#content_section.at("empty_message", default: "Aucun avis publié.")]
      ]
      #for item in opinion-blocks [
        #heading(level: 3)[#item.number #item.heading]
        #text(size: 8pt, weight: "bold")[#item.qualification_label]
        #h(1em)
        #text(size: 8pt)[Confiance : #item.certainty_percent%]
        #parbreak()
        #item.text
        #parbreak()
        #if item.finding_refs.len() > 0 [
          #text(size: 8pt, fill: grey)[Findings : ]
          #for (index, reference) in item.finding_refs.enumerate() [
            #if index > 0 [ · ]
            #link(reference.href)[#reference.label]
          ]
          #parbreak()
        ]
        #if item.evidence_refs.len() > 0 [
          #text(size: 8pt, fill: grey)[Évidences : ]
          #for (index, reference) in item.evidence_refs.enumerate() [
            #if index > 0 [ · ]
            #link(reference.href)[#reference.label]
          ]
          #parbreak()
        ]
      ]
      #for figure in content_section.at("protocol_figures", default: ()) [
        #v(8pt)
        #render-diagram(figure)
      ]
      #if content_section.at("investigation_chain_figure", default: none) != none [
        #v(8pt)
        #render-investigation-chain-figure(content_section.investigation_chain_figure)
      ]
      #if content_section.at("infrastructure_graph_figure", default: none) != none [
        #v(8pt)
        #render-infrastructure-graph-figure(content_section.infrastructure_graph_figure)
      ]
      #let unresolved-points = content_section.at("unresolved_points", default: ())
      #if unresolved-points.len() > 0 [
        #heading(level: 3)[Points non résolus]
        #for point in unresolved-points [
          #list.item[#point]
        ]
      ]
      #for item in content_section.at("unanchored_observations", default: ()) [
        #v(6pt)
        #render-analyst-observation(item, unanchored: true)
      ]
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
      #text(size: 6.5pt, fill: grey)[ID candidat : #content_section.candidate_id · version : #content_section.version_id]
      #parbreak()
      #text(size: 6.5pt, fill: grey)[SHA-256 source : #publication-display-text(content_section.source_sha256)]
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
      #let test_report_observation_id = content_section.at(
        "test_report_observation_id",
        default: none,
      )
      #if content_section.retrohunt_submitted == false [
        #text(size: 7pt)[Aucun Retrohunt soumis.]
        #parbreak()
      ] else if content_section.retrohunt_submitted == true [
        #text(size: 7pt)[Retrohunt soumis.]
        #parbreak()
      ]
      #if test_report_observation_id != none [
        #text(size: 5.5pt, fill: grey)[Rapport canonique #test_report_observation_id · SHA-256 #publication-display-text(content_section.at("test_report_sha256", default: ""))]
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
        "IOC",
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
          "Pistes / IOC candidats non confirmés",
          "Ces éléments restent séparés des IOC retenus et des originaux dont l'association est étayée.",
          content_section.candidate_indicators,
          reference-scope: reference-scope,
        )
      ]
      #if content_section.omitted_invalid_url_count > 0 [
        #text(size: 8pt, fill: grey)[#content_section.omitted_invalid_url_count URL non valide(s) omise(s) des listes.]
      ]
    ] else if content_section.type == "technical_annex" [
      #let ioc_rows = content_section.at("ioc_rows", default: ())
      #let ioc_row_groups = content_section.at("ioc_row_groups", default: ())
      #let legacy_indicators = content_section.at("indicators", default: none)
      #let legacy_original_indicators = content_section.at("original_indicators", default: none)
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
    ] else {
      panic("unsupported publication content section type: " + content_section.type)
    }
  }
}

#let render-publication(publication) = {
  render-target(
    publication.at("article_title_target_id", default: none),
    heading(level: 1)[#publication.title],
  )
  render-publication-body(publication)
}
