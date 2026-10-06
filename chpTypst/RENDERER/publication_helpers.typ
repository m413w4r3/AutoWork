#import "../UTILS/helpers.typ": section-title, timeline, styled-table, ioc-list, semantic-text, semantic-or-plain
#import "../UTILS/colors.typ": grey

#let has-text(value) = value != none and value != ""

#let unique-urls(urls) = urls.fold((), (result, url) => {
  if url in result { result } else { result + (url,) }
})

#let publication-timeline-events(events) = {
  events.map(event => (
    event.display_date,
    event.text,
    unique-urls(event.source_urls),
    event.at("semantic_spans", default: none),
  ))
}

#let render-publication-timeline(events) = {
  timeline(publication-timeline-events(events))
}

#let render-table(item) = {
  if has-text(item.title) [
    #text(size: 12pt, weight: "bold")[#semantic-or-plain(item.title, item.at("semantic_title", default: none))]
    #v(4pt)
  ]

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
  styled-table(
    widths,
    cell-align: cell-alignments,
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
  text(size: 9pt, fill: grey)[Figure #number : #semantic-or-plain(caption, semantic-spans)],
)

// Measure against the active page's printable frame. Keep natural dimensions
// unless the image plus its caption and source notes would exceed that frame.
#let render-publication-image(media-path, caption, annotation: []) = layout(size => {
  let natural-image = image(media-path, width: size.width)
  let following-content = [
    #if caption != none [
      #v(4pt)
      #caption
    ]
    #annotation
  ]
  let following-height = measure(following-content, width: size.width).height
  let available-image-height = calc.max(1pt, size.height - following-height)
  let rendered-image = if measure(natural-image, width: size.width).height > available-image-height {
    image(media-path, width: size.width, height: available-image-height, fit: "contain")
  } else {
    natural-image
  }

  block(breakable: false, width: 100%)[
    #rendered-image
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
  render-publication-image(item.media_path, caption-content)
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
    #if has-text(item.provenance) [
      #block(above: 2pt, text(size: 8pt, fill: grey)[Provenance : #semantic-or-plain(item.provenance, item.at("semantic_provenance", default: none))])
    ]
    #if has-text(item.locator) [
      #block(above: 2pt, text(size: 8pt, fill: grey)[Repère : #item.locator])
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
  render-publication-image(item.media_path, caption-content)
}

#let render-body-block(item) = {
  if item.type == "paragraph" {
    [#semantic-or-plain(item.text, item.at("semantic_spans", default: none)) #parbreak()]
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
    ] else if content_section.type == "technical_annex" [
      #let indicators = content_section.indicators
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
    ] else {
      panic("unsupported publication content section type: " + content_section.type)
    }
  }
}

#let render-publication(publication) = {
  heading(level: 1)[#publication.title]
  render-publication-body(publication)
}
