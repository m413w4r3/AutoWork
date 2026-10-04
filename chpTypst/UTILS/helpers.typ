#import "colors.typ": *

#set grid(gutter: 1em)

// The semantic helpers receive only strings from the JSON render projection.
// text(string) creates literal content, so source text is never parsed as Typst.
#let semantic-plain(body) = text(body)
#let semantic-actor(body) = text(weight: "bold", fill: accent)[#text(body)]
#let semantic-campaign(body) = text(weight: "bold", fill: purple)[#text(body)]
#let semantic-malware(body) = text(weight: "bold", fill: purple-dark)[#text(body)]
#let semantic-tool(body) = text(weight: "bold", fill: accent)[#text(body)]
#let semantic-product(body) = text(weight: "bold", fill: dark)[#text(body)]
#let semantic-english-term(body) = emph(text(body))
#let semantic-technical(body) = text(size: 10pt, fill: purple-dark)[#text(body)]
#let semantic-technical-literal(body) = text(
  font: "Cascadia Mono",
  size: 9pt,
  fill: purple-dark,
)[#text(body)]
#let semantic-ioc(body) = text(font: "Cascadia Mono", size: 9pt, fill: accent)[#text(body)]
#let semantic-path(body) = text(font: "Cascadia Mono", size: 9pt)[#text(body)]
#let semantic-command(body) = text(font: "Cascadia Mono", size: 9pt, fill: dark)[#text(body)]
#let semantic-protocol-field(body) = text(font: "Cascadia Mono", size: 9pt, fill: purple-dark)[#text(body)]
#let semantic-source(body) = text(size: 9pt, fill: accent)[#text(body)]
#let semantic-proof(body) = text(size: 9pt, fill: purple-dark)[#text(body)]

#let semantic-span(span) = {
  let style = span.style
  if style == "semantic-plain" { semantic-plain(span.text) }
  else if style == "semantic-actor" { semantic-actor(span.text) }
  else if style == "semantic-campaign" { semantic-campaign(span.text) }
  else if style == "semantic-malware" { semantic-malware(span.text) }
  else if style == "semantic-tool" { semantic-tool(span.text) }
  else if style == "semantic-product" { semantic-product(span.text) }
  else if style == "semantic-english-term" { semantic-english-term(span.text) }
  else if style == "semantic-technical" { semantic-technical(span.text) }
  else if style == "semantic-technical-literal" { semantic-technical-literal(span.text) }
  else if style == "semantic-ioc" { semantic-ioc(span.text) }
  else if style == "semantic-path" { semantic-path(span.text) }
  else if style == "semantic-command" { semantic-command(span.text) }
  else if style == "semantic-protocol-field" { semantic-protocol-field(span.text) }
  else if style == "semantic-source" { semantic-source(span.text) }
  else if style == "semantic-proof" { semantic-proof(span.text) }
  else { panic("unsupported semantic style: " + style) }
}

#let semantic-text(spans) = {
  for span in spans { semantic-span(span) }
}

#let semantic-or-plain(body, spans) = {
  if spans == none { text(body) } else { semantic-text(spans) }
}


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

#let timeline-event-source-urls(event) = {
  let sources = if event.len() > 2 { event.at(2) } else { () }
  if type(sources) == str {
    if sources == "" { () } else { (sources,) }
  } else if sources == none {
    ()
  } else {
    sources
  }
}

#let timeline(events) = {
  let all-source-urls = events.fold((), (all, event) => {
    timeline-event-source-urls(event).fold(all, (result, url) => {
      if url in result { result } else { result + (url,) }
    })
  })
  for event in events {
    let semantic-spans = if event.len() > 3 { event.at(3) } else { none }
    let source-urls = timeline-event-source-urls(event)
    let source-indices = source-urls
      .filter(url => url != none and url != "")
      .map(url => all-source-urls.position(candidate => candidate == url) + 1)

    block[
      #if event.at(0) != "" [
        #text(
          size: 11pt,
          weight: "extrabold",
          fill: purple,
        )[
          #event.at(0)
        ]
        :
      ]
      #semantic-or-plain(event.at(1), semantic-spans)
      #if source-indices.len() > 0 {
        super[#source-indices.map(str).join(", ")]
      }
    ]
  }

  for (source-index, url) in all-source-urls.enumerate() {
    block[
      #super[#str(source-index + 1)]
      #h(3pt)
      #better-link(url)[#url]
    ]
  }
}

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

  set text(size: 9pt)
  align(center, table(
    columns: columns,
    inset: (x: 5pt, y: 4pt),

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

    stroke: 0.6pt + gray,
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
