#import "colors.typ": *

#set grid(gutter: 1em)


// Petit label de section
#let tag(content) = box(
  fill: purple-light,
  inset: (x: 8pt, y: 4pt),
  radius: 3pt,
)[
  #set text(
    size: 8pt,
    weight: "bold",
    fill: black,
    tracking: 0.5pt,
  )

  #upper(content)
]

#let section-title(content) = [
  #v(4pt)

  #text(
    size: 14pt,
    weight: "extrabold",
    fill: purple,
  )[
    #content
  ]

]

#let better-link(target, body) = {
  if type(target) == str and not str.starts-with(target, "http") {
    body
  } else {
    text(
      fill: rgb("#0563C1"),
    )[
      #underline(
        link(target)[#body]
      )
    ]
  }
}

// Chronologie
#let timeline(events) = [
  #for event in events {
    let source-urls = if event.len() > 2 {
      let sources = event.at(2)
      if type(sources) == str {
        if sources == "" { () } else { (sources,) }
      } else if sources == none {
        ()
      } else {
        sources
      }
    } else {
      ()
    }

    grid(
      columns: (0.25cm, auto, 1fr),
      gutter: 2pt,
      pad(left: 2em)[
        #text(
          size: 11pt,
          fill: dark,
          weight: "bold",
        )[•]
      ],
      pad(left: 2em)[
        #text(
          size: 11pt,
          weight: "extrabold",
          fill: purple,
        )[
          #event.at(0)
        ]
        : #event.at(1)
        #for url in source-urls {
          if url != none and url != "" {
            footnote[#better-link(url)[#url]]
          }
        }
      ]
    )

  }
]

#let separator() = line(
  length: 100%,
  stroke: 0.6pt + light-grey,
)


#let vueEnsemble(items) = box(
  width: 100%,
  fill: purple-light,
  inset: 12pt,
  radius: 4pt,
)[
  #for item in items {
    pad(left: 1em)[
       • #text(
        weight: "semibold",
        fill: purple,
      )[ #item.first() ]: #item.last()
    ]

  }
]

#let noteAnalyste(content) = {
  block(
    fill: notion-gray-bg,
    radius: 4pt,
    width: 100%,
    inset: 8pt,
    breakable: true,
  )[
    #pad(
      top: 6pt,
      bottom: 8pt
    )[
      #text(
        size: 14pt,
        weight: "extrabold",
        fill: purple-dark,
      )[Note de l'analyste]
      
      #content
    ]
  ]
}


#let article-indexing(article-index) = {
  for article in article-index {
    box(
      width: 100%,
      inset: (y: 10pt, x: 12pt),
      stroke: 0.5pt + light-grey,
      radius: 4pt,
      [
        #grid(
          columns: (0.6cm, auto, auto),
  
          [
            #text(
              weight: "bold",
              fill: accent,
            )[
              #article.first()
            ]
          ],
  
          [
            #text(
              weight: "bold",
              fill: dark,
            )[
              #article.at(1)
            ]
          ],
  
        )
      ]
    )
  
  }
}


#let article(
  category: "Article",
  number: "01",
  title: "",
  /*
  (
    ("jour mois année", [contenu], "URL"),
    ....
  )
  */
  events: (),
  /*
  overview: (
    ("Nom du bullet point (par exemple, Arsenal)", [contenu]),
  )
  */
  overview: (),
  body: [],
) = {
  pagebreak()

  text(
    size: 16pt,
    weight: "regular",
    fill: purple,
  )[
    #title
  ]

  grid(
    columns: (1fr, auto),
    align: (left, right),

    [
      #tag(category)
    ],

    [
      #text(
        size: 11pt,
        weight: "bold",
        fill: purple,
      )[
        #number
      ]
    ],
  )

  if events != none [
    // Chronologie
    #section-title[Chronologie]
    #timeline(events)
    #v(10pt)
  ]

  if overview != () [
    #block(
      sticky: true, 
      [
      #section-title[Vue d'ensemble]
      #vueEnsemble(overview)
      ])
    #v(10pt)
  ]
  
  
  if events != none [
    // Synthèse
    #section-title[Synthèse]
    #set par(spacing: 2em)
  ]
  body
}






#let styled-table(columns, cell-align: auto, ..content) = {
  let cells = content.pos()

  let styled-cells = cells.enumerate().map(((i, cell)) => {
    let row = calc.floor(i / columns.len())

    if row == 0 {
      [
        #set text(
          weight: "bold",
          fill: white,
        )
        #cell
      ]
    } else {
      cell
    }
  })

  align(center, table(
    columns: columns,

    align: (x, y) => {
      if y == 0 {
        left
      } else {
        cell-align.at(x)
      }
    },

    fill: (_, y) => {
      if y == 0 {
        purple
      } else {
        none
      }
    },

    stroke: 1pt + gray,
    ..styled-cells,
  ))
}

#let fn(url) = {
  footnote[
    #better-link(url)[#url]
  ]
}

#let ioc(content) = {
  text(font: "Cascadia Mono")[#content]
}

#let vt(body) = highlight( fill: luma(0%), radius: 2pt )[ #text( font: "Cascadia Mono", fill: white, size:11pt)[#body]]

#let ioc-list(
  title: [IOC],
  ips: [],
  domains: [],
  urls: [],
  files: [],
  emails: [],
  hashes: [],
) = {
  set par(leading: 0pt)
  let parse-list(value) = {
    if type(value) == array {
      value
    } else if type(value) == content {
      if "children" in value.fields() { value.children } else { (value,) }
    } else if value == none {
      ()
    } else {
      (value,)
    }
  }

  let ioc-listt(items) = {
    
    for item in items {
      text(font: "Cascadia Mono")[#item]
      linebreak()
    }
  }

  let ips = parse-list(ips)
  let domains = parse-list(domains)
  let urls = parse-list(urls)
  let emails = parse-list(emails)
  let hashes = parse-list(hashes)
  let files = parse-list(files)

  [
    #text(size: 13pt, fill:purple-dark)[*#title*]

    #if ips.len() > 0 [
      Adresses IP : \
      #ioc-listt(ips)
    ]

    #if domains.len() > 0 [
      Noms de domaine : \
      #ioc-listt(domains)
    ]

    #if urls.len() > 0 [
      URL : \
      #ioc-listt(urls)
    ]

    #if emails.len() > 0 [
      Adresses e-mail : \
      #ioc-listt(emails)
    ]

    #if hashes.len() > 0 [
      Empreintes (hashes) : \
      #ioc-listt(hashes)
    ]

    #if files.len() > 0 [
      Fichiers : \
      #ioc-listt(files)
    ]
  ]
}

#let source-list(sources) = [
  #for source in sources {
    let title = source.at("title", default: none)
    let publisher = source.at("publisher", default: none)
    let date = source.at("date", default: none)

    block(above: 4pt, below: 4pt)[
      #if title != none and title != "" {
        text(weight: "bold")[#title]
      }
      #if publisher != none and publisher != "" {
        if title != none and title != "" [ · ]
        text(size: 9pt, fill: grey)[#publisher]
      }
      #if date != none and date != "" {
        if (title != none and title != "") or (publisher != none and publisher != "") [ · ]
        text(size: 9pt, fill: grey)[#date]
      }
      #linebreak()
      #better-link(source.url)[#source.url]
    ]
  }
]
