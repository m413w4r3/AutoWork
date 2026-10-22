#import "../UTILS/document_style.typ": apply-document-style
#import "edition_helpers.typ": render-edition

#let edition = json("edition-render-data.json")

#apply-document-style(
  country: edition.edition.bulletin_country,
  period: edition.edition.bulletin_period,
)[
  #set page(
    header: context {
      if counter(page).get().first() > 1 {
        align(right)[
          #text(size: 8pt, fill: gray)[#edition.at("document_header", default: "Bulletin de veille CTI")]
        ]
      }
    },
    header-ascent: 14pt,
  )
  #render-edition(edition)
]
