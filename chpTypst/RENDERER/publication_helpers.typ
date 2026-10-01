#import "../UTILS/helpers.typ": section-title, timeline, styled-table, ioc-list, source-list
#import "../UTILS/colors.typ": grey

#let has-text(value) = value != none and value != ""

#let unique-urls(urls) = urls.fold((), (result, url) => {
  if url in result { result } else { result + (url,) }
})

#let render-publication-timeline(events) = {
  section-title[Chronologie]
  let positional-events = events.map(event => (
    event.display_date,
    event.text,
    unique-urls(event.source_urls),
  ))
  timeline(positional-events)
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
  } else if item.type == "section_heading" {
    section-title(item.text)
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

#let render-publication(publication) = {
  heading(level: 1)[#publication.title]

  render-publication-timeline(publication.timeline)

  for item in publication.body_blocks {
    render-body-block(item)
  }

  ioc-list(
    title: [Indicateurs],
    ips: publication.indicators.ips,
    domains: publication.indicators.domains,
    urls: publication.indicators.urls,
    emails: publication.indicators.emails,
    hashes: publication.indicators.hashes,
  )

  if publication.uncertainties.len() > 0 [
    #section-title[Incertitudes et limites]
    #for uncertainty in publication.uncertainties {
      [#uncertainty #parbreak()]
    }
  ]

  if publication.sources.len() > 0 [
    #section-title[Sources]
    #source-list(publication.sources)
  ]
}
