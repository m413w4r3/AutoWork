#import "../UTILS/helpers.typ": section-title, timeline, styled-table, ioc-list, source-list, semantic-text, semantic-or-plain
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
    #text(size: 9pt, fill: grey)[#semantic-or-plain(item.caption, item.at("semantic_caption", default: none))]
  ]
}

#let render-diagram(item) = {
  if has-text(item.title) [
    #text(size: 12pt, weight: "bold")[#semantic-or-plain(item.title, item.at("semantic_title", default: none))]
    #v(4pt)
  ]
  image(item.media_path, width: 100%)
  v(4pt)
  if has-text(item.caption) [
    #text(size: 9pt, fill: grey)[#semantic-or-plain(item.caption, item.at("semantic_caption", default: none))]
  ]
}

#let render-figure(item) = {
  image(item.media_path, width: 90%)
  if has-text(item.caption) [
    #text(size: 9pt, fill: grey)[#semantic-or-plain(item.caption, item.at("semantic_caption", default: none))]
  ]
  if has-text(item.provenance) [
    #text(size: 8pt, fill: grey)[Provenance : #semantic-or-plain(item.provenance, item.at("semantic_provenance", default: none))]
  ]
  if has-text(item.locator) [
    #text(size: 8pt, fill: grey)[Repère : #item.locator]
  ]
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
  } else {
    panic("unsupported publication body block type: " + item.type)
  }
}

#let render-publication-body(publication) = {
  for content_section in publication.content_sections {
    if content_section.type == "references" [
      #section-title[RÉFÉRENCES]
      #if content_section.timeline.len() > 0 [
        #section-title[Chronologie]
        #render-publication-timeline(content_section.timeline)
      ]
      #for item in content_section.blocks {
        render-body-block(item)
      }
      #if content_section.sources.len() > 0 [
        #section-title[Sources complémentaires]
        #source-list(content_section.sources)
      ]
    ] else if content_section.type == "synthesis" [
      #section-title[SYNTHÈSE]
      #for item in content_section.blocks {
        render-body-block(item)
      }
    ] else if content_section.type == "technical_annex" [
      #section-title[ANNEXE TECHNIQUE — INDICATEURS]
      #ioc-list(
        title: [Indicateurs],
        ips: content_section.indicators.ips,
        domains: content_section.indicators.domains,
        urls: content_section.indicators.urls,
        emails: content_section.indicators.emails,
        hashes: content_section.indicators.hashes,
      )
    ] else {
      panic("unsupported publication content section type: " + content_section.type)
    }
  }
}

#let render-publication(publication) = {
  heading(level: 1)[#semantic-or-plain(publication.title, publication.at("title_spans", default: none))]
  render-publication-body(publication)
}
