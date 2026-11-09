#import "../UTILS/helpers.typ": semantic-text, semantic-or-plain, code-font-families, noteAnalyste, tag
#import "../UTILS/colors.typ": accent, grey, purple, purple-light

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

#let render-section-heading(title, level: 1) = heading(level: level)[#title]

#let render-article-section-heading(title, reduce-above: 0pt) = {
  if reduce-above > 0pt {
    v(-reduce-above)
  }
  heading(level: 1)[#title]
}

#let render-article-overview(overview) = {
  let fields = (
    ("Victimologie", overview.at("victimology", default: none)),
    ("Arsenal", overview.at("arsenal", default: none)),
    ("Objectif principal", overview.at("main_objective", default: none)),
  )
  let visible-fields = fields.filter(entry => entry.at(1) != none)
  if visible-fields.len() > 0 [
    #block(
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
      #for (index, (label, field)) in visible-fields.enumerate() [
        #let value = if type(field) == str { field } else { field.at("text", default: "") }
        #block(below: if index < visible-fields.len() - 1 { 6pt } else { 0pt })[
          #grid(
            columns: (10pt, 1fr),
            align: (left, left),
            [#text(size: 10pt)[•]],
            [
              #text(size: 10pt, weight: "bold", fill: purple)[#label : ]
              #text(size: 10pt)[#value]
            ],
          )
        ]
      ]
    ]
  ]
}

#let render-article-timeline(events) = {
  if events.len() >= 2 [
    #render-section-heading[Chronologie]
    #block(
      width: 100%,
      inset: (left: 0pt, right: 0pt, top: 3pt, bottom: 3pt),
      stroke: (left: 1pt + accent),
    )[
      #for (index, event) in events.enumerate() [
        #block(breakable: false)[
          #grid(
            columns: (1pt, 1fr),
            column-gutter: 8pt,
            align: (top, left),
            [#circle(radius: 3pt, fill: accent)],
            [
              #text(size: 9pt, weight: "bold", fill: accent)[#event.display_date_fr]
              #linebreak()
              #text(size: 9.5pt)[#text(event.text)]
            ],
          )
        ]
        #if index < events.len() - 1 [#v(8pt)]
      ]
    ]
  ]
}

#let render-article-ioc-category(label, values) = {
  if values.len() > 0 [
    #if values.len() <= 18 [
      #block(above: 16pt, breakable: false)[
        #text(size: 10pt, weight: "bold", fill: purple)[#(label + " :")]
        #set par(justify: false, leading: 1.2em, spacing: 0pt)
        #for (index, item) in values.enumerate() [
          #if index > 0 [#v(7pt)]
          #block(breakable: false)[
            #text(font: code-font-families, size: 8.5pt, hyphenate: false)[
              #publication-display-text(item.value)
            ]
          ]
        ]
      ]
    ] else [
      #block(above: 16pt, sticky: true)[
        #text(size: 10pt, weight: "bold", fill: purple)[#(label + " :")]
      ]
      #set par(justify: false, leading: 1.2em, spacing: 0pt)
      #for (index, item) in values.enumerate() [
        #if index > 0 [#v(7pt)]
        #block(breakable: false)[
          #text(font: code-font-families, size: 8.5pt, hyphenate: false)[
            #publication-display-text(item.value)
          ]
        ]
      ]
    ]
  ]
}

#let render-article-iocs(article-format) = {
  let categories = article-format.at("ioc_categories", default: ())
  let ips = categories.at("ips", default: ())
  let urls = categories.at("urls", default: ())
  let domains = categories.at("domains", default: ())
  let files = categories.at("files", default: ())
  if ips.len() + urls.len() + domains.len() + files.len() > 0 [
    #block(sticky: true)[
      #render-section-heading[IOC]
      #render-article-ioc-category("Adresses IP", ips)
      #render-article-ioc-category("URL", urls)
      #render-article-ioc-category("Noms de domaine", domains)
      #render-article-ioc-category("Fichiers", files)
    ]
  ]
}

#let render-legacy-iocs(content-sections) = {
  let ioc-sections = content-sections.filter(section => section.type == "ioc")
  let has-values = ioc-sections.any(section => {
    section.at("source_indicators", default: ()).any(
      group => group.at("items", default: ()).len() > 0,
    )
  })
  if has-values [
    #render-section-heading[IOC]
    #for section in ioc-sections {
      for group in section.at("source_indicators", default: ()) {
        let items = group.at("items", default: ())
        if items.len() > 0 [
          #block(above: 5pt, below: 3pt, sticky: true)[
            #text(size: 10pt, weight: "bold", fill: purple)[#(group.heading + " :")]
          ]
          #set text(hyphenate: false)
          #for item in items [
            #block(above: 0pt, below: 2pt)[
              #text(size: 8.75pt)[#publication-display-text(item.value)]
            ]
          ]
        ]
      }
    }
  ]
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

#let render-publication-timeline(
  events,
  ordered: false,
  reference-scope: "",
  date-fill: none,
  footnotes: false,
) = {
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
        #if date-fill == none [
          #text(weight: "bold")[#(display-date + " : ")]
        ] else [
          #text(weight: "bold", fill: date-fill)[#(display-date + " : ")]
        ]
      ]
      #semantic-or-plain(event.text, event.at("semantic_spans", default: none))
      #if footnotes [
        #for (index, source-url) in source-urls.enumerate() [
          #if index > 0 [, ]
          #footnote[
            #text(size: 7pt, fill: grey)[#link(source-url)[#source-url]]
          ]
        ]
      ] else if source-refs.len() > 0 [
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
  if not footnotes and not ordered and all-source-urls.len() > 0 and not uses-internal-refs {
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

#let render-publication-reference(entry, numbered: false, reference-scope: "") = {
  if numbered {
    let number = entry.at("number", default: 1)
    let display-date = entry.at("display_date", default: "Date de publication non précisée")
    let source-urls = entry.at("source_urls", default: ())
    let target-id = entry.at("target_id", default: "publication-source-legacy-" + str(number))
    let entry-text = entry.at("text", default: "")
    block(above: 4pt, below: 5pt)[
      #set par(justify: false)
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
  } else {
  let publisher = entry.at("publisher", default: "")
  let title = entry.at("title", default: "")
  let display-date = entry.at("display_date", default: entry.at("date", default: ""))
  let url = entry.at("url", default: none)
  let source-urls = entry.at("source_urls", default: ())
  let entry-text = entry.at("text", default: "")
  let publisher = if has-text(publisher) { str(publisher) } else { "" }
  let title = if has-text(title) { str(title) } else { "" }
  let display-date = if has-text(display-date) { str(display-date) } else { "" }
  let entry-text = if has-text(entry-text) { entry-text } else { "" }
  let primary-url = if url != none { url } else if source-urls.len() > 0 { source-urls.first() } else { none }
  let next-urls = source-urls.filter(candidate => candidate != primary-url)
  let has-label = title != "" or entry-text != ""
  let reference-content = [
    #set par(justify: false)
    #if publisher != "" [#text(publisher)]
    #if has-label [
      #if publisher != "" [ — ]
      #if title != "" [#text(title)] else [
        #semantic-or-plain(entry-text, entry.at("semantic_spans", default: none))
      ]
    ]
    #if display-date != "" [
      #if publisher != "" or has-label [ — ]
      #text(display-date)
    ]
    #if primary-url != none [
      #if publisher != "" or has-label or display-date != "" [ — ]
      #link(primary-url)[#text(primary-url)]
    ]
    #for next-url in next-urls [
      #text(" — ")
      #link(next-url)[#text(next-url)]
    ]
  ]
  list.item(reference-content)
  }
}

#let publication-styled-table(
  columns,
  cell-align: auto,
  title: none,
  breakable: false,
  text-size: 8pt,
  footer-note: none,
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
    [#block(breakable: false, above: 0pt, below: 0pt)[#cell]]
  })
  let footer-cells = if footer-note != none and footer-note != "" {
    (
      table.cell(colspan: column-count)[
        #block(breakable: false)[
          #set par(justify: false)
          #text(size: text-size, fill: grey, style: "italic")[#footer-note]
        ]
      ],
    )
  } else { () }
  let body-row-count = calc.floor((cells.len() - column-count) / column-count)

  set text(size: text-size)
  let rendered-table = align(center, table(
    columns: columns,
    inset: inset,
    align: (x, y) => {
      if y == 0 { left } else { cell-align.at(x) }
    },
    fill: (_, y) => if y == 0 { rgb("#5D7374") } else { none },
    stroke: 0.4pt + rgb("#AEB7C0"),
    table.header(repeat: true, ..header-cells),
    ..(body-cells + footer-cells),
  ))

  if body-row-count <= 12 {
    block(
      breakable: breakable,
      above: 10pt,
      below: 8pt,
    )[
      #if title != none [
        #title
        #v(4pt)
      ]
      #rendered-table
    ]
  } else {
    block(above: 10pt, below: 8pt)[
      #if title != none [
        #block(sticky: true)[
          #title
          #v(4pt)
        ]
      ]
      #rendered-table
    ]
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

#let render-opinion-pipe-table(pipe-table, footer-note: none) = {
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
    text-size: 9pt,
    footer-note: footer-note,
    inset: (x: 4pt, y: 4pt),
    ..(header + rows),
  )
}

#let render-opinion-text(spans, fallback, pipe-table: none) = {
  if pipe-table != none {
    let prefix = pipe-table.at("prefix", default: "")
    let suffix = pipe-table.at("suffix", default: "")
    if pipe-table.wide [
      #set page(flipped: true)
      #if prefix != "" [
        #block(sticky: true, below: 4pt)[
          #set par(justify: false)
          #text(prefix)
        ]
      ]
      #render-opinion-pipe-table(pipe-table, footer-note: suffix)
      #set page(flipped: false)
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

#let render-table(item, article-style: false) = {
  let title = if has-text(item.title) {
    [#text(size: 12pt, weight: "bold")[#semantic-or-plain(item.title, item.at("semantic_title", default: none))]]
  } else {
    none
  }

  let semantic-columns = item.at("semantic_columns", default: none)
  let semantic-cells = item.at("semantic_cells", default: none)
  let card-display-cells = item.at("card_display_cells", default: ())
  if item.columns.len() > 2 {
    block(above: 10pt, below: 8pt)[
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

  v(if article-style { 6pt } else { 4pt })
  if has-text(item.caption) [
    #block(
      above: if article-style { 0pt } else { 2pt },
      below: if article-style { 12pt } else { 4pt },
    )[
      #text(size: 9pt, fill: grey)[#semantic-or-plain(item.caption, item.at("semantic_caption", default: none))]
    ]
  ]
}

#let render-opinion-reference(
  target-id,
  display,
  reference-scope: "",
  fallback-url: none,
) = context {
  let target = label(reference-scope + target-id)
  if query(target).len() > 0 [
    #link(target)[#display]
  ] else if fallback-url != none [
    #link(fallback-url)[#display]
  ] else [
    #display
  ]
}

#let render-opinion-references(references, reference-scope: "") = {
  for (index, reference) in references.enumerate() [
    #if index > 0 [ · ]
    #render-opinion-reference(
      reference.target_id,
      [#reference.label],
      reference-scope: reference-scope,
      fallback-url: reference.at("href", default: none),
    )
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

#let render-opinion-ioc-item(item, reference-scope: "") = {
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

#let render-ioc-item(item, reference-scope: "") = render-opinion-ioc-item(
  item,
  reference-scope: reference-scope,
)

#let render-opinion-ioc-groups(
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
        render-opinion-ioc-item(item, reference-scope: reference-scope)
      }
    }
  }
}

#let render-opinion-ioc-section(title, description, groups, reference-scope: "") = {
  if groups.len() == 0 {
    block(breakable: false)[
      #render-section-heading[#title]
      #if description != none [#text(size: 8pt, fill: grey)[#description] #linebreak()]
    ]
  } else {
    let first-group = groups.first()
    let first-item = first-group.items.first()
    block(breakable: false)[
      #render-section-heading[#title]
      #if description != none [#text(size: 8pt, fill: grey)[#description]]
      #v(6pt)
      #block(above: 4pt, below: 2pt)[#text(size: 10pt, weight: "bold")[#first-group.heading]]
      #render-opinion-ioc-item(first-item, reference-scope: reference-scope)
    ]
    render-opinion-ioc-groups(
      groups,
      skip-first-heading: true,
      skip-first-item: true,
      reference-scope: reference-scope,
    )
  }
}

#let render-opinion-iocs(section, reference-scope: "") = {
  v(16pt)
  render-opinion-ioc-section(
    "IOC de l'article",
    none,
    section.at("source_indicators", default: ()),
    reference-scope: reference-scope,
  )
  let original = section.at("original_indicators", default: ())
  if original.len() > 0 [
    #v(8pt)
    #render-opinion-ioc-section(
      "IOC originaux",
      "Découverts dans les sources de ce run et associés au sujet par l'extrait cité.",
      original,
      reference-scope: reference-scope,
    )
  ]
  let candidates = section.at("candidate_indicators", default: ())
  let omitted-url-count = section.at("omitted_invalid_url_count", default: 0)
  if candidates.len() > 0 [
    #v(8pt)
    #let description = if omitted-url-count > 0 {
      "Éléments à examiner, sans promotion au statut d'IOC confirmé. "
        + str(omitted-url-count)
        + " URL non valide(s) omise(s) des listes."
    } else {
      "Éléments à examiner, sans promotion au statut d'IOC confirmé."
    }
    #render-opinion-ioc-section(
      "Pistes non confirmées",
      description,
      candidates,
      reference-scope: reference-scope,
    )
  ]
}

#let render-opinion-ioc-table(groups, reference-scope: "") = {
  for group in groups {
    let hash-only = group.rows.len() > 0 and group.rows.all(row => row.type == "hash")
    let header-cells = if hash-only {
      ([Empreinte de fichier — une valeur par ligne], [Statut, relation et provenance])
    } else {
      ([Type], [Valeur défanguée], [Statut indicateur], [Statut relation], [Contexte et provenance])
    }
    let body-cells = if hash-only {
      group.rows.map(row => (
        [#text(font: code-font-families, size: 9pt, hyphenate: false)[#publication-display-text(row.value)]],
        [
          #text(size: 8.5pt, weight: "bold")[Ind. #row.at("indicator_status_short_label", default: row.at("indicator_status_label", default: "")) · Rel. #row.at("relationship_status_short_label", default: row.at("relationship_status_label", default: row.at("status_label", default: "")))]
          #let context-summary = row.at("hash_context_summary", default: row.at("context_summary", default: ""))
          #let source-refs = row.at("source_refs", default: ())
          #if context-summary != "" [#linebreak() #text(size: 8.5pt, fill: grey)[#context-summary]]
          #if source-refs.len() > 0 [#if context-summary != "" [ · ] #text(size: 8.5pt, fill: grey)[src #render-source-references(source-refs, reference-scope: reference-scope)]]
          #let draft-refs = row.at("draft_refs", default: ())
          #if draft-refs.len() > 0 [#if context-summary != "" or source-refs.len() > 0 [ · ] #render-opinion-references(draft-refs, reference-scope: reference-scope)]
        ],
      )).flatten()
    } else {
      group.rows.map(row => (
        [#text(size: 9pt, weight: "bold")[#row.type_label]],
        [
          #text(font: code-font-families, size: 9pt, hyphenate: false)[
            #if type(row.value) == str {
              semantic-or-plain(row.value, row.at("value_spans", default: none))
            } else {
              publication-display-text(row.value)
            }
          ]
        ],
        [#text(size: 8.5pt, weight: "bold")[#row.at("indicator_status_label", default: "")]],
        [#text(size: 8.5pt, weight: "bold")[#row.at("relationship_status_label", default: row.at("status_label", default: ""))]],
        [
          #let context-summary = row.at("context_summary", default: "")
          #if context-summary != "" [#text(size: 8.5pt, fill: grey)[#context-summary]]
          #let source-refs = row.at("source_refs", default: ())
          #if source-refs.len() > 0 [#if context-summary != "" [#linebreak()] #text(size: 8.5pt, fill: grey)[src #render-source-references(source-refs, reference-scope: reference-scope)]]
          #let draft-refs = row.at("draft_refs", default: ())
          #if draft-refs.len() > 0 [#if context-summary != "" or source-refs.len() > 0 [#linebreak()] #render-opinion-references(draft-refs, reference-scope: reference-scope)]
        ],
      )).flatten()
    }
    block(sticky: true, above: 3pt, below: 1pt)[#text(size: 9pt, weight: "bold")[#group.heading]]
    publication-styled-table(
      if hash-only { (0.55, 0.45).map(weight => weight * 1fr) } else { (0.65, 1.45, 1.15, 1.15, 2.35).map(weight => weight * 1fr) },
      cell-align: if hash-only { (left, left) } else { (left, left, left, left, left) },
      breakable: true,
      text-size: 9pt,
      inset: (x: 4pt, y: 2.5pt),
      ..(header-cells + body-cells),
    )
  }
}

#let render-opinion-technical-annex(section, reference-scope: "") = {
  let ioc-rows = section.at("ioc_rows", default: ())
  let ioc-row-groups = section.at("ioc_row_groups", default: ())
  let identity-provenance = section.at("publication_identity_provenance", default: none)
  let implant-rows = section.at("implant_identifier_rows", default: ())
  let opinion-refs = section.at("opinion_refs", default: ())
  let figure-provenance = section.at("protocol_figure_provenance", default: ())
  if ioc-rows.len() > 0 or has-text(identity-provenance) or implant-rows.len() > 0 or opinion-refs.len() > 0 or figure-provenance.len() > 0 [
    #set page(flipped: true)
    #if has-text(identity-provenance) [
      #render-section-heading[PROVENANCE DE PUBLICATION]
      #text(size: 7.5pt, fill: grey)[Identité de base : #text(identity-provenance)]
      #v(5pt)
    ]
    #if ioc-rows.len() > 0 [
      #v(16pt)
      #render-section-heading[ANNEXE TECHNIQUE — inventaire exhaustif des IOC]
      #render-opinion-ioc-table(ioc-row-groups, reference-scope: reference-scope)
    ]
    #if implant-rows.len() > 0 [
      #v(10pt)
      #render-section-heading[IDENTIFIANT C2 DE L’IMPLANT — IOC]
      #for item in implant-rows [#text(size: 8pt)[#item.type_label · #item.value]]
    ]
    #if opinion-refs.len() > 0 [
      #v(10pt)
      #render-section-heading[ANNEXE DE TRAÇABILITÉ]
      #text(size: 8pt, fill: grey)[Les renvois courts du corps désignent les identifiants canoniques suivants.]
      #let finding-refs = opinion-refs.filter(item => item.kind_label == "Finding")
      #let evidence-refs = opinion-refs.filter(item => item.kind_label == "Évidence")
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
                #link(item.href)[#text(font: code-font-families, size: 7.5pt, hyphenate: false)[#publication-display-text(item.reference_id_spans)]]
              ]
            ]),
          )
        ]
      ]
    ]
    #if figure-provenance.len() > 0 [
      #v(10pt)
      #render-section-heading[PROVENANCE DES FIGURES D'AVIS]
      #for figure in figure-provenance [
        #block(above: 3pt, below: 5pt, breakable: true)[
          #text(size: 8pt, weight: "bold")[#figure.kind_label]
          #linebreak()
          #text(size: 7pt)[Identifiant d'actif : #figure.asset_id]
          #for field in ("model_sha256", "spec_sha256", "asset_sha256", "preview_sha256", "pin_sha256") [
            #if figure.at(field, default: none) != none [#linebreak() #text(size: 7pt)[#field : #figure.at(field)]]
          ]
        ]
      ]
    ]
    #set page(flipped: false)
  ]
}

#let render-figure-caption(
  number,
  caption,
  semantic-spans,
  top-space: 0pt,
  bottom-space: 0pt,
  below-space: 4pt,
) = block(
  above: 0pt,
  below: below-space,
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
    calc.min(size.width / natural-size.width, max-image-height / natural-size.height),
  )
  let rendered-image = if scale != 1 {
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

// Fit vector art to the frame. Raster art can enlarge by at most 2.5x because
// Typst exposes DPI-derived physical size but not source pixel dimensions. Do
// not enlarge raster assets with an intrinsic width under 100pt, which keeps
// small icons and logos from becoming large figures.
#let render-publication-image(
  media-path,
  caption,
  annotation: [],
  max-height-fraction: 0.82,
  article-style: false,
) = block(
  breakable: false,
  width: 100%,
  above: 10pt,
  below: if article-style { 12pt } else { 8pt },
)[
  #layout(size => {
    let natural-image = image(media-path)
    let natural-size = measure(natural-image)
    let following-content = [
      #if caption != none [
        #v(if article-style { 6pt } else { 4pt })
        #caption
      ]
      #annotation
    ]
    let max-image-height = calc.max(1pt, size.height * max-height-fraction)
    let frame-scale = calc.min(
      size.width / natural-size.width,
      max-image-height / natural-size.height,
    )
    let scale = if media-path.ends-with(".svg") or frame-scale <= 1 {
      frame-scale
    } else {
      let max-upscale = if natural-size.width < 100pt {
        1
      } else {
        2.5
      }
      calc.min(frame-scale, max-upscale)
    }
    let rendered-image = if scale != 1 {
      image(media-path, width: natural-size.width * scale)
    } else {
      natural-image
    }

    block(breakable: false, width: 100%)[
      #align(center)[#rendered-image]
      #following-content
    ]
  })
]

#let render-diagram(item, article-style: false) = {
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
      #block(above: 2pt, below: if article-style { 6pt } else { 2pt})[
        #text(size: 9pt, weight: "bold")[#title]
      ]
    ]
    #if has-text(status-label) [
      #block(above: 2pt, below: 2pt)[
        #text(size: 8pt, weight: "bold", fill: rgb("#315D7C"))[#status-label]
      ]
    ]
    #if has-text(caption) [
      #render-figure-caption(
        item.figure_number,
        caption,
        semantic-caption,
        bottom-space: if article-style { 6pt } else { 0pt },
        below-space: if article-style { 0pt } else { 4pt },
      )
    ]
    #if has-text(item.at("status_legend", default: none)) [
      #block(above: 3pt, below: 2pt)[
        #text(size: 6.5pt, fill: grey)[#item.status_legend]
      ]
    ]
  ]
  if has-text(item.at("status_legend", default: none)) {
    render-publication-image(
      item.media_path,
      caption-content,
      max-height-fraction: 0.52,
      article-style: article-style,
    )
  } else {
    render-publication-image(
      item.media_path,
      caption-content,
      max-height-fraction: 0.35,
      article-style: article-style,
    )
  }
  }
}

#let render-figure(item, article-style: false) = {
  let caption-content = if has-text(item.caption) {
    render-figure-caption(
      item.figure_number,
      item.caption,
      item.at("semantic_caption", default: none),
      bottom-space: if article-style { 6pt } else { 0pt },
      below-space: if article-style { 0pt } else { 4pt },
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
    max-height-fraction: 0.36,
    article-style: article-style,
  )
}

#let render-chart(item, article-style: false) = {
  let caption = if has-text(item.caption) { item.caption } else { item.title }
  let semantic-caption = if has-text(item.caption) {
    item.at("semantic_caption", default: none)
  } else {
    item.at("semantic_title", default: none)
  }
  let caption-content = if has-text(caption) {
    render-figure-caption(
      item.figure_number,
      caption,
      semantic-caption,
      bottom-space: if article-style { 6pt } else { 0pt },
      below-space: if article-style { 0pt } else { 4pt },
    )
  } else {
    none
  }
  render-publication-image(
    item.media_path,
    caption-content,
    max-height-fraction: 0.55,
    article-style: article-style,
  )
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

#let render-body-block(
  item,
  reference-scope: "",
  paragraph-gap: 5pt,
  article-style: false,
) = {
  let body = if item.type == "section_heading" {
    heading(level: 2)[#item.text]
  } else if item.type == "paragraph" {
    [
      #block(above: 0pt, below: paragraph-gap)[
        #semantic-or-plain(item.text, item.at("semantic_spans", default: none))
        #if item.at("anchor_target", default: none) != none [#label(item.anchor_target)]
      ]
      #parbreak()
    ]
  } else if item.type == "analyst_observation" {
    block(above: 10pt, below: 8pt)[
      #render-analyst-observation(item, reference-scope: reference-scope)
    ]
  } else if item.type == "table" {
    render-table(item, article-style: article-style)
  } else if item.type == "diagram" {
    render-diagram(item, article-style: article-style)
  } else if item.type == "figure" {
    render-figure(item, article-style: article-style)
  } else if item.type == "chart" {
    render-chart(item, article-style: article-style)
  } else {
    panic("unsupported publication body block type: " + item.type)
  }
  render-target(item.at("target_id", default: none), body)
}

#let render-publication-references(
  content-section,
  brief-publication: false,
  opinion-publication: false,
  reference-scope: "",
) = {
  let sources = content-section.at("sources", default: ())
  let entries = content-section.at("entries", default: ())
  let has-structured-entries = entries.any(entry =>
    has-text(entry.at("title", default: none))
    or has-text(entry.at("publisher", default: none))
  )
  let entries = if opinion-publication or has-structured-entries {
    entries
  } else if sources.len() > 0 {
    sources
  } else {
    entries
  }
  let blocks = content-section.at("blocks", default: ())
  if brief-publication and not opinion-publication {
    for item in blocks {
      render-body-block(item, reference-scope: reference-scope)
    }
  } else if entries.len() > 0 or blocks.len() > 0 [
    #if content-section.at("show_heading", default: true) [
      #render-section-heading[Références]
    ]
    #set list(spacing: 4pt, indent: 12pt)
    #for entry in entries [
      #render-publication-reference(
        entry,
        numbered: opinion-publication,
        reference-scope: reference-scope,
      )
    ]
    #for item in blocks {
      render-body-block(item, reference-scope: reference-scope)
    }
  ]
}

#let render-publication-body(publication, reference-scope: "") = {
  let brief-publication = publication.at("kind", default: "article") == "brief"
  let opinion-publication = publication.at("opinion_document", default: none)
  let opinion-publication = if opinion-publication == none {
    publication.content_sections.any(section => section.type == "analyst_opinion")
  } else {
    opinion-publication
  }
  let article-format = publication.at("article_format", default: none)
  let article-publication = not brief-publication and not opinion-publication
  let chronology-sections = publication.content_sections.filter(
    section => section.type == "chronology",
  )
  let article-events = if article-format != none {
    article-format.at("timeline_key_events", default: ())
  } else if chronology-sections.len() > 0 {
    chronology-sections.first().at("timeline", default: ()).map(event => (
      display_date_fr: event.at("display_date", default: ""),
      text: event.text,
    ))
  } else {
    ()
  }
  let article-overview = if article-format == none {
    none
  } else {
    article-format.at("overview", default: none)
  }
  let has-article-overview = article-overview != none
  if article-publication [
    #render-article-timeline(article-events)
    #if article-overview != none [#render-article-overview(article-overview)]
  ]
  for content_section in publication.content_sections {
    if content_section.type == "references" {
      if not article-publication {
        render-publication-references(
          content_section,
          brief-publication: brief-publication,
          opinion-publication: opinion-publication,
          reference-scope: reference-scope,
        )
      }
    } else if content_section.type == "synthesis" [
      #if brief-publication [
        #render-section-heading[Synthèse]
        #for item in content_section.at("blocks", default: ()) {
          render-body-block(
            item,
            reference-scope: reference-scope,
            paragraph-gap: 9pt,
          )
        }
      ] else if article-publication [
        #render-article-section-heading(
          "Synthèse",
          reduce-above: if has-article-overview { 13pt } else { 0pt },
        )
        #for item in content_section.at("blocks", default: ()) {
          render-body-block(
            item,
            reference-scope: reference-scope,
            paragraph-gap: 7pt,
            article-style: true,
          )
        }
      ] else [
        #render-section-heading[Synthèse]
        #for item in content_section.at("blocks", default: ()) {
          render-body-block(item, reference-scope: reference-scope)
        }
      ]
    ] else if content_section.type == "chronology" [
      #let brief-chronology = publication.at("kind", default: "article") == "brief"
      #if brief-chronology [
        #render-section-heading[Chronologie]
        #render-publication-timeline(
          content_section.timeline,
          reference-scope: reference-scope,
          date-fill: purple,
          footnotes: brief-chronology,
        )
      ] else if opinion-publication [
        #render-section-heading[Chronologie]
        #render-publication-timeline(
          content_section.timeline,
          ordered: true,
          reference-scope: reference-scope,
          date-fill: purple,
        )
      ]
    ] else if content_section.type == "limitations" [
      #render-section-heading[Limites des preuves]
      #for item in content_section.items [
        #render-opinion-text(item.at("text_spans", default: none), item.text)
        #let source-refs = item.at("source_refs", default: ())
        #if source-refs.len() > 0 [
          #parbreak()
          #text(size: 7.5pt, fill: grey)[Sources : ]
          #for (index, source-ref) in source-refs.enumerate() [
            #if index > 0 [ · ]
            #super[#link(source-ref.url)[#str(source-ref.number)]]
          ]
        ]
        #parbreak()
      ]
      #let source-list = content_section.at("source_list", default: ())
      #if source-list.len() > 0 [
        #v(3pt)
        #text(size: 8pt, weight: "bold")[Sources]
        #set list(spacing: 3pt, indent: 12pt)
        #for source in source-list [
          #list.item[#link(source.url)[#text(size: 7pt, fill: grey)[#source.url]]]
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
      #render-section-heading[Avis de l'analyste]
      #for item in opinion-blocks [
        #heading(level: 3)[#item.number #text(item.heading)]
        #text(size: 8pt, weight: "bold")[#text(item.qualification_label)]
        #parbreak()
        #render-opinion-text(
          item.at("text_spans", default: none),
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
      #render-section-heading[#text(content_section.heading)]
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
          #let sample-sha256 = item.at("sample_sha256", default: none)
          #if sample-sha256 != none [
            #linebreak()
            #text(size: 7pt)[SHA-256 de l’échantillon : ]
            #text(font: code-font-families, size: 7pt, hyphenate: false)[#text(sample-sha256)]
          ]
          #linebreak()
          #text(size: 7pt)[Observation de preuve : ]
          #render-opinion-reference(
            item.evidence_ref.target_id,
            [#text(item.evidence_ref.label)],
            reference-scope: reference-scope,
            fallback-url: item.evidence_ref.at("href", default: none),
          )
        ]
      ]
      #text(size: 7pt, fill: grey)[#text(content_section.attacker_identifier_note)]
    ] else if content_section.type == "detection" [
      #render-section-heading[Détection]
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
      #if opinion-publication [
        #render-opinion-iocs(content_section, reference-scope: reference-scope)
      ]
    ] else if content_section.type == "technical_annex" [
      #if opinion-publication [
        #render-opinion-technical-annex(content_section, reference-scope: reference-scope)
      ]
    ] else if content_section.type == "provenance" or content_section.type == "publication_provenance" [
      #if opinion-publication [
        #render-section-heading[PROVENANCE DE PUBLICATION]
        #text(size: 7.5pt, fill: grey)[#publication-display-text(content_section.at("text", default: ""))]
      ]
    ] else {
      panic("unsupported publication content section type: " + content_section.type)
    }
  }
  if article-publication {
    for content_section in publication.content_sections {
      if content_section.type == "references" {
        render-publication-references(
          content_section,
          reference-scope: reference-scope,
        )
      }
    }
    if article-format != none {
      render-article-iocs(article-format)
    } else {
      render-legacy-iocs(publication.content_sections)
    }
  } else if brief-publication {
    render-legacy-iocs(publication.content_sections)
  }
}

#let render-publication(publication) = {
  let document-label = publication.at("document_label", default: none)
  if not has-text(document-label) {
    panic("publication render-data requires document_label")
  }
  [
    #render-target(
      publication.at("article_title_target_id", default: none),
      if publication.at("kind", default: "article") == "brief" [
        #heading(level: 1)[#publication.title]
      ] else [
        #text(size: 16pt, weight: "regular", fill: purple)[#publication.title]
      ],
    )
    #if publication.at("kind", default: "article") != "brief" [
      #grid(
        columns: (1fr, auto),
        align: (left, right),
        [#tag("Article")],
        [
          #let number = publication.at("number", default: none)
          #if number != none [#text(size: 11pt, weight: "bold", fill: purple)[#number]]
        ],
      )
      #v(10pt)
    ]
    #render-publication-body(publication)
  ]
}
