#import "colors.typ": purple
#import "header_footer.typ": report-header, report-footer

#let apply-document-style(body) = {
  set text(
    font: "Hanken Grotesk",
    size: 11pt,
    lang: "fr",
  )

  set page(
    paper: "a4",
    margin: (
      top: 2.3cm,
      bottom: 2.3cm,
      left: 2.2cm,
      right: 2.2cm,
    ),
    header: report-header,
    footer: report-footer,
  )

  // Inline and block code styling.
  show raw.where(block: false): highlight.with(fill: luma(190), radius: 2pt)
  show raw.where(block: false): set text(size: 10pt)
  show raw.where(block: true): it => pad(x: 2%, block(
    width: 100%,
    fill: black,
    inset: (x: 10pt, y: 1pt),
    outset: (y: 5pt),
    radius: 2pt,
  )[
    #set text(fill: white)
    #it
  ])
  show raw: set text(font: "Cascadia Mono")

  set par(justify: true)

  // Gray background with a superscript footnote number.
  show footnote: it => {
    set text(weight: "regular", size: 12pt)
    highlight(
      fill: gray.transparentize(20%),
      extent: 1pt,
      [#super[#counter(footnote).at(it.location()).first()]],
    )
  }
  set footnote.entry(indent: 0em)

  set figure(supplement: [Figure])
  set figure.caption(separator: [ : ])
  show figure.caption: set text(9pt)
  show figure.caption: emph

  show heading.where(level: 1): it => block(
    above: 28pt,
    below: 16pt,
    [#text(size: 20pt, weight: "extrabold", fill: purple)[#it.body]],
  )

  show heading.where(level: 2): it => block(
    above: 20pt,
    below: 12pt,
    [#text(size: 14pt, weight: "bold", fill: purple)[#it.body]],
  )

  show heading.where(level: 3): it => block(
    above: 15pt,
    below: 8pt,
    [#text(size: 12pt, weight: "bold", fill: purple)[#it.body]],
  )

  show heading.where(level: 4): it => block(
    above: 15pt,
    below: 8pt,
    [#text(size: 10pt, weight: "bold", fill: purple)[#it.body]],
  )

  body
}
