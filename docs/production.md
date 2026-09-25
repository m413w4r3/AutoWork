# Production canonique

## Modèle

`Subject` est l’identité éditoriale stable matérialisée par Selection. Il conserve sa relation
avec l’édition, sa provenance Discovery/Fusion, ses sources et ses artifacts.

`ProductionRun` est une tentative historique de produire un `Subject`. Un sujet peut donc avoir
plusieurs runs, par exemple après un échec, une annulation ou une nouvelle génération. Un nouveau
run n’écrase jamais un run, un artifact ou une décision antérieurs.

`ProductionInputSnapshot` est créé dans la même transaction que son run : jamais de run sans
snapshot, jamais de snapshot sans run. Il fige exactement ce que cette tentative savait au moment
où elle a commencé. Une trigger PostgreSQL interdit tout `UPDATE` et tout `DELETE` de la table
`production_input_snapshots`.

En résumé :

- Selection crée un `Subject` ; elle ne crée jamais de `ProductionRun`, de batch ni de job.
- Production crée un `ProductionRun`, uniquement sur une action explicite de l’opérateur.
- Le `ProductionRun` fige son entrée.
- Une nouvelle vague Discovery, une Fusion ou un renommage du Subject n’altère jamais un run
  existant.
- Un nouveau run peut être créé ultérieurement sur le nouvel état ; il reçoit le `run_number`
  suivant du Subject (`UNIQUE(subject_id, run_number)`).

### Construction du snapshot

La résolution est toujours :

```text
ProductionRun.subject_id → Subject → SubjectDiscoveryOrigin
  → resolve_canonical_subject(origin.discovery_subject_id)
  → DiscoverySnapshot actif → member_candidate_ids → DiscoveryCandidate → SourceCandidate
```

Le snapshot capture :

- `subject_version`, `subject_title`, `subject_tlp` : un renommage ultérieur ne réécrit pas le run ;
- `selection_decision_id`, `origin_discovery_subject_id` (identité sélectionnée) et
  `canonical_discovery_subject_id` (identité courante après merges éventuels) ;
- `discovery_snapshot_id` et `discovery_snapshot_version` : le membership exact utilisé ;
- `member_candidate_ids`, uniquement des `DiscoveryCandidate.id` ;
- `discovery_summary`, issu du résumé du `DiscoverySubject` canonique ;
- `actor_or_campaign`, dédupliqué de façon déterministe depuis les candidates membres ;
- la période de l’édition et la `research_date` ;
- les `core_sources`, identifiées par `(discovery_candidate_id, source_candidate_id)`, avec URL,
  rôle, TLP, sensibilité et politique de modèle externe. Le `discovery_batch_id` n’est qu’une
  provenance de collecte.

Aucun titre normalisé, `local_ref` ou contenu de `DiscoveryBatch` ne sert de source fonctionnelle.

### Hashes

`input_hash` est le SHA-256 déterministe de toute l’entrée fonctionnelle, `research_date`
comprise. `reuse_basis_hash` couvre la même entrée sans `research_date` : c’est la base
d’égalité qui permet de réutiliser raisonnablement un stage coûteux entre deux runs. Aucun des
deux n’inclut d’identité technique (run, snapshot, job, conversation, horodatages) ; les listes
sont triées avant sérialisation. AW-010 à AW-013 préciseront les règles de réutilisation par stage.

Tous les stages construisent leur contexte depuis ce snapshot. Son absence est une erreur
`production_input_snapshot_missing` ; il n’existe aucune lecture de repli.

### REFERENCES et corpus de production

Dans AW-010, l’artifact canonique de `REFERENCES` est `ProductionReferenceCorpusV1` : il décrit
les sources retenues pour ce run, leur provenance, tier, type, état de collecte, document archivé
exact, hash du contenu et éligibilité à l’extraction. Les sources du snapshot sont conservées
comme `CORE`; la recherche web peut ajouter des références `SUPPORTING` ou des ressources
`TECHNICAL`. Une source inaccessible reste dans le corpus avec son état et n’est pas éligible.

Le blob RAW conserve temporairement le wire format de recherche historique, notamment
`editorial-title` et `EVENT`, pour les consommateurs legacy. Ces champs ne font pas partie du
corpus canonique. `REFERENCES` appelle `ModelGateway` sans conversation canonique. Le hash
fonctionnel de l’étape permet la réutilisation d’un artifact compatible entre runs; le corpus
réutilisé ne porte donc pas d’identité de `ProductionRun`.

Le `Reference corpus` du domaine malware/investigation et `ProductionReferenceCorpusV1` de la
production éditoriale sont deux contrats distincts : ils ne partagent ni module ni service.
La projection legacy `ReferenceReport` demeure temporaire pour les stages qui n’ont pas encore
migré.

Transition prévue : AW-011 fera consommer directement le corpus par Extraction; AW-012 retirera
la dépendance aux `EVENT` legacy dans Synthesis; AW-013 terminera la suppression de la projection
`ReferenceReport`.

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

`ProductionBatchService.create` est l’unique primitive de création, pour un seul sujet comme pour
plusieurs. Dans une transaction, elle verrouille l’édition, vérifie qu’elle est ouverte, puis
valide chaque sujet (existence, appartenance à l’édition, `SubjectDiscoveryOrigin`, absence de run
`QUEUED`/`RUNNING`) et capture son snapshot avant toute écriture. Un seul sujet invalide annule tout :
aucun batch, aucun run, aucun snapshot. L’ordre du payload devient l’ordre du lot ; il n’est
jamais retrié. Le job `production.subject.sources` du premier run n’est soumis qu’après le commit :
si le dispatcher échoue, un replay exact avec la même clé reprend la soumission.

La clé d’idempotence est liée à `(edition_id, Idempotency-Key)` et à l’empreinte de
`edition_id` et des `subject_ids` ordonnés. Un replay exact retourne le même batch sans créer de
nouveau run ni de nouveau job logique.

| Cas | Réponse |
| --- | --- |
| `Idempotency-Key` absente, `subject_ids` absent ou vide | `422` |
| édition ou sujet inconnu | `404` (`edition_not_found`, `production_subject_not_found`) |
| édition archivée | `409 production_edition_archived` |
| même clé, autre payload (y compris autre ordre) | `409 production_idempotency_conflict` |
| nouvelle clé pendant un batch actif | `409 production_batch_active` |
| sujet ayant déjà un run actif | `409 production_subject_active` avec les `subject_ids` |
| sujet sans origine Discovery ou d’une autre édition | `409` avec le `subject_id` concerné |

Un batch n’est qu’une enveloppe de séquencement (ordre, pacing, annulation groupée). Un seul run
est `RUNNING` à la fois ; un run terminal (`READY`, `NEEDS_REVIEW`, `FAILED`, `CANCELLED`) passe
la main au suivant. Plusieurs vagues peuvent se succéder dans l’édition : `[A, B]`, puis `[C]`,
puis de nouveau `[A]`. L’édition ne porte aucun statut ni phase de production.

L’historique d’un sujet est lu par `GET /api/subjects/{subject_id}/production/runs` (du plus
récent au plus ancien), un run par `GET /api/production/runs/{run_id}`, et
`GET /api/subjects/{subject_id}/production` reste un raccourci vers le dernier run.

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
