# Workflow canonique AutoWork

AutoWork est un cockpit de production CTI avec des traitements asynchrones. Le navigateur
déclenche des commandes et lit des états ; il ne communique jamais directement avec les services
externes. PostgreSQL, les fichiers versionnés et les evidence packs sont canoniques. Les
workspaces, conversations et réponses de services externes sont des projections reconstructibles.

## Édition et frontières

Une édition est un conteneur mensuel en état `OPEN` ou `ARCHIVED`. Elle ne porte aucune phase
globale de production : les statuts et étapes appartiennent aux `ProductionRun` des `Subject`.
Une édition archivée reste lisible, mais ses commandes de mutation sont refusées.

Les capacités sont séparées :

1. Discovery produit des `DiscoveryCandidate` versionnés.
2. Fusion produit le `DiscoverySnapshot` actif et décide la structure.
3. Selection décide atomiquement si un élément devient un `Subject`.
4. Production traite explicitement les `subject_id` sélectionnés.
5. Review et Publication figent les artifacts retenus dans un manifeste immutable.

Selection ne compose jamais un lot de production et Production ne lit jamais l’API Selection.
Chaque frontière utilise les identifiants canoniques, jamais un titre, une position de carte ou une
similarité visuelle.

## Discovery, Fusion et Selection

`POST /api/editions/{edition_id}/discovery/runs` crée un `DiscoveryRun` avec une
`Idempotency-Key`. Discovery et Fusion peuvent être relus sur une édition archivée, sans mutation.
Fusion versionne ses snapshots ; il ne réécrit ni les candidats ni les snapshots antérieurs.

Selection lit le snapshot actif via :

```text
GET  /api/editions/{edition_id}/selection
POST /api/editions/{edition_id}/selection/decisions
```

La décision `SELECT` matérialise dans une transaction le `Subject` stable et son
`SubjectDiscoveryOrigin`. `IGNORE` conserve seulement l’historique. La commande porte le
`snapshot_version`, la décision attendue et une clé d’idempotence. Un replay exact retourne le
même résultat ; un snapshot périmé ou une empreinte différente est refusé en `409`.

## Production canonique

La surface Production commence par le `ProductionBoard` :

```text
GET /api/editions/{edition_id}/production
```

Le board retourne `200` même lorsqu’il est vide. Il expose les `Subject` éligibles, le batch actif
et les batchs récents. L’opérateur choisit explicitement l’ordre canonique des `subject_ids` et
crée un lot avec :

```text
POST /api/editions/{edition_id}/production/batches
Idempotency-Key: <clé de la commande>
{
  "subject_ids": ["<subject-a>", "<subject-b>"]
}
```

Le service `ProductionBatchService.create` vérifie l’édition, les sujets, l’ordre et l’absence de
batch actif incompatible, puis crée un `ProductionRun` et son `ProductionInputSnapshot` pour
chaque sujet. La clé est enregistrée avec l’empreinte canonique de l’ensemble du payload : un
replay exact retourne le batch existant, tandis qu’une nouvelle clé pendant un batch actif est
refusée sans créer de run concurrent.

Le snapshot d’entrée est immuable et capture exactement l’état Discovery, Fusion et Subject vu au
démarrage. Il adresse les versions et hashes des données utilisées ; un changement ultérieur ne
modifie jamais ce snapshot ni le run historique.

Chaque run suit une pipeline statique et ordonnée :

```text
SOURCES → REFERENCES → EXTRACTION → RELEVANCE_PROJECTION → SYNTHESIS → EDITORIAL_ENRICHMENT → ASSEMBLY → READY
```

`RELEVANCE_PROJECTION` ajoute une classification déterministe propre au sujet sur l’extraction
canonique, sans modifier ses checkpoints source. `SYNTHESIS` et `EDITORIAL_ENRICHMENT` consomment
cette projection. `EDITORIAL_ENRICHMENT` produit un `EditorialEnrichmentV1` canonique vide et
déterministe en AW-015, sans appel modèle. `ASSEMBLY` exige cet artifact et inclut son hash dans l’identité
fonctionnelle de `PUBLICATION`, dont le corps courant est `PublicationDocumentV5`
(`schema_version: "5"`), wrapper sur le document V4 avec `rich_text` sémantique. `READY` signifie
que les artifacts canoniques sont valides et que la QA canonique a réussi ; le rendu PDF constitue
un pipeline distinct.

Les artifacts et diagnostics de chaque étape sont adressés par le run et sa génération de
pipeline. La progression se lit depuis le batch et les runs en base. Une annulation d’un batch
actif annule les runs non terminés, conserve les artifacts historiques et laisse l’édition
`OPEN`. Une nouvelle production peut ensuite capturer un nouvel état avec une nouvelle clé.

Pour AW-010, `REFERENCES` a pour contenu canonique `ProductionReferenceCorpusV1`. Les sources
CORE viennent du `ProductionInputSnapshot`; la recherche via `ModelGateway` est additive et les
références complémentaires sont collectées par le service de collecte existant. Le corpus retient
l’état de collecte observé, ainsi que l’identifiant du `SourceDocument` exact et son hash lorsqu’il
est disponible. Son hash fonctionnel autorise la réutilisation cross-run d’un artifact équivalent.
Le stage n’a pas de conversation canonique; le RAW conserve temporairement le wire format avec
les champs legacy nécessaires aux anciens consommateurs.

Le corpus malware/investigation reste distinct de `ProductionReferenceCorpusV1` et ne partage
avec lui ni module ni service. Extraction consomme le corpus directement. `RELEVANCE_PROJECTION`
classe les éléments de `ProductionExtractionV1` pour le sujet figé. Synthesis et Editorial
Enrichment ne consomment que les éléments admis par cette projection. Assembly construit
`PublicationDocumentV5` (`schema_version: "5"`) depuis le snapshot, le corpus, l’extraction, la projection, la synthèse et
cet enrichment, sans compiler de média ni rendre de document. Production State V5 conserve les
quatre artefacts portables de `REFERENCES`, `EXTRACTION`, `SYNTHESIS` et `EDITORIAL_ENRICHMENT`;
l’import reprend à `RELEVANCE_PROJECTION` pour la reconstruire avant Synthesis.

La surface Production ne déclenche ni `GET` ni `POST` Selection. Elle ne dépend d’aucune projection
de regroupement éditorial et n’ajoute aucun statut de production à l’édition.

## Review et publication

La review est distincte de la production. Elle travaille sur la composition éditoriale courante,
les `ProductionRun`, leurs générations et les références exactes des documents. L’acceptation
requiert `expected_composition_version`, vérifie la composition et ses artifacts sous verrou, puis
crée un manifeste append-only V2 avec l’ordre, les exclusions décidées et les références exactes.
Le manifeste V2 porte `composition_id` et `composition_version`; les batches sont une provenance
nullable par entrée, jamais un batch racine. Les manifestes V1 historiques gardent leur schéma
avec batch racine et restent lisibles. L’assemblage utilise les références gelées et crée les JSON
canoniques `EditionDocumentV2` et `EditionRelease`.

La frontière d'article est `PublicationDocumentV5 → PublicationRender → Typst PDF`. Le preview
Subject projette le V5 côté frontend ; le wrapper V5 conserve les champs V4 et ajoute `rich_text`.
`GET /api/subjects/{id}/publication/pdf` sert le PDF AW-019. Le rendu d'édition accepte les
documents de schéma `4` et `5`. Pour l'édition, `PublicationManifestV1|PublicationManifestV2 → EditionDocumentV2 → EditionRelease →
EditionRender → Typst PDF` rend un seul document Typst à partir des articles ordonnés du manifeste.
`EditionRelease` reste JSON uniquement et ne contient pas le PDF.

Les jobs `publication.edition.assemble` et `publication.edition.render` séparent la création du
release de son rendu. `EditionRender` est une ligne distincte, identifiée par `input_hash` calculé
sur le release/document, les versions du renderer, du template, du compilateur, du bundle de
polices, la policy de rendu et le format. Un retry du rendu réutilise le release et ne rejoue pas
Assembly ni Production.

`GET /api/editions/{id}/composition/validation` expose la Review appliquée à la version courante.
`GET /api/editions/{id}/composition/preview` construit l’aperçu à partir des pins éditoriaux ; il
peut répondre `partial: true` avec les sujets `missing_subject_ids` et `deferred_subject_ids`.
Son `preview_input_hash` dépend de la version de composition, pas de l’arrivée d’un nouvel artifact
CTI. L’acceptation finale passe par
`POST /api/editions/{id}/composition/accept` avec
`{"expected_composition_version": 3}` ; une Review incomplète est refusée. Le preview édition
affiche `EditionDocumentV2` côté frontend ; son PDF est servi par
`GET /api/editions/{id}/preview/pdf?preview_input_hash=<sha256>`. Le PDF final est servi par
`GET /api/editions/{id}/release/pdf`, et `POST /api/editions/{id}/release/render` lance ou relance
le rendu. Les routes de téléchargement DOCX ont été supprimées.

Une nouvelle génération ou un nouveau run ne réécrit pas les décisions, snapshots, artifacts ou
manifestes historiques. Les blobs résident dans MinIO ; PostgreSQL ne stocke que leurs métadonnées
et références SHA-256.

Le workspace d'édition matérialise la release sous
`release/{publication-manifest.json,edition.json,bulletin.pdf}`. Il reste une projection
reconstructible du release JSON et de l'`EditionRender` PDF.

## Mode opératoire multibatch

La production est progressive et éditorialement indépendante de son ordre. Après une première
Discovery, sélectionner A et lancer explicitement un batch `[A]`. Attendre que son run soit
`READY` et que son artifact `PUBLICATION` soit `VERIFIED`. Une nouvelle Discovery peut ensuite
ajouter C ou apporter des candidats à A. Elle ne modifie pas le `ProductionInputSnapshot`, les
artifacts ni les hashes de A et ne lance aucune tâche de production. L'API de composition peut
alors indiquer :

```json
{
  "axes": {
    "production_validity": true,
    "discovery_update_available": true,
    "composition_eligibility": true
  }
}
```

L'analyste peut conserver le pin actuel de A ou créer explicitement un nouveau batch pour A. Le
nouveau run capture alors le snapshot Discovery actif; la composition ne suit jamais ce run
implicitement.

Après avoir sélectionné et produit les autres sujets, lire la composition et sa validation :

```http
GET /api/editions/{edition_id}/composition
GET /api/editions/{edition_id}/composition/validation
```

`PUT /api/editions/{edition_id}/composition` remplace la composition entière. La commande porte
`Idempotency-Key`, `expected_version`, des positions uniques et, pour chaque entrée incluse, le
pin complet d'un run et d'un artifact appartenant à cette édition :

```http
PUT /api/editions/{edition_id}/composition
Idempotency-Key: compose-edition-v4
Content-Type: application/json

{
  "expected_version": 3,
  "entries": [
    {
      "subject_id": "<subject-a>", "disposition": "included", "position": 1,
      "production_run_id": "<run-a>", "pipeline_generation": 2,
      "artifact_id": "<publication-a>", "artifact_version": 3,
      "artifact_input_hash": "<sha256-a>", "provenance_batch_id": "<batch-a>"
    },
    {"subject_id": "<subject-b>", "disposition": "deferred"},
    {
      "subject_id": "<subject-c>", "disposition": "excluded",
      "review_decision_id": "<exclude-decision-c>"
    }
  ]
}
```

Un `deferred` n'a pas de pin; un `excluded` exige une décision de Review exacte. Un sujet sans
entrée est signalé `subject_unaddressed`. Les sujets différés ou exclus ne bloquent pas les
réparations des sujets inclus, mais leur intention éditoriale reste visible. Un doublon de sujet,
un artifact d'une autre édition, un pin modifié ou une version de composition périmée est refusé.

Prévisualiser le document assemblé, y compris s'il est partiel, puis geler uniquement quand la
validation ne contient aucun blocage :

```http
GET /api/editions/{edition_id}/composition/preview
POST /api/editions/{edition_id}/composition/accept
Content-Type: application/json

{"expected_composition_version": 4}
```

Le manifeste V2 conserve l'ordre, les IDs exacts et les hashes; il n'a pas de batch racine. Après
acceptation, le job `publication.edition.assemble` produit le release JSON à partir des pins. Son
retry réutilise le même manifeste et le release associé; il ne relance pas Production. Le rendu
PDF est distinct et peut être redemandé par `POST /api/editions/{edition_id}/release/render`.

Un merge Fusion qui ferait converger deux `Subject` matérialisés est refusé avec le code
`fusion_selected_subject_conflict` et leurs `subject_ids`. L'opérateur doit choisir explicitement
de garder les deux entrées distinctes dans la composition ou de corriger le périmètre; aucun titre
ni score de similarité ne tranche l'identité.

## Inspection Typst et correction ciblée

Pour un rendu publié, suivre `render.id`; pour un PDF de preview, suivre l'ID opaque renvoyé par
`X-Render-Inspection-ID`. Les routes `GET /api/render-inspections/{render_id}`, `/files`,
`/render-data` et `/diagnostics` exposent le snapshot exact et des lectures bornées. Les variantes
preview utilisent `/api/render-inspections/previews/{inspection_id}`. `file_id`, `subject_id`,
`block_key` et JSON Pointer sont des sélecteurs stricts; aucun chemin libre n'est accepté.

L'inspection est en lecture seule. Un défaut de contenu ou d'enrichissement doit être résolu via
la révision ciblée existante, avec son artifact/version/hash cible et son preview, puis appliqué
avec les mêmes fences. Un problème de composition passe par une nouvelle version de composition;
un problème de template passe par une modification versionnée et testée du dépôt. Aucun agent ne
modifie directement un `.typ` historique ou un workspace pour faire passer cette modification
pour canonique. Typst query et l'inspection PDF restent requis pour juger la mise en page.

## Écrans principaux

```text
/editions/{edition_id}                        Dashboard
/editions/{edition_id}/discovery              Discovery
/editions/{edition_id}/fusion                 Fusion
/editions/{edition_id}/selection              Selection
/editions/{edition_id}/production             ProductionBoard et suivi
/editions/{edition_id}/review                 Review
/editions/{edition_id}/publication            Publication et téléchargements
```

Ces routes sont des destinations indépendantes, pas une machine à états de l’édition. Les tâches
techniques, les fichiers et les workspaces sont consultables sans devenir une seconde source de
vérité.

## Historique explicitement legacy

Les anciens profils et formats `brief`/`major`, lorsqu’ils apparaissent encore dans des lecteurs
ou artifacts historiques, sont des compatibilités legacy isolées. Ils ne sont ni produits par la
pipeline courante, ni utilisés par Selection ou Production, et aucune nouvelle fonctionnalité ne
doit en dépendre.
