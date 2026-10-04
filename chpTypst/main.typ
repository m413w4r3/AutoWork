#import "UTILS/colors.typ": *
#import "UTILS/document_style.typ": apply-document-style
#import "UTILS/helpers.typ": tag, section-title, timeline, separator, vueEnsemble, noteAnalyste, better-link, article-indexing, article, styled-table

#apply-document-style[
  #set enum(indent: 2em)
  #set list(indent: 2em)

// PAGE DE GARDE


// Page de garde générique sans métadonnées d'édition.
#align(center)[
  #text(
    size: 20pt,
    weight: "extrabold",  
    fill: purple,
  )[Actualité des codes et infrastructures]
]

#v(16pt)

== Résumé de l'actualité #tag("Articles")


// PAGE DE GARDE ARTICLE

#let article-index = (
  ("01", better-link(<article01>)[[Groupe] Titre]),
  ("02", better-link(<article02>)[[Groupe] Titre]),
)

#article-indexing(article-index)

#v(16pt)

== Autres brèves relevées #tag("Brèves")


// PAGE DE GARDE BREVE
#v(8pt)

#let breve-index = (
  ("03", better-link(<breve01>)[Titre 1]),
  ("04", better-link(<breve02>)[Titre 2]),
)

#article-indexing(breve-index)

#v(16pt)

== Règles Suricata _"Emerging Threats"_
#v(8pt)
- Règles modifiées : ?

#v(16pt)
== Suivi des indicateurs
- Échantillons relevés : ?
    
#v(16pt)

#styled-table(
  (1fr, 4fr),
  cell-align: (center, left),

  [Fichier intégré], [Description],
  [📎 ?], [Données CTI issues du bulletin, au format STIX.],
  [📎 ?], [Synthèse des thèmes et des IOC collectés, sous forme d’un tableau Excel.],
  [📎 ?], [Annexes associées au bulletin, sous forme d’une archive ZIP ; le fichier `.txt` doit être renommé avec une extension `.zip` avant extraction.],
)

////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////

// ARTICLES
#include "articles/article01/article01.typ"
<article01> 

#include "articles/article02/article02.typ"
<article02> 


// BREVES
#include "breves/breve01/breves01.typ"
<breve01>

#include "breves/breve02/breve02.typ"
<breve02>

#pagebreak()
#include "articles_non_traites.typ"
]
