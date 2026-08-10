# Persistance, provenance et stockage de blobs

## Frontières

Le domaine (`cti_app.domain`) ne dépend ni de SQLAlchemy ni de MinIO. Les ports de repositories, de Unit of Work et de stockage vivent dans `cti_app.application`. PostgreSQL, MinIO et le filesystem sont des adaptateurs dans `cti_app.infrastructure`.

PostgreSQL est la source canonique des identités, métadonnées, relations et événements. Aucun corps de document, binaire, archive ou contenu volumineux n'est stocké dans une colonne SQL.

## Tables

| Table | Rôle | Invariants principaux |
| --- | --- | --- |
| `blobs` | Catalogue des objets binaires | unicité `(logical_bucket, sha256)`, taille positive, clé objet déterministe |
| `subjects` | Identité stable minimale d'un dossier sujet | `external_id` et `slug` uniques, TLP sans déclassement |
| `source_documents` | Sémantique d'une source acquise | référence restrictive vers `blobs`, provenance d'acquisition et politique de diffusion |
| `samples` | Sémantique d'un échantillon | table et repository distincts des documents, référence restrictive vers `blobs` |
| `provenance_events` | Journal factuel | insertion uniquement ; `UPDATE` et `DELETE` rejetés par trigger PostgreSQL |
| `editions` | État canonique d'une édition mensuelle | unicité pays+période, version optimiste, TLP sans déclassement |
| `edition_audit_events` | Audit métier des éditions | avant/après, acteur et corrélation ; append-only |
| `job_events` | Transitions techniques des jobs | statuts avant/après et acteur ; append-only |
| `model_runs` | Exécutions de modèles | hash d'entrée, versions, usage, statut et références de sortie ; aucun prompt en clair |

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

Les révisions `0001` à `0004` sont additives. Leurs downgrades retirent triggers, index et
tables dans l'ordre inverse. La CI démarre un PostgreSQL isolé, crée une base temporaire par
fixture, teste `upgrade head`, `downgrade base`, les transactions, les triggers et les
repositories, puis supprime la base.
