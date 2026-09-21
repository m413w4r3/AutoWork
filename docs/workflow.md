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
SOURCES → REFERENCES → EXTRACTION → SYNTHESIS → ASSEMBLY → READY
```

Les artifacts et diagnostics de chaque étape sont adressés par le run et sa génération de
pipeline. La progression se lit depuis le batch et les runs en base. Une annulation d’un batch
actif annule les runs non terminés, conserve les artifacts historiques et laisse l’édition
`OPEN`. Une nouvelle production peut ensuite capturer un nouvel état avec une nouvelle clé.

La surface Production ne déclenche ni `GET` ni `POST` Selection. Elle ne dépend d’aucune projection
de regroupement éditorial et n’ajoute aucun statut de production à l’édition.

## Review et publication

La review est distincte de la production. Elle travaille sur le `ProductionRun`, sa génération,
l’artifact de document et son hash d’entrée. L’acceptation crée un manifeste de publication
append-only qui fige l’ordre et les références exactes. L’assemblage lit uniquement ce manifeste
et produit les rendus Markdown/DOCX de façon déterministe.

Une nouvelle génération ou un nouveau run ne réécrit pas les décisions, snapshots, artifacts ou
manifestes historiques. Les blobs résident dans MinIO ; PostgreSQL ne stocke que leurs métadonnées
et références SHA-256.

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
