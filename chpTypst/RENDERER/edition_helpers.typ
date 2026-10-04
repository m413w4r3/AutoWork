#import "../UTILS/colors.typ": purple
#import "../UTILS/helpers.typ": article, article-indexing
#import "publication_helpers.typ": render-publication-body

#let has-text(value) = value != none and value != ""

#let article-number(position) = {
  let value = str(position)
  if position < 10 { "0" + value } else { value }
}

#let render-edition(edition) = {
  align(center)[
    #image("../UTILS/chap.png", width: 18%)
    #v(14pt)
    #text(size: 20pt, weight: "extrabold", fill: purple)[Bulletin de veille CTI]
    #v(10pt)
    #text(size: 14pt, weight: "bold")[#edition.edition.country]
    #if has-text(edition.edition.country_code) [
      #linebreak()
      #text(size: 10pt)[#edition.edition.country_code]
    ]
    #v(8pt)
    #if has-text(edition.edition.bulletin_period) [
      #text(size: 12pt)[Période : #edition.edition.bulletin_period]
    ]
    #if has-text(edition.edition.tlp) [
      #v(6pt)
      #text(size: 10pt)[Classification : #edition.edition.tlp]
    ]
  ]

  pagebreak()
  heading(level: 1)[Articles]

  let article-index = edition.publications.map(publication => (
    article-number(publication.position),
    publication.title,
  ))
  article-indexing(article-index)

  for publication in edition.publications {
    article(
      category: "Article",
      number: article-number(publication.position),
      title: publication.title,
      events: none,
      body: render-publication-body(publication),
    )
  }
}
