# Inspection des entrées Typst — Lot D

Date : 2026-10-12 · Branche : `chantier/multibatch-composition`

## Audit de l’outillage

| Surface existante | Avant le Lot D | Complément du Lot D |
|---|---|---|
| Rendus publication et release | PostgreSQL conservait versions, hashes de template, compilateur, PDF et références aux blobs d’entrypoint et de render-data. | Lecture par `render_id`, avec identité d’artefact ou release, manifeste, composition V2 et entrées épinglées. |
| Entrées Typst persistées | Le blob source ne contenait que l’entrypoint; les autres fichiers du bundle et les octets médias n’étaient pas conservés. | Le blob source reçoit un snapshot versionné, avec le bundle exact et les médias disponibles, SHA-256 par fichier et l’empreinte du JSON. Aucun champ DB ni migration ajouté. |
| Preview d’édition | PDF et sidecar de localisation vivaient déjà dans le cache dérivé borné, à durée de vie limitée. | Le même cache conserve les entrées effectives sous l’ID opaque annoncé dans `X-Render-Inspection-ID`. |
| Inspection PDF | Résolution du PDF par hash, nombre de pages, signaux Poppler et mapping Typst existants. | Réutilisation par hash; les mappings ne sont renvoyés que si leur `target_id` figure dans le render-data inspecté. |
| Sources absentes | Il n’existait pas de route d’inspection sans recompilation. | Les anciens blobs source restent lisibles comme entrypoint partiel. Si un snapshot manque ou a expiré, l’API renvoie `unavailable` et ne consulte pas le checkout courant. |

L’inspection ne reçoit aucun compilateur ni service modèle. La lecture PDF appelle le service d’inspection existant, qui peut lancer Poppler sur les octets PDF déjà persistés ou cachés; elle ne lance pas Typst. Aucun diagnostic de mise en page n’est déduit du texte source.

## Contrat HTTP

Toutes les routes sont en lecture seule. Les rendus persistés utilisent un UUID issu de la table de rendus; les previews utilisent l’ID SHA-256 renvoyé par l’en-tête `X-Render-Inspection-ID` du PDF de preview.

```http
GET /api/render-inspections/{render_id}
GET /api/render-inspections/{render_id}/files
GET /api/render-inspections/{render_id}/files/{file_id}?start_line=1&line_count=100
GET /api/render-inspections/{render_id}/render-data?subject_id={uuid}
GET /api/render-inspections/{render_id}/render-data?block_key={key}
GET /api/render-inspections/{render_id}/render-data?pointer=/content_sections/0
GET /api/render-inspections/{render_id}/diagnostics
```

Les variantes preview ont le préfixe `/api/render-inspections/previews/{inspection_id}` et les mêmes suffixes. `pointer` ne se combine pas avec `subject_id` ou `block_key`; `subject_id` et `block_key` peuvent être combinés. Les filtres renvoient des correspondances exactes avec leur JSON Pointer. Les blocs sans `target_id` ne sont jamais associés par titre, position ou proximité.

Les réponses ont les versions `render-input-inspection-v1`, `render-input-files-v1`, `render-input-file-v1`, `render-data-inspection-v1` et `render-input-diagnostics-v1`. Les réponses refusent les champs supplémentaires. Voici les réponses capturées avec les fixtures unitaires fixes.

### Inspection du rendu

```json
{
  "schema_version": "render-input-inspection-v1",
  "render": {
    "id": "00000000-0000-4000-8000-000000000001",
    "type": "publication_render",
    "status": "succeeded",
    "identity": {
      "kind": "subject_publication",
      "subject_id": "00000000-0000-4000-8000-000000000010",
      "artifact_id": "00000000-0000-4000-8000-000000000020",
      "artifact_version": 3,
      "artifact_input_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    },
    "input_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "error_code": null
  },
  "template": {
    "version": "publication-template-test-v1",
    "sha256": "1407176619118835ec32b256c28c9d03af164231257eec34fb6e687ab1bdd6f2",
    "snapshot_sha256": "1407176619118835ec32b256c28c9d03af164231257eec34fb6e687ab1bdd6f2",
    "verified": true
  },
  "compiler": {
    "name": "typst",
    "version": "0.15.1",
    "renderer": "typst",
    "renderer_version": "publication-renderer-test-v1",
    "font_bundle_version": "font-bundle-test-v1"
  },
  "source": {
    "availability": "ready",
    "entrypoint": {
      "file_id": "4ee806f37b3105998f9cdcb369db981b",
      "path": "RENDERER/publication.typ",
      "sha256": "c5201138f493237e7dbb04b3b0b15878d7b5e5b83f6f0d62cb5979632564c832",
      "byte_size": 36,
      "kind": "template"
    },
    "files_href": "/api/render-inspections/00000000-0000-4000-8000-000000000001/files"
  },
  "render_data": {
    "availability": "ready",
    "schema_version": "typst-publication-model-v20-block-location",
    "sha256": "fff4685ed3cb516dd8332f6fcee89cbda27ede036d866027513971eec24d3cf0",
    "href": "/api/render-inspections/00000000-0000-4000-8000-000000000001/render-data"
  },
  "hashes": {
    "template_sha256": "1407176619118835ec32b256c28c9d03af164231257eec34fb6e687ab1bdd6f2",
    "source_sha256": "c5201138f493237e7dbb04b3b0b15878d7b5e5b83f6f0d62cb5979632564c832",
    "render_data_sha256": "fff4685ed3cb516dd8332f6fcee89cbda27ede036d866027513971eec24d3cf0",
    "media": [],
    "pdf_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "origin": {
    "kind": "subject_publication",
    "subject_id": "00000000-0000-4000-8000-000000000010",
    "artifact_id": "00000000-0000-4000-8000-000000000020",
    "artifact_version": 3,
    "artifact_input_hash": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
  },
  "pdf": {
    "availability": "ready",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "page_count": 2,
    "href": "/api/pdf-inspections/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "visual_diagnostics_href": "/api/pdf-inspections/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "mapping_status": "partial",
    "page_png_url_template": "/api/pdf-inspections/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/pages/{page}.png",
    "visual_diagnostics": [
      {
        "code": "page_blank",
        "page": 2,
        "measurement": {"text_word_count": 0}
      }
    ],
    "canonical_blocks": [
      {
        "target_id": "target-diagram-1",
        "target": {
          "subject_id": "00000000-0000-4000-8000-000000000010",
          "artifact_id": "artifact-owner"
        },
        "locations": [{"page": 1}]
      }
    ]
  }
}
```

Pour un rendu d’édition, `origin` expose également `manifest_id`, `manifest_schema_version`, `manifest_sha256`, `composition_id`/`composition_version` (V2) ou `batch_id` (V1), release, hashes du document et entrées avec positions, `subject_id`, artefact, version, `input_hash`, run et provenance. La liste d’artefacts V2 ne déduit pas de provenance batch absente.

### Inventaire, lecture bornée et sélection JSON

```json
{
  "schema_version": "render-input-files-v1",
  "render_id": "00000000-0000-4000-8000-000000000001",
  "availability": "ready",
  "files": [
    {
      "file_id": "4ee806f37b3105998f9cdcb369db981b",
      "path": "RENDERER/publication.typ",
      "sha256": "c5201138f493237e7dbb04b3b0b15878d7b5e5b83f6f0d62cb5979632564c832",
      "byte_size": 36,
      "kind": "template"
    },
    {
      "file_id": "7a236451ae388171e21da3248718bb47",
      "path": "RENDERER/helpers.typ",
      "sha256": "914ac42791f9ec027321bb26a2380de16e907dbd2a6f82b1ce394de087ff0cdc",
      "byte_size": 25,
      "kind": "template"
    }
  ]
}
```

```json
{
  "schema_version": "render-input-file-v1",
  "render_id": "00000000-0000-4000-8000-000000000001",
  "file_id": "4ee806f37b3105998f9cdcb369db981b",
  "path": "RENDERER/publication.typ",
  "sha256": "c5201138f493237e7dbb04b3b0b15878d7b5e5b83f6f0d62cb5979632564c832",
  "byte_size": 36,
  "kind": "template",
  "start_line": 1,
  "end_line": 1,
  "content": "#import \"helpers.typ\"\n"
}
```

```json
{
  "schema_version": "render-data-inspection-v1",
  "render_id": "00000000-0000-4000-8000-000000000001",
  "sha256": "fff4685ed3cb516dd8332f6fcee89cbda27ede036d866027513971eec24d3cf0",
  "schema": "typst-publication-model-v20-block-location",
  "availability": "ready",
  "selection": {"pointer": "/title", "value": "Sujet d'essai"}
}
```

`file_id` est fabriqué côté serveur depuis le render, le chemin du snapshot et son hash. Il ne s’agit pas d’un chemin; aucun endpoint n’accepte un chemin client. Les chemins internes sont revalidés comme relatifs POSIX avant exposition.

### Diagnostics

```json
{
  "schema_version": "render-input-diagnostics-v1",
  "render_id": "00000000-0000-4000-8000-000000000001",
  "diagnostics": [
    {
      "code": "page_blank",
      "category": "layout_dependent",
      "severity": "warning",
      "message": "Signal visuel calculé depuis le PDF existant; la mise en page n'est pas évaluée statiquement.",
      "location": {"page": 2, "measurement": {"text_word_count": 0}},
      "suggested_channel": {
        "id": "pdf_review",
        "route": "pdf_inspection",
        "requires_human_decision": true
      }
    }
  ],
  "warnings": {
    "compiler": {"availability": "not_persisted", "items": []},
    "pdf": {
      "availability": "available",
      "items": [
        {
          "code": "page_blank",
          "category": "layout_dependent",
          "severity": "warning",
          "message": "Signal visuel calculé depuis le PDF existant; la mise en page n'est pas évaluée statiquement.",
          "location": {"page": 2, "measurement": {"text_word_count": 0}},
          "suggested_channel": {
            "id": "pdf_review",
            "route": "pdf_inspection",
            "requires_human_decision": true
          }
        }
      ]
    }
  },
  "pdf": {
    "availability": "ready",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "page_count": 2,
    "href": "/api/pdf-inspections/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "visual_diagnostics_href": "/api/pdf-inspections/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "mapping_status": "partial",
    "page_png_url_template": "/api/pdf-inspections/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/pages/{page}.png",
    "visual_diagnostics": [
      {"code": "page_blank", "page": 2, "measurement": {"text_word_count": 0}}
    ],
    "canonical_blocks": [
      {
        "target_id": "target-diagram-1",
        "target": {
          "subject_id": "00000000-0000-4000-8000-000000000010",
          "artifact_id": "artifact-owner"
        },
        "locations": [{"page": 1}]
      }
    ]
  }
}
```

Les erreurs sont persistées uniquement comme `error_code` et message existants; l’API n’en renvoie pas le texte brut. Une localisation compilateur n’est exposée que si son fichier relatif appartient au bundle vérifié. Les avertissements Typst des rendus réussis ne sont pas conservés par le modèle de rendu; `warnings.compiler.availability` le signale sans recompilation.

## Classement et boucle de correction

| Catégorie | Exemple | Canal structuré |
|---|---|---|
| `canonical_data` | Champ JSON absent, référence incohérente, média canonique absent, bloc sans identité/mapping, sujets du manifeste différents du JSON | `content_revision` → révision ciblée existante; `enrichment_revision` → canal d’enrichissement ciblé; `composition` → édition de la composition. |
| `typst_source` | Snapshot indisponible, bundle ancien incomplet, erreur de manifeste/template | `template_code` → changement versionné et testé du template. |
| `compiler` | Échec enregistré avec emplacement exploitable dans un `.typ` du snapshot | `engine_diagnostic` → diagnostic technique; aucune proposition de modifier le `.typ` d’un rendu historique. |
| `layout_dependent` | Page blanche, mapping Typst absent ou cible canonique sans localisation PDF | `pdf_review` → inspection PDF existante et jugement humain. Le layout reste calculé par Typst query et observé dans le PDF; l’inspection statique ne le résout pas. |

Les correspondances PDF passent uniquement par `target_id` exact et les identités canoniques du sidecar PDF. La réponse ne rapproche jamais par titre, ordre, coordonnées proches ou ressemblance visuelle.

## Erreurs et limites

| HTTP | `detail.code` | Cas |
|---:|---|---|
| 404 | `render_not_found` | UUID inconnu ou ID preview mal formé. |
| 404 | `file_not_in_bundle` | `file_id` inventé, absolu ou contenant `..`; il n’est jamais interprété comme chemin. |
| 503 | `input_snapshot_unavailable` | blob source absent, cache preview expiré/évincé ou snapshot trop ancien. |
| 409 | `input_hash_mismatch` | SHA déclaré incompatible, snapshot mal formé ou origine différente. |
| 422 | `invalid_selector` | JSON Pointer ou combinaison de filtres invalide. |
| 413 | `size_limit_exceeded` | lecture de lignes ou fichier au-delà des bornes. |

Bornes : snapshot sérialisé 32 MiB max; entrées brutes cumulées 24 MiB; render-data 8 MiB; fichier d’entrée 16 MiB; inventaire 500 fichiers; lecture 200 lignes par requête et 512 KiB de réponse. Le cache dérivé PDF garde ses quotas et TTL existants (512 MiB, 24 h); le PDF preview reste borné à 100 MiB et 10 min. L’ID preview est SHA-256 de `{kind, preview_input_hash, pdf_sha256}`. Un snapshot dépassant les limites n’empêche pas le rendu, mais l’inspection des fichiers non conservés devient `partial` ou `unavailable`.

Les routes suivent le dépôt et le service d’inspection PDF existants; elles n’ajoutent pas de modèle d’autorisation/TLP. Elles n’émettent ni URL signée, ni chemin système, ni secret, ni commande Typst. Les routes n’acceptent aucun code de template ou paramètre d’exécution.

## Lot E — intégration UI

- Sur le téléchargement du PDF preview d’édition, lire `X-Render-Inspection-ID`, puis charger `/api/render-inspections/previews/{id}` et ses sous-routes. Le PDF et les diagnostics visuels restent liés par `pdf.sha256`.
- Sur un rendu publié ou release, utiliser `render.id` (ou l’ID `render[]` de l’inspection PDF) pour ouvrir l’inspection Typst. Garder `origin` comme lien de provenance vers sujet/artefact ou édition/manifest/composition.
- Afficher `availability=partial|unavailable` pour les rendus historiques; ne pas tenter de retrouver des `.typ` dans le dépôt courant.
- Pour l’éditeur de source, charger `/files`, puis le fichier par `file_id` et une plage courte. Pour l’inspecteur JSON, demander seulement le `subject_id`, `block_key` ou JSON Pointer utile.
- Relier les cibles canonisées uniquement via `pdf.canonical_blocks[].target_id`; ouvrir pages et miniatures par les liens PDF existants. Les canaux de correction sont des indications qui renvoient vers les routes de révision/composition existantes et ne modifient pas un rendu historique.

## Validation et état non vérifié

- `make test-backend PYTEST_ARGS='tests/test_typst_input_inspection.py tests/test_pdf_inspection.py -q --tb=short'` : **22 passés**.
- `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_edition_render_repository.py INTEGRATION_PYTEST_ARGS='-q --tb=short'` : **4 passés**; dépôt SQLAlchemy sur PostgreSQL jetable, blobs représentés par les fixtures intégration, aucune base réelle.
- `make lint-backend` : OK; 550 fichiers formatés.
- `make typecheck-backend` : OK; 528 fichiers source contrôlés.

Non vérifié : aucun smoke Typst réel lancé par le Lot D, aucune compilation ni requête modèle; pas d’essai sur PDF/TLP réel ni validation du cache en déploiement Compose. Les chemins preview et publication sont couverts par tests unitaires simulés; l’intégration PostgreSQL couvre la résolution du render ID et de sa provenance. Le service PDF existant peut utiliser Poppler comme d’habitude.
