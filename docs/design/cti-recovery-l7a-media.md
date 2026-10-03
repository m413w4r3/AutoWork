# L7a source media collection

L7a collects media lazily when the existing source figure inventory is built
for Editorial Enrichment. The archived HTML/PDF remains the input of record;
the inventory resolves references, then uses the source collection HTTP client
to fetch only media still eligible after deterministic exclusions. The same
SSRF checks, DNS pinning, redirect validation, transfer limits, and collection
diagnostics apply to each image. No JavaScript executes and no media is fetched
from the final render.

Each source occurrence has a PostgreSQL metadata row keyed by source document,
locator, selected URL or embedded-image hash, source capture hash, and the
versioned policy SHA-256. Image bytes use the existing content-addressed blob
store, so identical bytes share one blob while retaining separate occurrence
rows and collection provenance. The baseline schema is built from ORM metadata.

The initial policy excludes images under navigation/header/footer/aside
landmarks; boilerplate logo, menu, banner, social, icon, tracking, and similar
URL/class/alt patterns; tracking-sized images; dimensions below 160 x 100; and
files below 2 KiB or above 5 MiB. Exact SHA-256 duplicates and PNG difference
hashes within four bits are excluded. Accepted candidates are marked
`ACCEPTED_FOR_REVIEW`; the existing editorial figure decision contract remains
the consumer boundary.

The backend has `pypdf` for embedded PDF image extraction but no rasterizer.
Embedded image bytes are archived directly. Pages containing embedded images
also receive `PAGE_EXCERPT_NEEDED` metadata with the document ID, page number,
and page bounding box; no crop bytes are fabricated or rendered.
