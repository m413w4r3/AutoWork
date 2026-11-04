# Mission 14 annex export

Run from `backend/` against canonical PostgreSQL and MinIO state:

```sh
PYTHONPATH=src python scripts/export_investigation_annex.py INVESTIGATION_UUID /absolute/output/path \
  --profile m14_research_permissive --include-restricted-archive --with-pdf-preview
```

`--profile` overrides `ADVISOR_DISCLOSURE_PROFILE` (`standard` by default). The
permissive qualification profile keeps policy labels in the manifest and
includes policy-restricted technical material in the explicitly requested
private archive. The public projection contains hashes and relations but never
sample or PCAP bytes. Standard keeps the existing disclosure masks.

`--include-restricted-archive` writes verified private blobs under `restricted/`
and creates `technical-archive.tar.gz` plus its `.sha256` file. Without it, no
private blobs are copied. `--overwrite` atomically replaces an existing output;
without it, an existing destination is refused. The PDF preview is attempted by
default for the latest current draft; use `--no-with-pdf-preview` to skip it.
Unavailable previews are recorded in the manifest.

The output contains `yara/` sources and test reports, `suricata/` rules and
test-status JSON, `iocs/` CSVs, `samples/sample-inventory.csv`,
`network/captures/` metadata, `technical/`, optional `opinion/opinion-draft.pdf`,
and `manifest.json` with `manifest.sha256`. Private PCAP and sample bytes are
stored under `restricted/network/captures/` and `restricted/samples/`.
