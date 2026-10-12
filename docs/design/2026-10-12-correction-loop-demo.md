# PDF inspection correction-loop proof (Phase 4.4)

`backend/tests/integration/test_agentic_correction_loop_postgres.py` exercises
the existing publication and targeted editorial-enrichment services against a
throwaway PostgreSQL database. Its pipeline models are scripted; it makes no
external model or network calls. Typst and Poppler run locally on the generated
publication PDF.

The scenario combines the S3 targeted delete path, S5 render reuse, and the S4
stale-fence rejection:

1. The normal production pipeline reaches `READY` and stores one keyed diagram
   and one keyed table in canonical editorial enrichment.
2. The publication renderer compiles and queries the same workspace once. The
   inspection service verifies the requested PDF SHA-256, reports page count,
   renders and saves a before PNG, and projects the keyed blocks to page/bbox
   locations.
3. The diagram is supplied as a 30×10 SVG. `block_too_small` is deterministic,
   reports its measured dimensions and thresholds, and carries the diagram's
   canonical `block_key` and target identity. The target's artifact ID,
   version, and input hash match EDITORIAL_ENRICHMENT, the correction fence;
   the article-title target is separately owned by SYNTHESIS.
4. The test resolves that key to the existing `delete_diagram` operation and
   asks the targeted-revision service for a preview.
5. Preview reports zero model calls, preserves REFERENCES, EXTRACTION,
   RELEVANCE_PROJECTION, and SYNTHESIS, and announces publication invalidation.
6. Apply is fenced by the current enrichment artifact ID, version, input hash,
   canonical content hash, target key, and operation. The result has no model
   run; the provider-call count and persisted `ModelRun` IDs are unchanged.
7. The four upstream artifact identities remain identical. Only
   EDITORIAL_ENRICHMENT and PUBLICATION receive new artifact IDs and versions;
   their canonical payload hashes change. The diagram is removed; the table
   keeps the same key and byte-equivalent canonical content. Stage `input_hash`
   values are also recorded because they identify the inputs/fence and need
   not equal the canonical payload hash.
8. The second publication render has a new PDF SHA-256. The old SHA-256 still
   resolves to the old render, and the test saves a second page PNG alongside
   the first. The diagram diagnostic is absent; the table is mapped again at
   its newly computed page/bbox. Its target ID changes because the enrichment
   artifact version changed, while its canonical key/content remain stable.
9. A subsequent render request reuses the existing output: compiler/query
   counts stay at two (one for each distinct publication artifact), and no
   model call or `ModelRun` is added. Inspection metadata and PNG requests also
   leave render and model-run rows unchanged.
10. Replaying the old artifact fence raises the typed revision conflict and
    leaves the set of production artifact IDs unchanged.

On a successful run the test writes `correction-loop-proof.json` under
`tmp_path`, next to `before.png` and `after.png`. The JSON records the concrete
before/after artifact IDs, versions, input hashes, and canonical payload
hashes; preserved-stage identities; old/new PDF hashes and page numbers; PNG
hashes; expected and actual
provider-call deltas; model-run stability; render/query counts; diagnostic
removal; and stale-fence outcome. The temporary evidence directory is owned
by pytest and is not canonical application state.

The integration assertion is stronger than an HTTP status: it checks lineage,
hashes, target keys, canonical content, rerender reuse, old-render
resolvability, and proof images. The inspection API remains read-only and the
correction continues through the existing targeted editorial-enrichment
channel.
