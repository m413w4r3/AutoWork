#import "../UTILS/document_style.typ": apply-document-style
#import "publication_helpers.typ": render-publication

#let publication = json("render-data.json")

#apply-document-style(
  document-header: publication.at("document_header", default: "Publication"),
  brand-logo-available: publication.at("brand_logo_available", default: false),
  draft-watermark: publication.at("draft_watermark", default: none),
)[
  #render-publication(publication)
]
