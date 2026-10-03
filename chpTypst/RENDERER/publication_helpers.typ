#import "../UTILS/helpers.typ": section-title, timeline, styled-table, ioc-list, source-list
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
  ))
}

#let render-publication-timeline(events) = {
  timeline(publication-timeline-events(events))
}

#let render-table(item) = {
  if has-text(item.title) [
    #text(size: 12pt, weight: "bold")[#item.title]
    #v(4pt)
  ]

  let widths = item.columns.map(column => 1fr)
  let cell-alignments = item.columns.map(column => left)
  let header-cells = item.columns.map(cell => [#cell])
  let body-cells = item.rows.flatten().map(cell => [#cell])
  styled-table(
    widths,
    cell-align: cell-alignments,
    ..(header-cells + body-cells),
  )

  if has-text(item.caption) [
    #text(size: 9pt, fill: grey)[#item.caption]
  ]
}

#let render-diagram(item) = {
  if has-text(item.title) [
    #text(size: 12pt, weight: "bold")[#item.title]
    #v(4pt)
  ]
  image(item.media_path, width: 90%)
  if has-text(item.caption) [
    #text(size: 9pt, fill: grey)[#item.caption]
  ]
}

#let render-figure(item) = {
  image(item.media_path, width: 90%)
  if has-text(item.caption) [
    #text(size: 9pt, fill: grey)[#item.caption]
  ]
  if has-text(item.provenance) [
    #text(size: 8pt, fill: grey)[Provenance : #item.provenance]
  ]
  if has-text(item.locator) [
    #text(size: 8pt, fill: grey)[Repère : #item.locator]
  ]
}

#let render-body-block(item) = {
  if item.type == "paragraph" {
    [#item.text #parbreak()]
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
  heading(level: 1)[#publication.title]
  render-publication-body(publication)
}
