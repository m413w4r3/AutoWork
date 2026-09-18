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
| `discovery_runs` | Vagues de recherche de découverte | rattachement immuable à l'édition, intention et configuration de la vague |
| `discovery_batches` | Révisions parsées d'une vague | rattachement au run et au `ModelRun`, rapport archivé, version de parseur et avertissements |
| `discovery_candidates` | Propositions brutes de découverte | identité immuable, provenance du batch et contenu sémantique non génériquement éditable |
| `editorial_groups` | Groupes proposés et sélectionnés | références de candidats, score explicable, rapprochement historique, version et état |
| `human_decisions` | Décisions de sélection, fusion, séparation et rejet | acteur, corrélation et payload ; append-only |

La provenance relationnelle des propositions est `DiscoveryCandidate -> DiscoveryBatch ->
DiscoveryRun -> Edition`. Chaque `DiscoveryBatch` référence en outre le `ModelRun` et son rapport
archivé : `DiscoveryBatch -> ModelRun -> rapport archivé`. `discovery_candidates` est la source
canonique des candidats bruts ; le `payload` d'un batch ne stocke plus les candidats canoniques
complets. Un retraitement conserve le même `DiscoveryRun`, ajoute une nouvelle révision de
`DiscoveryBatch` et crée de nouvelles identités immuables de `DiscoveryCandidate`. Les anciennes
identités restent adressables ; les lectures opérationnelles actives se déterminent par la
révision de batch, et non par une paire `batch_id + candidate_id`.

La chaîne de révision est relationnelle : `discovery_batches.supersedes_batch_id` et
`discovery_batches.replaced_by_batch_id` sont des colonnes avec clé étrangère `ON DELETE RESTRICT`
et chaînage unique, jamais des entrées de `payload`. Une clé étrangère composite
`(discovery_batch_id, discovery_run_id)` garantit en base que le run d'un candidat est celui de
son batch. Une correction manuelle d'URL publie un nouveau candidat qui pointe vers le candidat
historique via `discovery_candidates.supersedes_candidate_id` ; le candidat historique n'est
jamais modifié et reste lisible avec `include_replaced`. L'activité d'un candidat reste dérivée
de ces relations : `discovery_candidates` ne porte aucune colonne de statut.

`CandidateTopic`, `DiscoverySnapshot` et `CandidateReference` peuvent servir de structures de
parsing, de cumul ou de Selection, mais ne sont pas des magasins canoniques concurrents. Les
annotations de vérification des sources peuvent évoluer ; la provenance sémantique et le contenu
d'un candidat ne sont pas modifiables génériquement. La fusion en `DiscoverySubject` appartient à
AW-007 et la matérialisation/sélection de `Subject` à AW-008.

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
