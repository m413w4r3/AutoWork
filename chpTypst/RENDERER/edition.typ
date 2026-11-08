#import "../UTILS/document_style.typ": apply-document-style
#import "edition_helpers.typ": render-edition

#let edition = json("edition-render-data.json")

#apply-document-style(
  country: edition.edition.country,
  period: edition.edition.bulletin_period,
  bulletin-number: edition.edition.bulletin_number,
  bulletin-identifier: edition.edition.bulletin_identifier,
)[
  #render-edition(edition)
]
