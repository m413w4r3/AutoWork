# M14 fresh-stack bootstrap

Export the seven source documents from the old stack with `m14_export_sources.py`, then copy the manifest and SHA-256-named files to the new backend container. The seed creates an Iraq edition for the current UTC month, imports one `manual_import` discovery subject, selects it, archives only the verified local bytes, then starts and polls production.

```sh
OLD_BACKEND_CONTAINER=$(docker compose -p cti-bulletin ps -q backend)
docker exec "$OLD_BACKEND_CONTAINER" mkdir -p /app/scripts/ops /tmp/m14-sources
docker cp scripts/ops/m14_export_sources.py "$OLD_BACKEND_CONTAINER:/app/scripts/ops/m14_export_sources.py"
docker exec "$OLD_BACKEND_CONTAINER" python /app/scripts/ops/m14_export_sources.py --output-dir /tmp/m14-sources
docker cp "$OLD_BACKEND_CONTAINER:/tmp/m14-sources" /tmp/m14-sources

NEW_BACKEND_CONTAINER=$(docker compose -p cti-m14 ps -q backend)
docker exec "$NEW_BACKEND_CONTAINER" mkdir -p /app/scripts/ops /tmp/m14-sources
docker cp scripts/ops/m14_bootstrap_subject.py "$NEW_BACKEND_CONTAINER:/app/scripts/ops/m14_bootstrap_subject.py"
docker cp /tmp/m14-sources "$NEW_BACKEND_CONTAINER:/tmp/m14-sources"
docker exec "$NEW_BACKEND_CONTAINER" python /app/scripts/ops/m14_bootstrap_subject.py --manifest /tmp/m14-sources/sources_manifest.json --dry-run
docker exec "$NEW_BACKEND_CONTAINER" python /app/scripts/ops/m14_bootstrap_subject.py --manifest /tmp/m14-sources/sources_manifest.json
# Optional: creates operator_led and prints its G2 envelope without approving it.
docker exec "$NEW_BACKEND_CONTAINER" python /app/scripts/ops/m14_bootstrap_subject.py --manifest /tmp/m14-sources/sources_manifest.json --create-investigation
```

The container reads `OPERATOR_API_TOKEN` from its environment; never pass or print it. Expect 1–3 minutes for edition/discovery/selection/source archival, then about 20–90 minutes for production, depending on model latency. Polling defaults to 5 seconds and stops on `needs_review`, reconciliation, failure or timeout for human handling. Production makes real model calls. Manual discovery import and source archival make no provider or source-network calls. `--create-investigation` invokes the investigation start route and may queue generation 1; it does not approve G2.

The edition and discovery candidate use the strictest TLP found in the manifest. The discovery API applies that aggregate TLP to all imported source candidates; stricter existing edition TLP disables external LLM use.

The manual content route requires existing collection rows. Its only initializer route, `POST /api/subjects/{id}/collection`, dispatches network collection, so the in-container script calls `SubjectCollectionService.initialize(subject_id)` directly before uploading bytes. Host mode (`--base-url`) can resume when those rows already exist; a fresh host-mode run stops at this route gap and must be run inside the backend container.

`m14_seed_subject.py` (production-state import) and `m14_create_investigation.py` (direct-database handoff) are obsolete for fresh-stack seeding. `m14_export_sources.py` remains the source-only export step.
