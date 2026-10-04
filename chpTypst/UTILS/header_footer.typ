#import "colors.typ": dark, light-grey

#let has-text(value) = value != none and value != ""

#let report-header(country: none, period: none) = {
  let title = if has-text(country) {
    "Actualité des codes et infrastructures " + country
  } else {
    ""
  }
  let label = if has-text(period) {
    if title == "" { period } else { title + " — " + period }
  } else {
    title
  }
  table(
    columns: (1fr, 5fr),
    stroke: 0.6pt + light-grey,
    align: (left, right),
    [#image("chap.png")],
    [#if label != "" [#text(size: 11pt, weight: "bold", fill: dark)[#label]]],
  )
}

#let report-footer(country: none, period: none) = context {
  let bulletin = if has-text(country) {
    "Bulletin-" + country + " | Actualité des codes et infrastructures " + country
  } else {
    ""
  }
  let label = if has-text(period) {
    if bulletin == "" { period } else { bulletin + " | " + period }
  } else {
    bulletin
  }
  line(length: 100%, stroke: 0.6pt + light-grey)
  grid(
    columns: (1fr, auto),
    inset: (x: 8pt, y: 4pt),
    [#text(size: 9pt)[#label]],
    [
      #set text(size: 8pt, weight: "bold", fill: dark)
      #counter(page).display() #text("/") #counter(page).final().at(0)
    ],
  )
}
