#import "../UTILS/document_style.typ": apply-document-style
#import "edition_helpers.typ": render-edition, render-edition-footer

#let edition = json("edition-render-data.json")

#apply-document-style[
  #set page(header: none, footer: render-edition-footer(edition))
  #render-edition(edition)
]
