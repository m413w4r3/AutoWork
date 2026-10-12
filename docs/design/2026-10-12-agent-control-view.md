# Vue de contrôle de production pour agents

Cette projection `agent-control.v1` rassemble en lecture seule l’état utile au
pilotage. Elle expose des identités et des liens vers les détails existants,
jamais les corps d’artefacts, documents, PDF, textes de source ou sorties de
modèle.

## Requêtes

```http
GET /api/editions/10000000-0000-4000-8000-000000000001/production/control-view?limit=1
```

```http
GET /api/production/runs/30000000-0000-4000-8000-000000000001/control-view
```

La première route accepte `cursor` et `limit` (1 à 25, défaut 10). La seconde
lit un run par son identifiant, même si une génération plus récente est
courante. Voici la réponse exacte capturée par le test d’intégration PostgreSQL
pour la première requête (la date `as_of` est celle de cette capture) :

```json
{
  "schema_version": "agent-control.v1",
  "as_of": "2026-10-12T09:04:26.502527Z",
  "scope": {
    "kind": "edition",
    "edition_id": "10000000-0000-4000-8000-000000000001",
    "run_id": null
  },
  "edition": {
    "id": "10000000-0000-4000-8000-000000000001",
    "state": "open",
    "version": 1,
    "period_start": "2026-10-01",
    "period_end": "2026-10-31",
    "country_code": "CT",
    "tlp": "GREEN"
  },
  "selection": {
    "snapshot_id": null,
    "snapshot_version": null,
    "selected_count": 10,
    "href": "/api/editions/10000000-0000-4000-8000-000000000001/selection"
  },
  "batch": {
    "active": {
      "batch_id": "40000000-0000-4000-8000-000000000001",
      "status": "paused",
      "phase": "initial",
      "paused_at": "2026-10-12T09:30:00Z",
      "href": "/api/editions/10000000-0000-4000-8000-000000000001/production"
    },
    "recent": [],
    "href": "/api/editions/10000000-0000-4000-8000-000000000001/production"
  },
  "subjects": {
    "total": 10,
    "items": [
      {
        "subject_id": "20000000-0000-4000-8000-000000000001",
        "canonical_identity": "20000000-0000-4000-8000-000000000001",
        "title": "Control subject 1",
        "tlp": "GREEN",
        "current_run": true,
        "run": {
          "run_id": "30000000-0000-4000-8000-000000000002",
          "run_number": 2,
          "pipeline_generation": 2,
          "status": "ready",
          "current_stage": "assembly",
          "error": {
            "code": null,
            "details_ref": "/api/production/runs/30000000-0000-4000-8000-000000000002"
          },
          "stages": [
            {"stage": "sources", "status": "succeeded", "artifact": null},
            {
              "stage": "references",
              "status": "succeeded",
              "artifact": {
                "id": "00604be7-3de4-838a-d9a5-cf8800000007",
                "version": 1,
                "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf8800000007",
                "status": "verified",
                "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/references"
              }
            },
            {
              "stage": "extraction",
              "status": "succeeded",
              "artifact": {
                "id": "00604be7-3de4-838a-d9a5-cf8800000008",
                "version": 1,
                "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf8800000008",
                "status": "verified",
                "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/extraction"
              }
            },
            {
              "stage": "relevance_projection",
              "status": "succeeded",
              "artifact": {
                "id": "00604be7-3de4-838a-d9a5-cf8800000009",
                "version": 1,
                "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf8800000009",
                "status": "verified",
                "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/relevance_projection"
              }
            },
            {
              "stage": "synthesis",
              "status": "succeeded",
              "artifact": {
                "id": "00604be7-3de4-838a-d9a5-cf880000000a",
                "version": 1,
                "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf880000000a",
                "status": "verified",
                "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/synthesis"
              }
            },
            {
              "stage": "editorial_enrichment",
              "status": "succeeded",
              "artifact": {
                "id": "00604be7-3de4-838a-d9a5-cf880000000b",
                "version": 1,
                "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf880000000b",
                "status": "verified",
                "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/editorial_enrichment"
              }
            },
            {
              "stage": "assembly",
              "status": "succeeded",
              "artifact": {
                "id": "00604be7-3de4-838a-d9a5-cf880000000c",
                "version": 1,
                "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf880000000c",
                "status": "verified",
                "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/publication"
              }
            }
          ],
          "revision_debt": {
            "stale_enrichment_blocks": null,
            "latest_revision_artifact_id": "00604be7-3de4-838a-d9a5-cf880000000b",
            "lineage_href": "/api/production/artifacts/00604be7-3de4-838a-d9a5-cf880000000b/revision-lineage"
          }
        },
        "jobs": {
          "active": 0,
          "failed": 0,
          "waiting": 0,
          "total": 0,
          "items": [],
          "href": "/api/jobs?aggregate_type=subject&aggregate_id=20000000-0000-4000-8000-000000000001"
        },
        "sources": {
          "total": 0,
          "archived": 0,
          "eligible": 0,
          "critical_unresolved": 0,
          "critical_unresolved_items": [],
          "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/artifacts/references"
        },
        "repair_debt": {
          "unresolved": 0,
          "blocking": 0,
          "pending_rebuild": 0,
          "items": [],
          "href": "/api/subjects/20000000-0000-4000-8000-000000000001/production/repairs"
        },
        "preview": {
          "state": "unknown",
          "artifact_id": "00604be7-3de4-838a-d9a5-cf880000000c",
          "artifact_version": 1,
          "input_hash": "0000000000000000000000000000000000604be73de4838ad9a5cf880000000c",
          "render_identity": null,
          "render_id": null,
          "href": "/api/subjects/20000000-0000-4000-8000-000000000001/publication/preview?artifact_id=00604be7-3de4-838a-d9a5-cf880000000c"
        },
        "actions": []
      }
    ],
    "next_cursor": "djE6MjAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAx"
  },
  "actions": [
    {
      "id": "resume_batch",
      "method": "POST",
      "href": "/api/editions/10000000-0000-4000-8000-000000000001/production/40000000-0000-4000-8000-000000000001/resume",
      "permitted": "unknown",
      "reason": "backend_rechecks_fences",
      "requires_human_decision": false
    }
  ],
  "review": {
    "can_accept": false,
    "blocking_subjects": 1,
    "unresolved_repairs": 1,
    "pending_rebuild": 1,
    "href": "/api/editions/10000000-0000-4000-8000-000000000001/review"
  },
  "release": {
    "manifest_id": null,
    "manifest_sha256": null,
    "release_id": null,
    "render_status": "none",
    "pdf_available": false,
    "ioc_export_href": "/api/editions/10000000-0000-4000-8000-000000000001/release/iocs.zip"
  }
}
```

## Permissions et limites

- La vue ne reçoit ni ne transporte d’identité d’acteur et ne confère aucun
  droit. Elle ne change pas le contexte d’identité de l’API. Chaque route de
  mutation conserve ses vérifications d’identité, règles, fences et décisions
  humaines ; la projection ne donne aucun moyen de les contourner. Le
  `LocalIdentityProvider` actuellement câblé par l’application est une
  identité de développement, pas un mécanisme d’authentification de production.
- `actions` décrit seulement les opérations applicables à l’état observé.
  `permitted: "unknown"` signifie que le projecteur ne peut pas statuer
  sûrement sans exécuter les vérifications de la commande. La réponse n’est
  jamais une autorisation d’exécuter l’action.
- Le résultat est un instantané avec `as_of`. Une course avec une mutation
  ultérieure reste possible ; les endpoints de commande arbitrent avec leurs
  fences.
- Les sujets sont paginés par curseur opaque et clé stable. Les jobs et dettes
  de réparation sont résumés et limités à 20 éléments par sujet.
- Les blobs MinIO ne sont pas lus pour calculer un hash canonique. Un hash
  canonique non persisté, les additions de sources en attente et les compteurs
  non calculables depuis les lectures persistées ne sont pas inventés.
- Cette version projette les réparations connues, l’aperçu de publication et
  l’état du release ; elle n’expose ni les contenus ni l’historique complet.
