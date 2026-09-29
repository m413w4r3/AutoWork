# Persistance, provenance et stockage de blobs

## Frontières

Le domaine ne dépend ni de SQLAlchemy ni de MinIO. Les ports vivent dans l’application et
PostgreSQL, MinIO et le filesystem sont des adaptateurs. PostgreSQL est canonique pour les
identités, métadonnées, relations et événements ; les documents et binaires résident dans MinIO,
adressés par SHA-256. Un workspace ou une conversation ne peut jamais être une source de vérité.

## Tables canoniques

| Table | Rôle et invariants |
| --- | --- |
| `editions` | Conteneur mensuel, version optimiste et TLP monotone ; état `OPEN`/`ARCHIVED`. |
| `subjects` | Identité éditoriale stable, directement rattachée à l’édition. |
| `discovery_candidates` | Candidats Discovery immuables et leur provenance de batch. |
| `discovery_snapshots` | Versions append-only de Fusion et membres adressés par UUID. |
| `selection_decisions` | Décisions `SELECT`/`IGNORE`, acteur vérifié, snapshot attendu et idempotence. |
| `subject_discovery_origins` | Origine append-only du `Subject` matérialisé par Selection. |
| `human_decisions` | Journal append-only ; `subject_ids` conserve les sujets explicitement concernés. |
| `production_runs` | Tentatives historiques d’un sujet, génération, état, étape et erreurs. |
| `production_input_snapshots` | Snapshot immutable de l’état Discovery/Fusion/Subject au démarrage d’un run. |
| `edition_production_batches` | Lot explicite, état agrégé, clé d’idempotence (`UNIQUE(edition_id, idempotency_key)`) et empreinte du payload. |
| `edition_production_batch_items` | Position de chaque sujet dans le lot et run exact qu’il pilote. |
| `production_artifacts` | Résultats versionnés des étapes de production, référencés par hash. |
| `source_extractions` | Checkpoints d’extraction adressés par contenu, indépendants du Subject et du run. |
| `publication_manifests` | Ordre et artifacts exacts retenus pour une publication, append-only. |
| `blobs` | Catalogue des objets MinIO, unicité par bucket logique et SHA-256. |

La baseline finale ne contient aucune table `editorial_groups`. Elle ne contient pas non plus de
`group_id` sur `source_collections`, `claims` ou `indicators`. Les relations de sélection et de
production utilisent `subject_id` et les identifiants de snapshot/run ; aucune identité n’est
reconstruite depuis un titre.

Les snapshots de découverte, décisions humaines, runs, snapshots d’entrée, artifacts et
manifestes sont append-only ou versionnés. Une correction crée une nouvelle version et ne réécrit
pas les faits historiques.

## Production et immutabilité

`Subject` est l’identité stable. `ProductionRun` est une tentative historique ; plusieurs runs
peuvent donc exister pour un même sujet. `ProductionInputSnapshot` fige exactement l’état observé
au démarrage d’un run, avec les versions et hashes nécessaires à la reprise et à l’audit.

Contraintes structurelles de la baseline :

- `production_runs` : `UNIQUE(subject_id, run_number)`, `run_number >= 1`, `version >= 1`, un seul
  run `queued`/`running` par sujet (index unique partiel) et une FK composite
  `(subject_id, edition_id) → subjects(id, edition_id)` qui interdit un run hors de l’édition de
  son sujet ;
- `production_input_snapshots` : `UNIQUE(production_run_id)`, FK composite
  `(production_run_id, subject_id, edition_id) → production_runs`, FK vers la décision Selection,
  les identités Discovery d’origine et canonique et le snapshot Fusion, versions `>= 1`,
  période ordonnée, hashes SHA-256 hexadécimaux et trigger append-only (`UPDATE`/`DELETE`
  interdits).

`ProductionBatchService.create` reçoit seulement un ordre explicite de `subject_ids` via
`POST /api/editions/{edition_id}/production/batches` et une `Idempotency-Key`. La contrainte
d’idempotence associe la clé à l’empreinte canonique du payload : le même payload rejoué retourne
le même batch ; une autre empreinte ou une nouvelle clé incompatible avec un batch actif est
refusée. Le board de production peut être vide et retourne alors `200` avec zéro sujet.

La pipeline d’un run est fixe : `SOURCES`, `REFERENCES`, `EXTRACTION`, `SYNTHESIS`, `ASSEMBLY`,
puis `READY`. Les états asynchrones vivent en PostgreSQL ; Redis ne transporte que les identifiants
de jobs. L’annulation conserve l’historique, arrête les travaux non terminés et ne ferme pas
l’édition.

### Extraction : artifact borné et checkpoints source-level

`production_artifacts` porte, pour l’étape `EXTRACTION`, un unique `canonical_blob_id` vers
`ProductionExtractionV1`. PostgreSQL ne recopie ni les faits, ni les IOC, ni les règles, ni la
chronologie : le metadata reste une projection bornée (compteurs `source_count`,
`full_source_count`, `ioc_rules_source_count`, `reused_source_count`, `fresh_source_count`,
`omitted_source_count`, `fact_count`, `event_count`, `indicator_count`, `rule_count`,
`warning_count`, versions de contrat et de policy). Le `raw_blob_id` run-level reste vide : les
sorties modèle brutes appartiennent aux checkpoints source-level, donc `model_run_id` n’est pas
fixé sur l’artifact lorsqu’une extraction a nécessité plusieurs appels.

`source_extractions` est un checkpoint content-addressed, indépendant du `Subject` et du
`ProductionRun`. Son identité fonctionnelle
(`uq_source_extractions_identity`) couvre `source_content_sha256`, `profile`, `contract_version`,
`prompt_version`, `parser_version`, `verifier_version`, `source_text_contract_version`,
`model_policy_version` et `routing_policy_version`. Même contenu et mêmes versions ⇒
réutilisation sans appel modèle ; un contenu ou une version différent ⇒ nouveau checkpoint. Un
checkpoint `IOC_RULES` ne satisfait jamais `FULL` ; la projection `FULL` → `IOC_RULES` reste
déterministe. Deux URLs au contenu identique partagent un calcul mais conservent chacune leur
entrée canonique et leur provenance.

### Synthesis : artifact canonique et compatibilité temporaire

Pour `SYNTHESIS`, `canonical_blob_id` pointe sur le JSON validé de `ProductionSynthesisV1` ; c’est
l’unique état canonique de l’étape. `raw_blob_id` peut référencer la réponse brute du modèle si
elle est conservée. `rendered_blob_id` peut référencer un aperçu Markdown déterministe optionnel,
qui reste une projection de présentation ou de compatibilité. `model_run_id` conserve la lineage
de génération et l’identité durable de soumission. PostgreSQL garde les références, hashes et
métadonnées bornées, pas le corps canonique complet.

La réutilisation canonique exacte exige un artifact vérifié dont `canonical_blob_id` se décode en
`ProductionSynthesisV1` valide et dont les hashes d’entrée et d’extraction correspondent. Un
artifact legacy qui ne possède que `rendered_blob_id` n’est pas candidat à cette réutilisation.
Pour une révision, la synthèse précédente n’est qu’un contexte non autoritatif : seules les
évidences de l’extraction courante peuvent justifier le résultat. Toute référence à une évidence
retirée de l’extraction courante est supprimée ou reformulée.

Jusqu’à la refonte d’AW-013, l’adapter temporaire projette `ProductionSynthesisV1` vers la
représentation Markdown/legacy attendue par Assembly. Ce passage est à sens unique : le Markdown
legacy n’est jamais reparsé pour reconstruire un `ProductionSynthesisV1`. Les artifacts
historiques rendus peuvent rester lisibles par compatibilité, sans devenir un état canonique ni
une base de réutilisation.

## Blobs et workspaces

Un blob est adressé par `<bucket>/<2 premiers caractères>/<sha256>`. L’écriture objet précède la
ligne SQL ; une référence canonique ne pointe jamais vers un objet dont l’écriture a échoué. Les
octets ne sont pas renvoyés par les endpoints de liste.

Les workspaces sont des projections locales avec un manifeste `"canonical": false`. Ils sont
reconstructibles, best-effort et sans effet sur PostgreSQL ou MinIO lorsqu’ils sont modifiés ou
supprimés.

## Migrations

`backend/migrations/versions/0001_baseline.py` est l’unique head Alembic et définit la cible
complète depuis une base vide. La validation vérifie les tables, colonnes, contraintes
d’idempotence et triggers d’immutabilité de cette baseline.
