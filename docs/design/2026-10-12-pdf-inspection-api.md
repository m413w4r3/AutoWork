# PDF inspection API (`pdf-inspection-v1`)

This read-only API resolves a PDF by the SHA-256 of its bytes. It does not
accept a client path, compile a render, call a model, or persist inspection
results as canonical data.

## Requests

```http
GET /api/pdf-inspections/{pdf_sha256}
GET /api/pdf-inspections/{pdf_sha256}/pages/{page}.png?profile=preview
GET /api/pdf-inspections/{pdf_sha256}/pages/{page}/thumbnail.png
```

The hash must be 64 lowercase hexadecimal characters. PDF bytes can come from
the verified blob for a successful persisted `publication_render` or
`edition_render` whose `output_sha256` matches, or from the short lived cache
populated by the existing edition-preview PDF route. The blob's byte count and
SHA-256 are checked before Poppler reads it. Every matching persisted render is
returned, so one PDF hash can have multiple provenance records.

The inspection routes use the same API access boundary as the corresponding
existing PDF delivery routes. They add no user or TLP authorization model.
Inspection endpoints are read-only. The edition preview PDF route retains its
existing compile and stale-preview behavior; it additionally returns
`X-PDF-SHA256` and seeds the temporary inspection cache.

## Metadata response

The following response was captured from the API route test using the fixed
test PDF and IDs. The test processor deliberately supplies one parser warning,
one font diagnostic, and a blank-page heuristic so that the wire schema is
visible in the example.

```http
GET /api/pdf-inspections/5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b
```

```json
{
  "schema_version": "pdf-inspection-v1",
  "pdf": {
    "sha256": "5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b",
    "byte_size": 25,
    "page_count": 2,
    "page_size_points": [595.276, 841.89],
    "encrypted": false,
    "parser_warnings": [
      "Syntax Error: Suspects object is wrong type (boolean)"
    ]
  },
  "availability": "ready",
  "render": [
    {
      "id": "00000000-0000-0000-0000-000000000001",
      "status": "succeeded",
      "input_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "renderer_version": "typst-publication-test",
      "template_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "compiler_version": "0.15.1",
      "font_bundle_version": "fonts-v1"
    }
  ],
  "source": [
    {
      "kind": "subject_publication",
      "render_id": "00000000-0000-0000-0000-000000000001",
      "subject_id": "00000000-0000-0000-0000-000000000010",
      "artifact_id": "00000000-0000-0000-0000-000000000011",
      "artifact_version": 3,
      "artifact_input_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "edition_id": null,
      "release_id": null,
      "manifest_id": null,
      "manifest_sha256": null,
      "edition_document_sha256": null,
      "preview_input_hash": null,
      "artifacts": []
    }
  ],
  "raster": {
    "status": "available",
    "rasterizer": "poppler",
    "rasterizer_version": "poppler-24.02.0",
    "profiles": {
      "preview": {"dpi": 120, "max_edge_px": 1600},
      "thumbnail": {"max_edge_px": 480}
    },
    "page_png_url_template": "/api/pdf-inspections/5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b/pages/{page}.png?profile=preview",
    "thumbnail_url_template": "/api/pdf-inspections/5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b/pages/{page}/thumbnail.png"
  },
  "diagnostics": {
    "status": "ready",
        "engine_version": "pdf-diagnostics-v2",
    "counts": {"deterministic": 2, "heuristic": 1},
    "items": [
      {
        "code": "pdf_integrity_verified",
        "class": "deterministic",
        "page": null,
        "measurement": {
          "sha256": "5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b",
          "byte_size": 25
        },
        "target": null
      },
      {
        "code": "font_not_embedded",
        "class": "deterministic",
        "page": null,
        "measurement": {"fonts": ["MissingFont"]},
        "target": null
      },
      {
        "code": "page_blank",
        "class": "heuristic",
        "page": 2,
        "measurement": {
          "text_word_count": 0,
          "threshold": 0,
          "basis": "pdftotext bbox word count; image-only pages may be false positives"
        },
        "target": null
      }
    ]
  },
  "mapping": {
    "status": "not_available",
    "reason": "location_sidecar_unavailable",
    "items": [],
    "unmapped": []
  }
}
```

`mapping` is a projection of render metadata when that render produced a
location sidecar. Missing sidecars leave it unavailable; they do not trigger a
new render. The deterministic/heuristic class is attached to each diagnostic.
The server emits only `deterministic` and `heuristic`; visual observations by
an agent and human decisions remain outside this API contract.
Blank-page and low-fill measurements use text boxes returned by
`pdftotext -bbox-layout`; image-only pages can therefore look blank, and
overlapping boxes may inflate the fill ratio. These are signals, not layout
judgments.

## Canonical block mapping

The renderers instrument the article title and enrichment tables, diagrams,
charts, and source figures. The title's `block_key` is the named canonical
`title` field. An enrichment block is instrumented only when its canonical
element has an explicit, unique non-empty `key`. Title targets carry the
SYNTHESIS artifact identity and `rewrite_title` channel; keyed enrichment
targets carry the EDITORIAL_ENRICHMENT artifact identity and that channel's
admitted operations. Those artifact IDs and fences are the ones accepted by
the existing correction route; PUBLICATION is only the rendered container.
The target identity is the SHA-256 of canonical JSON
containing `subject_id`, `artifact_id`, `artifact_version`,
`artifact_input_hash`, `block_type`, and `block_key`. It is independent of
page, order, content similarity, or coordinates. A reflow of the same artifact
therefore preserves `target_id`; a new canonical artifact version gets a new
target identity even when an unaffected block retains the same key and
canonical content.

`revision_channel` is included where the existing targeted enrichment route
supports the element. The server reports admitted operations; it does not add
a mutation route. Paragraphs, section headings, and individual table cells do
not have stable identity and are returned only as aggregate `unmapped` reasons.
Table/diagram/chart/figure elements without a valid key likewise are not
mapped.

This exact response shape is used by the mapping contract test. Its page and
bbox values are fixed test measurements; the IDs are deterministic from the
identity values shown:

```json
{
  "mapping": {
    "status": "partial",
    "reason": null,
    "items": [
      {
        "target_id": "f573e1d744ee61318da73b4e36c9f05d1ae93a9ad85c7db655e526bcd67f86f1",
        "target": {
          "subject_id": "00000000-0000-0000-0000-000000000010",
          "artifact_id": "00000000-0000-0000-0000-000000000011",
          "artifact_version": 3,
          "artifact_input_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
          "block_type": "diagram",
          "block_key": "overview",
          "revision_channel": {
            "family": "editorial_enrichment",
            "operations": ["rewrite_diagram", "delete_diagram"]
          }
        },
        "locations": [
          {
            "page": 1,
            "bbox_points": {"x": 62.36, "y": 180.0, "width": 470.55, "height": 50.0}
          }
        ],
        "mapping_quality": "canonical_block_key"
      }
    ],
    "unmapped": [
      {"block_type": "paragraph", "reason": "no_stable_identity", "count": 1}
    ]
  }
}
```

The Typst helper emits zero-layout `metadata` records before and after each
instrumented block. The opening record measures the block in its normal
rendering width; start and end page positions delimit blocks that flow across
pages. After the one authoritative PDF compile, `typst query` reads those
records from the same private workspace, template snapshot, render data,
resolved media, and font snapshot. It does not write or compile a second PDF.
The query bytes are held only in the existing ephemeral derived cache, keyed by
PDF SHA-256, mapping version, output renderer version, and render ID. The API
checks the cached PDF hash and provenance against the requested PDF before
projecting any target; a provenance mismatch is `unverified`.

The layout-only instrumentation was prototyped on the real two-page Typst
inspection fixture. The baseline and instrumented PDFs were both 12,086 bytes
with SHA-256
`76d23b6560270015d41f3c36402091902aac0890cdc7d0cd04f102875339e600`; both
rasterized pages compared byte-for-byte equal. The metadata query returned
start and end coordinates for the same page. Renderer and template versions
were bumped for publication and edition rendering; canonical artifact
versions, synthesis/enrichment inputs, and model calls do not depend on these
render-only versions.

Diagnostics `pdf-diagnostics-v2` adds deterministic `block_too_small` for a
mapped diagram, chart, or source figure when its measured slot is below either
120 pt wide or 72 pt high. The measurement exposes both thresholds and carries
the canonical target. Heuristic page occupancy/blank-page observations remain
separate and targetless.

## PNG response

The following headers were captured from the same route test:

```http
GET /api/pdf-inspections/5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b/pages/1.png?profile=preview
HTTP/1.1 200 OK
content-type: image/png
etag: "4b957ce1f15c52f83ad017e87534ad3d418f1c15ce58f94809f202311e0a5b6a"
x-pdf-sha256: 5e044a816962ffa67f7037725aadb2cbf274fc1f9abee599034730bdb6d3f31b
x-pdf-page: 1
x-pdf-profile: preview
x-content-type-options: nosniff
content-length: 5035
```

`preview` is fixed to 120 dpi with a 1600 px maximum edge. The thumbnail route
is fixed to a 480 px maximum edge. The profile query parameter is retained on
the preview route for explicitness; other values are rejected by request
validation. Rasterization runs one page at a time with a 15 second process
timeout and 512 MiB address-space limit, behind a process-wide two-request
semaphore. PDFs larger than 25 MiB, over 200 pages, or with extreme page
dimensions are refused before rasterization.

## Stable error codes

The body is `{"detail":{"code":"<code>"}}`.

| HTTP | Code | Meaning |
| --- | --- | --- |
| 404 | `absent` | No verified persisted PDF or cached preview has this hash. |
| 410 | `preview_cache_expired` | The hash was seen as a preview, but its temporary entry expired. |
| 413 | `limit_exceeded` | PDF size, page count, or page dimensions exceed limits. |
| 416 | `page_out_of_bounds` | Requested page is outside the PDF page count. |
| 422 | `hash_invalid` | The path component is not a lowercase SHA-256. |
| 422 | `output_corrupt` | Registered bytes fail their size/hash or parser/raster checks. |

## Cache and identities

Only derived files are cached, under the configured temporary storage root:

- Raster cache: atomic writes, LRU up to 512 MiB, 24 hour TTL.
- Edition preview PDF cache: atomic writes, up to 100 MiB total, 10 minute TTL.
- Raster key: `pdf_sha256 + rasterizer_build + profile + page + inspection_policy_version`.

Neither cache is canonical state; neither is written to PostgreSQL or MinIO.
The response names separate identities explicitly: the PDF byte SHA-256,
render `input_hash`, render ID/output blob, and edition manifest hash. None are
substituted for another.

## Known limits

- `pdftotext` provides text-box occupancy, not visual pixel coverage; figures,
  intentional whitespace, and image-only pages need human interpretation.
- `pdfinfo` reports the first page dimensions in metadata. Each requested page
  is checked individually before it is rasterized.
- Cache data may be removed on process restart or host cleanup. Expired preview
  PDFs cannot be reconstructed by this API; call the existing preview PDF route
  again with its current preview input hash.
- Mapping sidecars share the derived-cache quota and 24 hour TTL. If one is
  evicted, inspection returns `location_sidecar_unavailable`; an existing
  render path must produce the sidecar again. Inspection does not recompile.
- Poppler version is part of the cache key and returned metadata. Pixel output
  is tested for repeatability with the same local build; different Poppler
  builds can produce different pixels.
- A mapped bbox is a Typst layout slot in points, not the exact bounds of
  painted pixels. Multi-page slots use Typst start/end pages and the current
  A4 text-area margins for intermediate fragments; captions, whitespace, and
  overlapping material can make the box larger than visible ink. This lot does
  not map paragraphs, headings, or individual table cells, and it does not
  claim OCR, word-level, or pixel-level localization. Blocks with no stable
  canonical key and legacy renders without a sidecar remain unmapped.
