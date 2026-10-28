#import "../UTILS/document_style.typ": apply-document-style
#import "../UTILS/colors.typ": dark, light-grey
#import "publication_helpers.typ": render-publication

#let publication = json("render-data.json")

#let publication-header(publication) = {
  let classification = publication.at("classification_label", default: none)
  table(
    columns: (1fr, 5fr),
    stroke: 0.6pt + light-grey,
    align: (left, right),
    [
      #if publication.at("brand_logo_available", default: false) [
        #image("../UTILS/chap.png", height: 22pt)
      ] else [
        #text(size: 9pt, weight: "bold", fill: dark)[Chaps Vision]
      ]
    ],
    [
      #if classification != none [
        #block(
          fill: rgb("#EAF2F2"),
          stroke: 0.4pt + light-grey,
          inset: (x: 5pt, y: 2pt),
          radius: 2pt,
        )[#text(size: 8pt, weight: "bold", fill: dark)[#classification]]
        #linebreak()
      ]
      #text(size: 8pt, weight: "bold", fill: dark)[#publication.at("document_header", default: "Publication")]
    ],
  )
}

#apply-document-style(draft-watermark: publication.at("draft_watermark", default: none))[
  #set page(header: publication-header(publication), header-ascent: 28pt)
  #render-publication(publication)
]
