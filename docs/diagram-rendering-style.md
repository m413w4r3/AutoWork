# Publication diagram and table rendering

## Visual contract

Publication diagrams use a white canvas, horizontal direction for compact
graphs, rounded white rectangles, small bold labels, thin black connectors, and
a single orange malware outline. Groups use a dashed blue-grey frame with a
light-blue label tab. The bounded D2 renderer accepts no external image
resources; until role icons are embedded and allowlisted, every role uses the
readable rectangle fallback.

Diagrams switch to a vertical layout above eight nodes or when any node, edge,
or group label exceeds 44 characters. Typst preserves the SVG aspect ratio and
limits a standard diagram to 35% of the available page height (52% when a
status legend is present). Protocol figures inside an analyst-opinion box may
use up to 82% of the available page height. Dense protocol sequence, state, and
field views use ordered multi-column card grids so they can use the box width;
their title, status, caption, and legend remain below the image. D2 label sizes
scale by graph density (12–18 px for nodes and 11–16 px for edges); labels are
wrapped more tightly as graphs grow. The corpus layout check measures at least
8 pt node text and 7 pt relation labels after fitting. Captions are centered
and italic below the diagram.

Sequence views whose message labels start with a lowercase group tag such as
`[a]` render one independent lifeline panel per group. Messages retain their
model order inside each panel and have no global sequence numbers; the caption
states that messages are handler-derived and their order was not observed.
Untagged messages share a `Hors groupes` panel. When no message has a group tag,
the existing numbered sequence panels remain in use. Typst keeps a grouped
sequence and its caption together on one page, fitting the SVG within the
82%-height limit.

Publication tables use an 8 pt sans-serif face, a muted blue-grey header
(`#5D7374`) with white bold labels, fine grey borders, white body rows, and no
row striping. This matches the IOC tables in the reference bulletin. The style
lives in the publication renderer so the human-article table helper remains
unchanged.

## Versioning and rebuild scope

- `D2_COMPILER_VERSION` remains pinned to the installed D2 executable. Visual
  source or SVG changes increment `DIAGRAM_COMPILATION_POLICY_VERSION`, now
  `diagram-d2-svg-v16-m21-grouped-protocol-sequences`. Protocol diagram
  projections also pin `protocol-diagram-projection-v12-m21-grouped-sequence-captions`.
- Publication Typst changes increment both `PUBLICATION_RENDERER_VERSION` and
  `PUBLICATION_RENDER_POLICY_VERSION`. The template bundle SHA is also part of
  the publication render input hash.
- During `PUBLICATION` assembly, an absent or stale diagram asset policy causes
  the existing canonical `DiagramSpecV1` to be recompiled and stored. The
  resulting publication document references the new content-addressed SVG.
  This uses the saved graph and does not call a model or rebuild synthesis or
  editorial enrichment.
- The changed template and publication content then produce a new PDF render
  input hash. Reconstruct `PUBLICATION` and its PDF render only.

Canonical diagram semantics remain stable for a style-only update. The D2
source and SVG hashes change with layout, while the compilation policy version
invalidates only the derived diagram asset. Publication renderer versions
invalidate the rendered document and PDF cache.
