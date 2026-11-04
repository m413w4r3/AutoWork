# M14 fresh-stack seed

`infra/backend.Dockerfile` copies only selected operations scripts. Copy these
three scripts into each backend container before running them. Replace
`OLD_BACKEND_CONTAINER` with the old backend container ID/name.

```sh
docker exec "$OLD_BACKEND_CONTAINER" mkdir -p /app/scripts/ops /tmp/m14-sources
docker cp scripts/ops/m14_export_sources.py "$OLD_BACKEND_CONTAINER:/app/scripts/ops/m14_export_sources.py"
docker exec "$OLD_BACKEND_CONTAINER" python /app/scripts/ops/m14_export_sources.py --output-dir /tmp/m14-sources
docker cp "$OLD_BACKEND_CONTAINER:/tmp/m14-sources" /tmp/m14-sources

NEW_BACKEND_CONTAINER=$(docker compose -p cti-m14 ps -q backend)
docker exec "$NEW_BACKEND_CONTAINER" mkdir -p /app/scripts/ops /tmp/m14-seed
docker cp scripts/ops/m14_seed_subject.py "$NEW_BACKEND_CONTAINER:/app/scripts/ops/m14_seed_subject.py"
docker cp scripts/ops/m14_create_investigation.py "$NEW_BACKEND_CONTAINER:/app/scripts/ops/m14_create_investigation.py"
docker cp /tmp/m14-sources "$NEW_BACKEND_CONTAINER:/tmp/m14-sources"
docker cp /home/nill/perso/work/AutoWork-backups/m14-seed/production_state_blinder.json "$NEW_BACKEND_CONTAINER:/tmp/m14-seed/production_state_blinder.json"
docker cp /home/nill/perso/work/AutoWork/.autowork-runs/m13-q1/orch/initial_corpus_hashes.txt "$NEW_BACKEND_CONTAINER:/tmp/m14-seed/initial_corpus_hashes.txt"
docker exec "$NEW_BACKEND_CONTAINER" python /app/scripts/ops/m14_seed_subject.py --manifest /tmp/m14-sources/sources_manifest.json --production-state /tmp/m14-seed/production_state_blinder.json --initial-hashes /tmp/m14-seed/initial_corpus_hashes.txt --dry-run
# After a compatible seed and verified synthesis exist:
docker exec "$NEW_BACKEND_CONTAINER" python /app/scripts/ops/m14_create_investigation.py --subject-id "$SUBJECT_ID" --production-run-id "$PRODUCTION_RUN_ID" --synthesis-artifact-id "$SYNTHESIS_ARTIFACT_ID" --synthesis-sha256 "$SYNTHESIS_SHA256"
```

The seed command currently stops before writing. v5 import requires the old
subject ID and its active discovery lineage/input snapshot; the requested new
subject UUID and source-only manifest cannot satisfy that contract; v5 also
omits the full input snapshot needed to reconstruct it. The actual export cites
only 2/15 initial hashes in synthesis (15/15 in extraction), and
`import_state` produces `needs_review` pending assembly, not `READY`. Source
archive provenance is `manual` / `source.archived_manually`; no
`manual_import` source-document provenance exists. Resolve these issues and
source-document ID remapping before a mutating seed. No G2 envelope is approved
by `m14_create_investigation.py`; after a compatible verified synthesis exists,
it creates the operator-led handoff and prints the proposal. The later start
request must follow the authorized G2 policy decision.
