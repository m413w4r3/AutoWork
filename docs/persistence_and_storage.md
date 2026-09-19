# Persistance, provenance et stockage de blobs

## Frontières

Le domaine (`cti_app.domain`) ne dépend ni de SQLAlchemy ni de MinIO. Les ports de repositories, de Unit of Work et de stockage vivent dans `cti_app.application`. PostgreSQL, MinIO et le filesystem sont des adaptateurs dans `cti_app.infrastructure`.

PostgreSQL est la source canonique des identités, métadonnées, relations et événements. Aucun corps de document, binaire, archive ou contenu volumineux n'est stocké dans une colonne SQL.

## Tables

| Table | Rôle | Invariants principaux |
| --- | --- | --- |
| `blobs` | Catalogue des objets binaires | unicité `(logical_bucket, sha256)`, taille positive, clé objet déterministe |
| `subjects` | Pivot canonique d'un dossier sujet, rattaché directement à son édition | `edition_id` obligatoire et immuable (`ON DELETE RESTRICT`), unicité `(edition_id, slug)`, slug stable, titre non vide, version optimiste, TLP sans déclassement |
| `source_documents` | Sémantique d'une source acquise | référence restrictive vers `blobs`, provenance d'acquisition et politique de diffusion |
| `samples` | Sémantique d'un échantillon | table et repository distincts des documents, référence restrictive vers `blobs` |
| `provenance_events` | Journal factuel | insertion uniquement ; `UPDATE` et `DELETE` rejetés par trigger PostgreSQL |
| `editions` | État canonique d'une édition mensuelle | unicité pays+période, version optimiste, TLP sans déclassement |
| `edition_audit_events` | Audit métier des éditions | avant/après, acteur et corrélation ; append-only |
| `job_events` | Transitions techniques des jobs | statuts avant/après et acteur ; append-only |
| `model_runs` | Exécutions de modèles | hash d'entrée, versions, usage, statut et références de sortie ; aucun prompt en clair |
| `discovery_batches` | Provenance/audit des résultats de découverte | paramètres hachés, runs, requêtes, citations et révisions |
| `discovery_candidates` | Identités fonctionnelles canoniques découvertes | UUID métier unique, hash/ordre déterministes, `supersedes_candidate_id` et provenance de batch |
| `editorial_groups` | Projection de compatibilité pour Selection | références de candidats par UUID, score explicable, rapprochement historique, version et état |
| `discovery_snapshots` | État versionné de la fusion (`DiscoverySnapshot`) | `version`, parent, sujets de découverte et membres par `candidate_id` ; `intake_id` nul pour une fusion/séparation humaine |
| `discovery_merge_runs` | Trace auditable de chaque proposition ou décision de fusion (`DiscoveryMergeRun`) | planner, snapshot parent, plan, correspondance interne handle ↔ UUID, statut de revue ; `intake_id` nul pour une opération structurelle humaine |
| `subject_contributions` | Apport d'une candidate à un sujet de découverte | unique par `candidate_id` (FK `discovery_candidates`) ; append-only |
| `human_decisions` | Décisions de sélection et de rejet | acteur, corrélation et payload ; append-only |

Il n'existe aucune table `fusion_groups` ni `candidate_groups` : un groupe de Fusion est un
sujet d'un `DiscoverySnapshot` et ses membres sont toujours référencés par l'UUID métier du
`DiscoveryCandidate` (`DiscoveryMemberReference(candidate_id)`), le batch d'origine étant
retrouvé via `DiscoveryCandidate.discovery_batch_id`. Les snapshots et merge runs sont
append-only : chaque décision Fusion produit un nouveau merge run `human` puis une nouvelle
version de snapshot, sans réécrire les précédents. Le read model Fusion n'est pas persisté ; il
est reconstruit à chaque lecture. Une édition `ARCHIVED` autorise la lecture, mais aucune
mutation Fusion.

Une correction ciblée crée une nouvelle ligne `discovery_candidates` portant
`supersedes_candidate_id` (au plus un remplaçant direct par candidate, index unique partiel) ;
l'ancienne ligne reste lisible par son identifiant mais ne participe plus au calcul actif, pas
plus que les candidates d'un batch remplacé (`replaced_by_batch_id`).

Les signaux déterministes sont recalculés localement à la lecture ; la suggestion modèle
provient uniquement du plan et de la justification courte d'un merge run modèle persisté. Les
handles de prompt (`C1`, `X1`) restent internes au merge run et ne sont jamais exposés ; aucune
chaîne de pensée n'est persistée ou exposée.

`source_documents` et `samples` conservent séparément : nom d'origine, origine, date d'acquisition, licence ou restriction, TLP, `do_not_submit` et `external_llm_allowed`. Partager les mêmes octets ne leur donne donc jamais la même sémantique.

Les clés étrangères de documents et échantillons utilisent `ON DELETE RESTRICT`. Le service de cycle de vie vérifie en plus le nombre de références avant de retirer le catalogue, puis seulement l'objet physique. Une panne lors de la suppression physique peut créer un objet orphelin, jamais une référence canonique cassée.

## Adressage des blobs

Un objet est décrit par :

- SHA-256 hexadécimal en minuscules ;
- taille exacte ;
- type MIME déclaré ;
- bucket logique validé ;
- clé déterministe `<bucket-logique>/<2 premiers caractères>/<sha256>`.

MinIO utilise un bucket physique de développement et les buckets logiques comme préfixes. Une écriture répétée du même contenu vérifie l'objet existant et n'en crée pas un second. L'adaptateur filesystem applique les mêmes règles, mais il est réservé aux tests.

L'écriture objet précède l'enregistrement SQL. Si la transaction SQL échoue, l'objet devient éventuellement orphelin et pourra être collecté ultérieurement ; aucune ligne canonique ne peut ainsi pointer vers un objet dont l'écriture n'a pas abouti.

## TLP et provenance

L'ordre de restriction est `CLEAR < GREEN < AMBER < AMBER+STRICT < RED`. Le domaine refuse un déclassement avant persistance et PostgreSQL le refuse également par trigger sur les sujets, documents et échantillons.

Les événements de provenance sont des dataclasses immuables et le repository n'expose qu'une opération `append`. Un trigger protège la table contre toute mise à jour ou suppression, y compris en SQL direct.

## Workspace sujet

`SubjectWorkspaceMaterializer` recrée l'arborescence logique de la spécification depuis les entités canoniques. Les fichiers visibles portent leur SHA-256 ; le nom d'origine reste dans `manifest.json`. Le filesystem de test privilégie un hardlink et revient à une copie atomique contrôlée si nécessaire. MinIO télécharge vers un fichier temporaire, vérifie SHA-256 et taille, puis effectue un remplacement atomique.

Le manifeste contient `"canonical": false`. Supprimer ou modifier un workspace ne modifie donc ni PostgreSQL ni le blob store. Le service ne lance aucun sous-processus et n'exécute jamais les fichiers matérialisés.

## Migrations et tests

AutoWork utilise une unique migration de baseline (`0001_baseline`) qui définit le schéma
complet à partir d'une base vide. La CI démarre un PostgreSQL isolé, crée une base temporaire
par fixture, teste `upgrade head`, `downgrade base`, les transactions, les triggers et les
repositories, puis supprime la base.
