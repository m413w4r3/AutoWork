#import "../UTILS/document_style.typ": apply-document-style
#import "edition_helpers.typ": render-edition

#let edition = json("edition-render-data.json")

#apply-document-style(
  country: edition.edition.bulletin_country,
  period: edition.edition.bulletin_period,
)[
  #render-edition(edition)
]
