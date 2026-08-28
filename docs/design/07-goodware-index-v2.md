# Goodware index v2

## Decision

The large yarGen measurement set is an offline artifact, not a PostgreSQL row
set. PostgreSQL remains the future home of baseline identity, source
provenance, investigation binding, and artifact metadata. Source databases are
immutable MinIO blobs. Runtime lookup will use a local read-only SQLite copy
materialized from MinIO.

This document covers only the offline artifact and its normalization contract.
The P09/P10 application and storage integration, migrations, and Compose are
deliberately out of scope.

## Versions

The operator implementation in `scripts/import_goodware.py` freezes these
values:

| Constant | Value |
| --- | --- |
| `SCHEMA_VERSION` | `autowork-goodware-index-v2` |
| `NORMALIZATION_VERSION` | `autowork-goodware-normalization-v2` |
| `KEY_VERSION` | `autowork-goodware-key-v1` |
| `INDEX_FORMAT_VERSION` | `autowork-goodware-index-v2` |
| `SOURCE_FORMAT` | `yargen-gzip-json-counter-v1` |
| non-discriminant pattern version | `non-discriminant-patterns-v1` |

The source format accepts the gzip JSON counter files produced by yarGen and
plain JSON variants used by operators. v2 parses the object incrementally and
uses bounded `executemany` batches; it does not construct a Python list for a
whole shard. The default decompressed-source bound is 8 GiB and can be
lowered with `--max-decompressed-bytes`.

## Normalization

All four supported feature kinds are exactly `string`, `opcode_fragment16`,
`imphash`, and `export`.

- `string`: require a string, reverse only yarGen's `\\` and `\"` escapes,
  remove an exact leading `UTF16LE:` transport marker after decoding, then
  apply Python `str.lower()`. The result must be non-empty. No NFC or other
  Unicode normalization is applied.
- `export`: apply Python `str.lower()` and require a non-empty result.
- `imphash`: skip only the exact raw empty string `""`, the official yarGen
  unavailable-imphash sentinel. Every other value is stripped, lowercased, and
  required to be exactly 32 hexadecimal characters. Whitespace-only and other
  malformed non-empty values are errors.
- `opcode_fragment16`: remove whitespace, lowercase, and require the existing
  exact 8..16-byte hexadecimal validation.

Consequently, ASCII and exact-marker `UTF16LE:` strings that produce the same
lowercase value are one feature and their occurrence counters are aggregated.

## Lookup key

For an already-normalized value, the key is the 32-byte digest:

```python
sha256(
    b"autowork-goodware-key-v1\\0"
    + feature_kind.encode("ascii")
    + b"\\0"
    + normalized_value.encode("utf-8")
).digest()
```

The script has golden vectors for all four feature kinds. Runtime code must
duplicate those vectors when the application integration is implemented; it
must not import the operator script.

## Artifact

`build-index INPUT_DIR OUTPUT_DIR` writes:

- `goodware-index.sqlite3`
- canonical UTF-8 `manifest.json`

The `features` table is exactly:

```sql
CREATE TABLE features (
    feature_key BLOB PRIMARY KEY CHECK(length(feature_key) = 32),
    occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0)
) WITHOUT ROWID;
```

The only additional object is a small `metadata` table used for fail-closed
format and count validation. No normalized values or per-feature UUIDs are
stored, and no secondary index is created over `features`.

The manifest contains `schema_version`, `source_format`,
`source_set_sha256`, `normalization_version`, `key_version`,
`index_format_version`, `baseline_fingerprint_sha256`, `record_count`,
`occurrence_sum`, `index_sha256`, `index_size`, and `sources`.

`baseline_fingerprint_sha256` hashes canonical JSON containing exactly
`source_set_sha256`, `normalization_version`, and `pattern_version` (the
current non-discriminant pattern version). It intentionally excludes the
physical SQLite hash. `index_sha256` and `index_size` provide physical
artifact integrity separately.

## Build and verification

The v2 operator path rebuilds directly from the immutable source DBs:

```sh
uv run python scripts/import_goodware.py build-index SOURCES ARTIFACT
uv run python scripts/import_goodware.py verify-index ARTIFACT --source-dir SOURCES
uv run python scripts/import_goodware.py deep-verify-index ARTIFACT --source-dir SOURCES
```

The first verification is the routine structural/integrity check. It validates
the canonical manifest, source set when supplied, artifact hash and size,
SQLite schema and object set, metadata, feature key/count types, and aggregate
counts without a Python pass over all feature rows. `deep-verify-index` adds
SQLite's full `integrity_check`.

The completed SQLite file is reopened through SQLite's read-only URI and is
made filesystem read-only before promotion. Build output is prepared in a
temporary sibling location and verified before promotion.

The old `build`/`verify` commands remain the supported v1 JSONL stage path.
Direct rebuilding from source DBs is the transition path: it avoids converting
the large v1 `records.jsonl` representation and produces the final hashed-key
artifact in one offline run. The v1 code is retained until the application
integration commit is tested.

## Remaining integration work

The next storage/application commit must add artifact metadata to the
canonical PostgreSQL model, ingest source and SQLite artifact blobs into
MinIO, bind the baseline to investigations, materialize a verified local
read-only SQLite copy, and duplicate the normalization/key golden vectors in
runtime code. It must not reintroduce the large measurement set as PostgreSQL
rows.
