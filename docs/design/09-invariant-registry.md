# M3 / P09 — invariant registry contract

Status: locked pre-implementation contract. P09 consumes this file; it does not redesign it.

## Scope

P09 turns already-persisted M2 analysis outputs into an investigation-scoped registry of deterministic candidate invariants. It does not rerun static analysis, capa, SMDA, VirusTotal, a model, or a worker. It adds no endpoint.

The exact public feature taxonomy is:

- `string`
- `import`
- `export`
- `section`
- `imphash`
- `ssdeep`
- `tlsh`
- `rich_header_hash`
- `icon_hash`
- `vhash`
- `capability`
- `function_hash`
- `code_ngram`

`function_hash` is reserved by the M3 taxonomy but M2 currently produces no function hash. P09 must not synthesize one. `icon_hash` is the M3 name for the current `Sample.main_icon_dhash` value. `opcode_fragment16` is a goodware lookup source/helper, not a public M3 candidate type.

## Canonical sources

Use PostgreSQL only. Do not open sample binaries or feature blobs in P09.

- `sample_feature_index` supplies `string`, `import`, `export`, `section`, `imphash`, `capability`, and `code_ngram` values and support counts. Its three source-id columns identify the originating static/capa/code feature set.
- `samples` supplies scalar similarity values `ssdeep`, `tlsh`, `rich_header_hash`, `vhash`, and `main_icon_dhash` together with their LOCAL/VT source marker when present.
- The JSONB payloads already stored on `sample_feature_sets`, `capability_sets`, and `code_feature_sets` supply source locations and tool/version provenance. They may be queried in PostgreSQL; do not fetch MinIO blobs.
- Static string locations come from the stored string `offset`; section locations from RVA/address metadata when present; capa locations from `function_addresses`; code-ngram locations from `function_offset` and `start_offset`. Imports/exports/imphash/similarity hashes may legitimately have no byte offset; record an explicit location kind such as `sample` rather than inventing an offset.

Candidate generation is limited to samples explicitly belonging to the caller-provided investigation sample set. Never scan every sample globally to discover investigation membership.

## Identity and normalization

M3 never invents a second normalization algorithm. Use the canonical normalized values already persisted by M2. Scalar similarity values are trimmed and lower-cased only where M2 already treats the value case-insensitively.

`stable_id` is a lowercase SHA-256 hex string over the UTF-8 bytes of:

`autowork-invariant-v1\n<feature_type>\n<normalized_value>`

The database uniqueness boundary is `(investigation_id, stable_id)`. The stable id is intentionally feature-global while status/support/provenance are investigation-scoped. A replay of the same investigation input must resolve to the same row and stable id.

## CandidateInvariant contract

A candidate exposes at minimum:

- `investigation_id`
- `stable_id`
- `feature_type`
- `normalized_value`
- `sample_support`: distinct investigation samples containing the value
- `family_support`: deterministic sorted family/count mapping from eligible ReferenceCorpus malware members
- `goodware_frequency`: integer when measurable, otherwise `None`; `None` never means zero
- `reference_corpus_frequency`: distinct eligible malware samples containing the value when measurable, otherwise `None`
- `benign_reference_frequency`: distinct eligible benign reference samples containing the value when measurable, otherwise `None`
- `occurrence_locations`: persisted provenance occurrences, not a model-generated summary
- `confidence`: `float | None`; if non-null it must be deterministic and in `[0, 1]`. P09 must not invent opaque weights merely to fill this field.
- `status`

Statuses are exactly:

- `CANDIDATE`
- `REJECTED_BANAL`
- `REJECTED_MULTI_FAMILY`
- `REJECTED_INSUFFICIENT_SUPPORT`
- `ACCEPTED`

P09 never sets `ACCEPTED` automatically. Acceptance remains an explicit later analyst/application decision.

## Deterministic filtering

Apply deterministic filtering before any model involvement, in this order:

1. malformed/unresolvable source provenance: do not create a candidate; append a `MALFORMED` rejection journal entry;
2. measured goodware verdict `BANAL`: `REJECTED_BANAL`;
3. ReferenceCorpus verdict `MULTI_FAMILY`: `REJECTED_MULTI_FAMILY`;
4. ReferenceCorpus verdict `CORPUS_TOO_SMALL`: `REJECTED_INSUFFICIENT_SUPPORT`;
5. otherwise: `CANDIDATE`.

A `SUSPICIOUS_COMMON` goodware result is evidence, not an automatic banality rejection. Goodware `UNKNOWN` is unknown, not zero.

Goodware measurement is valid only for mappings already supported by M2:

- `string` -> goodware `string`
- `export` -> goodware `export`
- `imphash` -> goodware `imphash`
- `code_ngram` -> only the M2 exact fixed-byte 8–16-byte `opcode_fragment16` lookup rule; masked/out-of-range ngrams remain unmeasured/unknown

Other M3 types have `goodware_frequency=None` unless a later version adds an explicit measured mapping. Do not project a generic zero.

ReferenceCorpus measurement uses exact values and distinct sample counts. Existing `sample_feature_index` supports `string`, `import`, `export`, `section`, `imphash`, `capability`, and `code_ngram`. For scalar similarity fields not present in that index, P09 may add bounded repository queries joining eligible ReferenceCorpus members to `samples`; it must not rewrite M2 indexing.

## Provenance

Each occurrence is durable and inspectable. Record at least:

- origin `sample_id`;
- source kind (`static`, `sample_similarity`, `capa`, `smda_code`);
- source row id when one exists;
- analysis tool name and version when applicable;
- source `blob_id` / feature blob id reference when applicable;
- concrete locations available from the persisted payload;
- source marker such as LOCAL/VT for scalar similarity values;
- parameters/ruleset/compatibility hashes needed to identify the exact M2 analysis run.

No candidate may be sent to a model unless it has at least one valid persisted occurrence. Model text is never provenance.

## Persistence

P09 creates migration `0011_invariant_registry.py` with `down_revision = "0010_code_features"` and never edits migrations 0001–0010.

Recommended minimal tables are `candidate_invariants`, `candidate_invariant_occurrences`, and `invariant_rejections`; exact SQLAlchemy decomposition may vary only if the externally visible contract above is preserved.

Required database guarantees:

- unique `(investigation_id, stable_id)`;
- indexed search by investigation, feature type, status, and stable id;
- occurrence replay uniqueness backed by a canonical database key, not a Python pre-check;
- rejection replay uniqueness backed by a deterministic rejection key;
- rejection rows are append-only/inspectable;
- if an occurrence table contains a real `blob_id` foreign key, add it to `BlobRepository.count_references`.

No transaction remains open while external I/O occurs; P09 should perform no external I/O at all.

## P09 tests to lock

Tests must cover: stable id replay, each deterministic rejection class, `SUSPICIOUS_COMMON` not auto-rejected, unknown goodware not treated as zero, multi-family precedence after banality, complete source provenance for static/capa/code/scalar similarity examples, reserved `function_hash` not fabricated, exact search filters, database-backed replay, and rejection journal inspection.
