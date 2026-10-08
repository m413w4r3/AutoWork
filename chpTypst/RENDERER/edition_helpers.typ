#import "../UTILS/colors.typ": purple
#import "../UTILS/helpers.typ": article-indexing, better-link, styled-table, tag
#import "publication_helpers.typ": render-publication-body

#let has-text(value) = value != none and value != ""

#let index-entries(publications) = publications.map(publication => (
  publication.number,
  better-link(label(publication.anchor))[#publication.title],
))

#let render-article(publication) = [
  #pagebreak()
  #heading(level: 1, numbering: none)[#publication.title] #label(publication.anchor)
  #grid(
    columns: (1fr, auto),
    align: (left, right),
    [#tag("Article")],
    [#text(weight: "bold", fill: purple)[#publication.number]],
  )
  #v(10pt)
  #render-publication-body(publication)
]

#let render-brief(publication) = [
  #pagebreak()
  #heading(level: 2, numbering: none)[#publication.title] #label(publication.anchor)
  #grid(
    columns: (1fr, auto),
    align: (left, right),
    [#tag("Brève")],
    [#text(weight: "bold", fill: purple)[#publication.number]],
  )
  #v(6pt)
  #set text(size: 10pt)
  #render-publication-body(publication)
]

#let render-edition(edition) = {
  align(center)[
    #image("../UTILS/chap.png", width: 18%)
    #v(14pt)
    #text(size: 20pt, weight: "extrabold", fill: purple)[#edition.edition.bulletin_title]
    #v(10pt)
    #text(size: 14pt, weight: "bold")[#edition.edition.country]
    #if has-text(edition.edition.country_code) [
      #linebreak()
      #text(size: 10pt)[#edition.edition.country_code]
    ]
    #if has-text(edition.edition.bulletin_period) [
      #v(8pt)
      #text(size: 12pt)[#edition.edition.bulletin_period]
    ]
    #v(6pt)
    #text(size: 11pt)[Bulletin n° #edition.edition.bulletin_number]
    #linebreak()
    #text(size: 10pt)[Identifiant : #edition.edition.bulletin_identifier]
    #if has-text(edition.edition.tlp) [
      #v(6pt)
      #text(size: 10pt)[Classification : #edition.edition.tlp]
    ]
  ]

  v(16pt)
  heading(level: 1)[Résumé de l'actualité #tag("Articles")]
  let articles = edition.publications.filter(publication => publication.kind == "article")
  article-indexing(index-entries(articles))

  v(16pt)
  heading(level: 1)[Autres brèves relevées #tag("Brèves")]
  let briefs = edition.publications.filter(publication => publication.kind == "brief")
  if briefs.len() == 0 {
    text("Aucune brève relevée dans cette édition.")
  } else {
    article-indexing(index-entries(briefs))
  }

  v(16pt)
  heading(level: 1)[Règles Suricata]
  [- Aucune règle Suricata n'est projetée dans ce rendu d'édition.]

  v(12pt)
  heading(level: 1)[Suivi des indicateurs]
  [- Échantillons relevés : #edition.file_ioc_count]

  v(16pt)
  styled-table(
    (1fr, 4fr),
    cell-align: (center, left),
    [Fichier intégré], [Description],
    [Article CSV], [Un fichier `article.csv` par publication, avec les IOC publiés et les IOC originaux.],
    [Export IOC], [Archive #raw("iocs_" + edition.edition.bulletin_identifier + ".zip") : CSV, manifeste JSON et #raw("legacy_iocs_" + edition.edition.bulletin_identifier + ".txt").],
    [STIX], [Génération déléguée en aval ; aucun fichier STIX n'est intégré à cette édition.],
    [Excel], [Génération déléguée en aval ; aucun tableur Excel n'est intégré à cette édition.],
    [ZIP des annexes], [Non généré par AutoWork dans ce rendu.],
  )

  for publication in articles {
    render-article(publication)
  }
  for publication in briefs {
    render-brief(publication)
  }
}
