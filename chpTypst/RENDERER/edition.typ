#import "../UTILS/document_style.typ": apply-document-style
#import "edition_helpers.typ": render-edition

#let edition = json("edition-render-data.json")

#apply-document-style[
  #set page(header: none, footer: none)
  #render-edition(edition)
]
