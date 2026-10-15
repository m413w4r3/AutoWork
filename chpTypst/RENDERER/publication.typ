#import "../UTILS/document_style.typ": apply-document-style
#import "publication_helpers.typ": render-publication

#let publication = json("render-data.json")

#apply-document-style(draft-watermark: publication.at("draft_watermark", default: none))[
  #render-publication(publication)
]
