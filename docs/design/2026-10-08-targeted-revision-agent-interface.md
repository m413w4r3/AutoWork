# Interface agent — révision ciblée

L’agent lit l’artifact courant via `GET /api/subjects/{subject_id}/production/artifacts/{synthesis|editorial_enrichment}`. La réponse fournit `artifact_id`, `version`, `input_hash`, `canonical_sha256`, `production_run_id` et `revision_targets`; chaque cible contient son `target` stable et les opérations permises par le backend.

Le client appelle `POST /api/subjects/{subject_id}/production/artifacts/synthesis/revisions/preview`, puis `POST /api/subjects/{subject_id}/production/artifacts/synthesis/revisions` avec la même commande. Les deux routes acceptent les cibles de synthèse et d’enrichissement. Corps minimal :

```json
{
  "base_artifact_id": "00000000-0000-4000-8000-000000000001",
  "base_version": 1,
  "base_input_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "base_canonical_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "target": {"kind": "title"},
  "operation": "rewrite_title",
  "instruction": "Corrige uniquement le titre.",
  "mode": "existing_evidence_only"
}
```

Le preview est en lecture seule et renvoie les fences de base, `target_path`, `before_hash`, les stages conservés/invalidés/rebasés, les appels modèle, `source_extension`, les bloqueurs et un éventuel refus typé. Pour apply, reprendre exactement `base_artifact_id`, `base_version`, `base_input_hash`, `base_canonical_sha256` et `before_hash`; ajouter `source_additions_hash` si le preview en renvoie un. L’identité de requête est déterministe : rejouer la même commande d’application retourne le même résultat.

Pour une extension de sources, enregistrer puis sélectionner l’URL opérateur avec `POST .../source-additions/candidates` (`production_run_id`, `request_id`, `url`, `origin: "operator_url"`) et `POST .../source-additions/select` (mêmes IDs/URL, `criterion`), puis archiver/extracter avec `POST .../source-additions/acquire` (mêmes IDs/URL). Réutiliser un UUID `request_id` pour ces trois appels et le transmettre comme `source_addition_request_id` au preview/apply. `acquire` expose `manual_content_base64` pour l’archive fournie par l’opérateur après blocage d’accès. Un bloqueur critique ou une addition non `READY` doit arrêter le parcours.

```json
{"base_artifact_id":"00000000-0000-4000-8000-000000000001","base_version":1,"base_input_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","base_canonical_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","target":{"kind":"sections"},"operation":"insert_section","instruction":"Ajoute une section sur NanoDump.","mode":"allow_source_extension","source_addition_request_id":"00000000-0000-4000-8000-000000000002"}
```

```json
{"base_artifact_id":"00000000-0000-4000-8000-000000000003","base_version":2,"base_input_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","base_canonical_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","target":{"kind":"enrichment_element","element_kind":"table","element_key":"tools_table"},"operation":"delete_table","instruction":"Supprime ce tableau.","mode":"existing_evidence_only"}
```

```json
{"base_artifact_id":"00000000-0000-4000-8000-000000000003","base_version":2,"base_input_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","base_canonical_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","target":{"kind":"enrichment_element","element_kind":"diagram","placement":{"kind":"after_lead"}},"operation":"create_diagram","instruction":"Crée un diagramme pour ce pipeline.","mode":"existing_evidence_only"}
```

Le résultat contient l’artifact/version créés ou `refusal`. Lire son historique sans écriture avec `GET /api/production/artifacts/{artifact_id}/revision-lineage` (`revisions`, `paragraph_provenance`, `element_provenance`). Les refus métier renvoient `refusal_reason` (cause stable), `recommended_restart_stage` (nullable) et `next_safe_action`. Une sortie modèle invalide ou refusée ne recommande pas un stage amont : l’agent reformule la consigne ou apporte des preuves. EXTRACTION/REFERENCES ne sont recommandées que pour un défaut amont établi.

Exemple exact de requête refusée après validation du patch modèle (HTTP 200, aucun artifact créé) :

```json
{
  "base_artifact_id": "00000000-0000-4000-8000-000000000001",
  "base_version": 1,
  "base_input_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "base_canonical_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "target": {"kind": "title"},
  "operation": "rewrite_title",
  "instruction": "Corrige uniquement le titre.",
  "mode": "existing_evidence_only"
}
```

```json
{
  "request_identity": null,
  "artifact_id": null,
  "artifact_version": null,
  "artifact_input_hash": null,
  "artifact_canonical_sha256": null,
  "publication_artifact_id": null,
  "revision": null,
  "refusal": {
    "code": "revision_not_applicable",
    "recommended_restart_stage": null,
    "refusal_reason": "model_declined",
    "reason": "The admitted evidence does not establish a useful revision for this target.",
    "next_safe_action": "Reformulate the instruction or add supporting evidence via source extension."
  }
}
```

Les erreurs de transport sont des HTTP errors avec un `detail` typé. Seul l’état PostgreSQL `not_submitted` autorise le retry (503); la même identité déterministe réutilise le même `ModelRun`. Un état ambigu reste en réconciliation (409) et ne doit jamais être soumis à nouveau :

```json
{
  "detail": {
    "code": "targeted_revision_transport_not_submitted",
    "request_identity": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "model_run_id": "00000000-0000-4000-8000-000000000010",
    "retryable": true,
    "submission_state": "not_submitted",
    "next_safe_action": "Replay the same revision request with the same idempotency identity."
  }
}
```

```json
{
  "detail": {
    "code": "targeted_revision_reconciliation_required",
    "request_identity": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "model_run_id": "00000000-0000-4000-8000-000000000011",
    "retryable": false,
    "submission_state": "external_state_unknown",
    "next_safe_action": "Reconcile this model run before any resubmission."
  }
}
```

Un `409` reste aussi un conflit de fences; `422` couvre une cible/opération incompatible ou un bloqueur de requête. Les valeurs des exemples d’artifact restent des gabarits : reprendre les fences et cibles réellement lues/renvoyées par l’API.
