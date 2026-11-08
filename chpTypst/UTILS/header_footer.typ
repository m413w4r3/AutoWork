#import "colors.typ": dark, light-grey

#let has-text(value) = value != none and value != ""

#let report-header(
  country: none,
  period: none,
  bulletin-number: none,
  document-header: none,
  brand-logo-available: true,
) = {
  let bulletin-label = if bulletin-number != none {
    "Bulletin n°" + str(bulletin-number) + if has-text(period) { " / " + period } else { "" }
  } else if has-text(document-header) {
    document-header
  } else if has-text(country) {
    let title = "Actualité des codes et infrastructures " + country
    if has-text(period) { title + " — " + period } else { title }
  } else if has-text(period) {
    period
  } else {
    ""
  }
  block(
    width: 100%,
    stroke: 0.4pt + light-grey,
    inset: (x: 4pt, y: 2pt),
  )[
    #grid(
      columns: (auto, 1fr),
      column-gutter: 12pt,
      align: (left, right),
      [
        #if brand-logo-available [
          #image("chap.png", height: 22pt)
        ] else [
          #text(size: 9pt, weight: "bold", fill: dark)[Chaps Vision]
        ]
      ],
      [#if bulletin-label != "" [#align(right)[#text(size: 9pt, weight: "bold", fill: dark)[#bulletin-label]]]],
    )
  ]
}

#let report-footer(
  country: none,
  period: none,
  bulletin-number: none,
  bulletin-identifier: none,
  document-header: none,
  draft-notice: none,
) = context {
  if draft-notice != none [
    #block(
      width: 100%,
      height: 24pt,
      fill: rgb("#8c1d2c"),
      radius: 2pt,
    )[
      #grid(
        columns: (1fr, auto),
        inset: (x: 8pt, y: 4pt),
        align: (center, right),
        [
          #align(center + horizon)[
            #text(size: 10pt, weight: "bold", fill: white, tracking: 0.5pt)[#draft-notice]
          ]
        ],
        [
          #set text(size: 8pt, weight: "bold", fill: white)
          #counter(page).display() #text("/") #counter(page).final().at(0)
        ],
      )
    ]
  ] else {
    let footer-title = if has-text(country) {
      "Actualité des codes et infrastructures " + country
    } else if has-text(document-header) {
      document-header
    } else {
      ""
    }
    let bulletin-label = if has-text(bulletin-number) {
      "Bulletin n°" + str(bulletin-number)
    } else {
      ""
    }
    let identifier = if has-text(bulletin-identifier) {
      "Bulletin-" + str(bulletin-identifier)
    } else {
      footer-title
    }
    if has-text(bulletin-number) or has-text(bulletin-identifier) or has-text(country) [
      #block(width: 100%)[
        #grid(
        columns: (1fr, auto, 1fr),
        rows: (auto, auto),
        align: (left, center, right),
        inset: (x: 4pt, y: 2pt),
        stroke: 0.4pt + light-grey,
        [#text(size: 7pt)[#identifier]],
        [],
        [#if bulletin-label != "" [#text(size: 7pt, weight: "bold", fill: dark)[#bulletin-label]]],
        [#text(size: 7pt)[#footer-title]],
        [
          #set text(size: 8pt, weight: "bold", fill: dark)
          #counter(page).display() #text("/") #counter(page).final().at(0)
        ],
        [#text(size: 7pt)[#period]],
        )
      ]
    ] else [
      #line(length: 100%, stroke: 0.6pt + light-grey)
      #grid(
        columns: (1fr, auto),
        inset: (x: 8pt, y: 4pt),
        [#text(size: 9pt)[#footer-title]],
        [
          #set text(size: 8pt, weight: "bold", fill: dark)
          #counter(page).display() #text("/") #counter(page).final().at(0)
        ],
      )
    ]
  }
}
