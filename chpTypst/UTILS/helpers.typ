#import "colors.typ": *

#set grid(gutter: 1em)

// Rendered literal data stays exact. This tiny positive space creates a real
// break opportunity without introducing a visible or extractable separator.
#let display-text(item) = {
  if type(item) == str {
    text(item)
  } else {
    let chunks = item.at("break_chunks", default: ())
    if chunks.len() == 0 {
      text(item.text)
    } else {
      let break-after = item.at("break_after", default: ())
      for (index, chunk) in chunks.enumerate() {
        text(chunk)
        if index < chunks.len() - 1 {
          let should-break = if break-after.len() == chunks.len() - 1 {
            break-after.at(index)
          } else {
            true
          }
          if should-break { h(0.01pt) }
        }
      }
    }
  }
}

#let semantic-plain(body) = body
#let semantic-actor(body) = text(weight: "bold", fill: accent)[#body]
#let semantic-campaign(body) = text(weight: "bold", fill: purple)[#body]
#let semantic-malware(body) = text(weight: "bold", fill: purple-dark)[#body]
#let semantic-tool(body) = text(weight: "bold", fill: accent)[#body]
#let semantic-product(body) = text(weight: "bold", fill: dark)[#body]
#let semantic-english-term(body) = emph(body)
#let semantic-technical(body) = text(size: 10pt, fill: purple-dark)[#body]
#let semantic-technical-literal(body) = text(
  font: "Cascadia Mono",
  size: 9pt,
  fill: purple-dark,
  hyphenate: false,
)[#body]
#let semantic-table-technical(body) = text(
  font: "Cascadia Mono",
  size: 9pt,
  fill: purple-dark,
  hyphenate: false,
)[#body]
#let semantic-ioc(body) = text(font: "Cascadia Mono", size: 9pt, fill: accent, hyphenate: false)[#body]
#let semantic-table-ioc(body) = text(
  font: "Cascadia Mono",
  size: 9pt,
  fill: accent,
  hyphenate: false,
)[#body]
#let semantic-path(body) = text(font: "Cascadia Mono", size: 9pt, hyphenate: false)[#body]
#let semantic-table-path(body) = text(font: "Cascadia Mono", size: 9pt, hyphenate: false)[#body]
#let semantic-command(body) = text(font: "Cascadia Mono", size: 9pt, fill: dark, hyphenate: false)[#body]
#let semantic-table-command(body) = text(
  font: "Cascadia Mono",
  size: 9pt,
  fill: dark,
  hyphenate: false,
)[#body]
#let semantic-protocol-field(body) = text(font: "Cascadia Mono", size: 9pt, fill: purple-dark, hyphenate: false)[#body]
#let semantic-source(body) = text(size: 9pt, fill: accent)[#body]
#let semantic-proof(body) = text(size: 9pt, fill: purple-dark)[#body]

#let semantic-span(span) = {
  let style = span.style
  let body = display-text(span)
  if style == "semantic-plain" { semantic-plain(body) }
  else if style == "semantic-actor" { semantic-actor(body) }
  else if style == "semantic-campaign" { semantic-campaign(body) }
  else if style == "semantic-malware" { semantic-malware(body) }
  else if style == "semantic-tool" { semantic-tool(body) }
  else if style == "semantic-product" { semantic-product(body) }
  else if style == "semantic-english-term" { semantic-english-term(body) }
  else if style == "semantic-technical" { semantic-technical(body) }
  else if style == "semantic-technical-literal" { semantic-technical-literal(body) }
  else if style == "semantic-table-technical" { semantic-table-technical(body) }
  else if style == "semantic-ioc" { semantic-ioc(body) }
  else if style == "semantic-table-ioc" { semantic-table-ioc(body) }
  else if style == "semantic-path" { semantic-path(body) }
  else if style == "semantic-table-path" { semantic-table-path(body) }
  else if style == "semantic-command" { semantic-command(body) }
  else if style == "semantic-table-command" { semantic-table-command(body) }
  else if style == "semantic-protocol-field" { semantic-protocol-field(body) }
  else if style == "semantic-source" { semantic-source(body) }
  else if style == "semantic-proof" { semantic-proof(body) }
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

#let section-title(content) = block(sticky: true)[
  #v(4pt)

  #text(
    size: 14pt,
    weight: "extrabold",
    fill: purple,
  )[#content]
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






#let styled-table(columns, cell-align: auto, title: none, ..content) = {
  let cells = content.pos()
  let column-count = columns.len()
  let header-cells = cells.slice(0, column-count).map(cell => [
    #set text(weight: "bold", fill: white)
    #cell
  ])
  let body-cells = cells.slice(column-count).enumerate().map(((index, cell)) => {
    let row = calc.floor(index / column-count)
    if calc.rem(row, 2) == 0 {
      [#block(sticky: true, breakable: false, above: 0pt, below: 0pt)[#cell]]
    } else {
      [#block(breakable: false, above: 0pt, below: 0pt)[#cell]]
    }
  })
  let body-row-count = calc.floor((cells.len() - column-count) / column-count)

  set text(size: 9pt)
  let rendered-table = align(center, table(
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
    table.header(repeat: true, ..header-cells),
    ..body-cells,
  ))

  if body-row-count <= 12 {
    block(
      breakable: false,
      above: if title == none { 0pt } else { 6pt },
    )[
      #if title != none [
        #title
        #v(4pt)
      ]
      #rendered-table
    ]
  } else {
    if title != none [
      #block(sticky: true, above: 6pt)[
        #title
        #v(4pt)
      ]
    ]
    rendered-table
  }
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
  note: none,
  ips: [],
  domains: [],
  urls: [],
  emails: [],
  hashes: [],
) = {
  set text(hyphenate: false)
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

  let ioc-listt(items, skip: 0) = {
    for (index, item) in items.enumerate() {
      if index >= skip {
        block(breakable: false, above: 0pt, below: 0pt)[
          #text(font: "Cascadia Mono", size: 8.75pt)[#display-text(item)]
        ]
        linebreak()
      }
    }
  }

  let ioc-first-lines(items, take) = {
    for (index, item) in items.enumerate() {
      if index < take {
        text(font: "Cascadia Mono", size: 8.75pt)[#display-text(item)]
        linebreak()
      }
    }
  }

  let ioc-sublist(label, items, title: none, note: none) = {
    if items.len() > 0 [
      #let first-count = calc.min(3, items.len())
      #let above-space = if title == none { 4pt } else { 8pt }
      #block(
        sticky: true,
        breakable: false,
        above: above-space,
        below: 0pt,
      )[
        #if title != none [
          #text(size: 13pt, fill: purple-dark)[*#title*]
          #linebreak()
        ]
        #if note != none and note != "" [
          #text(size: 9pt)[#note]
          #linebreak()
        ]
        #text(label + " :")
        #linebreak()
        #ioc-first-lines(items, first-count)
      ]
      #if items.len() > first-count [
        #v(6pt)
      ]
      #ioc-listt(items, skip: first-count)
    ]
  }

  let ips = parse-list(ips)
  let domains = parse-list(domains)
  let urls = parse-list(urls)
  let emails = parse-list(emails)
  let hashes = parse-list(hashes)

  [
    #if ips.len() > 0 [
      #ioc-sublist("Adresses IP", ips, title: title, note: note)
    ]

    #if domains.len() > 0 [
      #ioc-sublist(
        "Noms de domaine",
        domains,
        title: if ips.len() == 0 { title } else { none },
        note: if ips.len() == 0 { note } else { none },
      )
    ]

    #if urls.len() > 0 [
      #ioc-sublist(
        "URL",
        urls,
        title: if ips.len() == 0 and domains.len() == 0 { title } else { none },
        note: if ips.len() == 0 and domains.len() == 0 { note } else { none },
      )
    ]

    #if emails.len() > 0 [
      #ioc-sublist(
        "Adresses e-mail",
        emails,
        title: if ips.len() == 0 and domains.len() == 0 and urls.len() == 0 {
          title
        } else {
          none
        },
        note: if ips.len() == 0 and domains.len() == 0 and urls.len() == 0 {
          note
        } else {
          none
        },
      )
    ]

    #if hashes.len() > 0 [
      #ioc-sublist(
        "Fichiers",
        hashes,
        title: if ips.len() == 0 and domains.len() == 0 and urls.len() == 0 and emails.len() == 0 {
          title
        } else {
          none
        },
        note: if ips.len() == 0 and domains.len() == 0 and urls.len() == 0 and emails.len() == 0 {
          note
        } else {
          none
        },
      )
    ]

    #if ips.len() + domains.len() + urls.len() + emails.len() + hashes.len() == 0 [
      #block(sticky: true, above: 8pt, below: 0pt)[
        #text(size: 13pt, fill: purple-dark)[*#title*]
        #if note != none and note != "" [
          #linebreak()
          #text(size: 9pt)[#note]
        ]
      ]
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
