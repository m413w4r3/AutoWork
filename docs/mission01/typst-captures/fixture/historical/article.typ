#import "/home/nill/perso/work/AutoWork/docs/references/chpTypst/UTILS/helpers.typ": article, noteAnalyste, styled-table, ioc-list


// The surrounding page settings match the approved historical main.typ.
#set text(font: "Hanken Grotesk", size: 11pt, lang: "fr")
#set page(paper: "a4", margin: (top: 2.3cm, bottom: 2.3cm, left: 2.2cm, right: 2.2cm))

#article(
  category: "Article",
  number: "07",
  title: "M01-T parity fixture — synthetic only",
  events: (
    ("2026-10-01", [Synthetic event; no external observations.], "https://example.invalid/reference"),
  ),
  overview: (
    ("Victimologie", [Synthetic target class only.]),
    ("Arsenal", [Synthetic mechanism only.]),
    ("Objectif principal", [Exercise layout; no threat claim.]),
  ),
  body: [
    #text("Synthetic lead paragraph for layout comparison.")
    #parbreak()
    #text(weight: "bold")[Synthetic IOC status matrix — test labels only]
    #parbreak()
    #styled-table(
      (1.2fr, 1fr),
      cell-align: (left, left),
      [Indicator value], [Evidence level], [192.0.2.17], [sourcé (test only)], [198.51.100.21], [validé (test only)], [203.0.113.31], [candidat (test only)], [192.0.2.41], [écarté (test only)],
    )
    #parbreak()
    #text(fill: gray)[All values use RFC 5737 documentation ranges; they are not indicators or evidence.]
    #parbreak()
    #figure(image("fixture.svg", width: 55%), caption: [Synthetic flow diagram; no observed infrastructure.])
    #noteAnalyste[Analyst note fixture; no external observations or evidence.]
  #ioc-list(title: [Sourcé (test only)], ips: [192.0.2.17])
  #ioc-list(title: [Validé (test only)], ips: [198.51.100.21])
  #ioc-list(title: [Candidat (test only)], ips: [203.0.113.31])
  #ioc-list(title: [Écarté (test only)], ips: [192.0.2.41])
  ],
)
