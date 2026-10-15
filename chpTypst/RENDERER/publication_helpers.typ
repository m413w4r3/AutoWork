#import "../UTILS/helpers.typ": section-title, timeline, ioc-list, semantic-text, semantic-or-plain
#import "../UTILS/colors.typ": grey

#let has-text(value) = value != none and value != ""

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

#let publication-timeline-events(events) = {
  events.map(event => {
    let role-label = event.at("role_label", default: none)
    let body = [
      #if has-text(role-label) [
        #text(size: 8pt, fill: grey)[#role-label]
        #linebreak()
      ]
      #semantic-or-plain(event.text, event.at("semantic_spans", default: none))
    ]
    (
      event.display_date,
      body,
      unique-urls(event.source_urls),
      none,
    )
  })
}

#let render-publication-timeline(events) = {
  timeline(publication-timeline-events(events))
}

#let publication-styled-table(columns, cell-align: auto, title: none, ..content) = {
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
    inset: (x: 4pt, y: 3pt),
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
      breakable: false,
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

#let render-figure-caption(number, caption, semantic-spans) = block(
  above: 0pt,
  align(center)[
    #text(size: 9pt, fill: grey, style: "italic")[
      Figure #number : #semantic-or-plain(caption, semantic-spans)
    ]
  ],
)

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
  let caption-content = if has-text(caption) {
    render-figure-caption(item.figure_number, caption, semantic-caption)
  } else {
    none
  }
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
  let annotation = [
    #if has-text(item.source_note) [
      #block(above: 2pt, text(size: 8pt, fill: grey)[Source : #item.source_note])
    ]
  ]
  render-publication-image(item.media_path, caption-content, annotation: annotation)
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

#let render-body-block(item) = {
  let body = if item.type == "section_heading" {
    heading(level: 3)[#item.text]
  } else if item.type == "paragraph" {
    [#semantic-or-plain(item.text, item.at("semantic_spans", default: none)) #parbreak()]
  } else if item.type == "analyst_observation" {
    block(
      width: 100%,
      fill: rgb("#f3eff5"),
      inset: 8pt,
      radius: 4pt,
      breakable: true,
    )[
      #text(size: 9pt, weight: "bold", fill: rgb("#51305f"))[
        Note de l'analyste · #item.state_label · #item.relation_label
      ]
      #parbreak()
      #item.text
      #if item.finding_refs.len() > 0 [
        #parbreak()
        #text(size: 8pt, fill: grey)[Findings : ]
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

#let render-publication-body(publication) = {
  for content_section in publication.content_sections {
    if content_section.type == "references" {
      if content_section.timeline.len() > 0 or content_section.blocks.len() > 0 [
        #section-title[RÉFÉRENCES]
        #if content_section.timeline.len() > 0 [
          #render-publication-timeline(content_section.timeline)
        ]
        #for item in content_section.blocks {
          render-body-block(item)
        }
      ]
    } else if content_section.type == "synthesis" [
      #section-title[SYNTHÈSE]
      #for item in content_section.blocks {
        render-body-block(item)
      }
    ] else if content_section.type == "chronology" [
      #section-title[CHRONOLOGIE]
      #render-publication-timeline(content_section.timeline)
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
      #for item in content_section.blocks [
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
      #for figure in content_section.at("protocol_figures", default: ()) {
        render-diagram(figure)
      }
      #if content_section.unresolved_points.len() > 0 [
        #heading(level: 3)[Points non résolus]
        #for point in content_section.unresolved_points [
          #list.item[#point]
        ]
      ]
    ] else if content_section.type == "technical_annex" [
      #let indicators = content_section.indicators
      #let originals = content_section.original_indicators
      #if indicators.ips.len() > 0 or indicators.domains.len() > 0 or indicators.urls.len() > 0 or indicators.emails.len() > 0 or indicators.hashes.len() > 0 [
        #ioc-list(
          title: [IOC],
          ips: indicators.ips,
          domains: indicators.domains,
          urls: indicators.urls,
          emails: indicators.emails,
          hashes: indicators.hashes,
        )
      ]
      #if originals.ips.len() > 0 or originals.domains.len() > 0 or originals.urls.len() > 0 or originals.emails.len() > 0 or originals.hashes.len() > 0 [
        #v(8pt)
        #ioc-list(
          title: [IOC originaux à lien non démontré],
          note: content_section.original_indicator_note,
          ips: originals.ips,
          domains: originals.domains,
          urls: originals.urls,
          emails: originals.emails,
          hashes: originals.hashes,
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
