#import "../UTILS/document_style.typ": apply-document-style
#import "publication_helpers.typ": render-publication

#let publication = json("render-data.json")

#apply-document-style(draft-watermark: publication.at("draft_watermark", default: none))[
  #set page(
    header: context {
      if counter(page).get().first() > 1 {
        align(right)[
          #text(size: 8pt, fill: gray)[#publication.at("document_header", default: "Publication")]
        ]
      }
    },
    header-ascent: 14pt,
  )
  #render-publication(publication)
]
