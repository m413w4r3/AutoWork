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
| `production_batches` | Lot explicite, ordre des sujets, état agrégé, clé d’idempotence et empreinte du payload. |
| `production_artifacts` | Résultats versionnés des étapes de production, référencés par hash. |
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

`ProductionBatchService.create` reçoit seulement un ordre explicite de `subject_ids` via
`POST /api/editions/{edition_id}/production/batches` et une `Idempotency-Key`. La contrainte
d’idempotence associe la clé à l’empreinte canonique du payload : le même payload rejoué retourne
le même batch ; une autre empreinte ou une nouvelle clé incompatible avec un batch actif est
refusée. Le board de production peut être vide et retourne alors `200` avec zéro sujet.

La pipeline d’un run est fixe : `SOURCES`, `REFERENCES`, `EXTRACTION`, `SYNTHESIS`, `ASSEMBLY`,
puis `READY`. Les états asynchrones vivent en PostgreSQL ; Redis ne transporte que les identifiants
de jobs. L’annulation conserve l’historique, arrête les travaux non terminés et ne ferme pas
l’édition.

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
