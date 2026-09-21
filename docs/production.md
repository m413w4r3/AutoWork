# Production canonique

## Modèle

`Subject` est l’identité éditoriale stable matérialisée par Selection. Il conserve sa relation
avec l’édition, sa provenance Discovery/Fusion, ses sources et ses artifacts.

`ProductionRun` est une tentative historique de produire un `Subject`. Un sujet peut donc avoir
plusieurs runs, par exemple après un échec, une annulation ou une nouvelle génération. Un nouveau
run n’écrase jamais un run, un artifact ou une décision antérieurs.

`ProductionInputSnapshot` est créé au démarrage de chaque run. Il fige exactement l’état observé
des données Discovery, Fusion et Subject, avec les versions et hashes nécessaires à l’audit et à
la reprise. Les données canoniques peuvent évoluer ensuite ; le snapshot et le run historique ne
changent pas.

## ProductionBoard

Le board d’une édition est lu par :

```text
GET /api/editions/{edition_id}/production
```

Il retourne `200` même lorsqu’aucun sujet n’est éligible et expose :

- les sujets matérialisés et leur capacité à démarrer un run ;
- le batch actif, sa progression et ses étapes ;
- les batchs récents et leurs résultats.

Le board ne lit pas la route Selection. Une édition `ARCHIVED` reste lisible, mais son board est
en lecture seule : aucun nouveau batch ni changement de sujet n’est accepté.

## Création d’un batch

L’opérateur choisit explicitement les sujets et leur ordre canonique. La commande est :

```http
POST /api/editions/{edition_id}/production/batches
Idempotency-Key: aw009-example
Content-Type: application/json

{"subject_ids": ["<subject-a>", "<subject-b>"]}
```

`ProductionBatchService.create` valide l’édition ouverte, les `subject_id` existants et
l’éligibilité de chaque sujet. Il conserve l’ordre du payload, crée un batch et un
`ProductionRun` par sujet, puis capture les snapshots d’entrée.

La clé d’idempotence est liée à `(edition_id, Idempotency-Key)` et à l’empreinte canonique du
payload. Un replay exact retourne le même batch sans créer de nouveau run. Une même clé avec un
payload différent répond `409`. Une nouvelle clé pendant un batch actif incompatible répond aussi
`409` : elle ne crée pas de production concurrente implicite.

La surface Production utilise exclusivement cette commande avec des `subject_ids` explicites. Elle
ne compose pas un lot à partir de Selection, n’appelle aucune API Selection et ne déduit jamais un
sujet depuis un titre, une position ou une ressemblance visuelle.

## Pipeline et états

Chaque run traverse la pipeline statique suivante :

```text
SOURCES → REFERENCES → EXTRACTION → SYNTHESIS → ASSEMBLY → READY
```

Chaque étape produit un artifact versionné et adressé par les entrées fonctionnelles, le run et la
génération de pipeline. Les états, erreurs, retries et progressions sont conservés dans
PostgreSQL ; Redis et les workspaces ne sont pas des sources de vérité.

L’annulation d’un batch actif arrête les runs non terminés, conserve les artifacts déjà produits
et laisse l’édition ouverte. Une nouvelle commande, avec une nouvelle clé, peut démarrer un nouvel
état et un nouveau run lorsque le board le permet.

## Review et publication

La review examine le run, sa génération et l’artifact exact du document. L’acceptation crée un
manifeste de publication immutable contenant l’ordre, les `subject_id`, les runs, les artifacts,
les versions et les hashes retenus. L’assemblage et les rendus lisent uniquement ce manifeste.

Une nouvelle production ne modifie donc pas les snapshots, artifacts ou manifestes historiques.
