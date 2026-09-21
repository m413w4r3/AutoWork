# Selection canonique et matérialisation du Subject

## Frontière d’autorité

Fusion décide la structure ; Selection décide s’il faut matérialiser un `Subject` ; `Subject` est
stable ; Production décide quand produire ce `Subject`.

`DiscoveryCandidate` reste la proposition brute canonique et `DiscoverySnapshot` le résultat
versionné de Fusion. Selection lit le snapshot actif et ne fait ni merge ni split. Elle ne choisit
pas un format `brief` ou `major`, ne compose pas le prochain lot et ne lance pas la production.

## État canonique

`SelectionDecision` est la décision humaine append-only prise pour un groupe identifié par
`discovery_subject_id` dans un `DiscoverySnapshot`. Son action est exactement `SELECT` ou
`IGNORE`, avec l’acteur vérifié, la clé d’idempotence, la version attendue du snapshot, le
`correlation_id`, le TLP demandé le cas échéant et la provenance de la décision.

`SubjectDiscoveryOrigin` est l’origine canonique d’un `Subject` créé par une décision `SELECT`.
Elle conserve le lien vers le `discovery_subject_id`, le snapshot et sa version, les
`DiscoveryCandidate` effectivement matérialisés, l’identifiant de la décision et la provenance.
Elle permet de détecter un conflit avec un `Subject` déjà sélectionné sans faire confiance au
titre, à la position d’une carte ou à une similarité visuelle.

La projection d’état effectif est calculée à partir des décisions append-only : la dernière
décision valide pour un groupe et son snapshot détermine l’état effectif. Une décision
`IGNORE` signifie que le groupe n’est pas matérialisé ; elle conserve toutefois la décision,
les versions et la provenance pour l’audit. Un `SELECT` déjà appliqué relit le même `Subject`
par `SubjectDiscoveryOrigin` et ne crée pas un doublon.

## SELECT atomique

Un `SELECT` vérifie dans une transaction unique :

1. que l’édition est mutable et que le snapshot fourni est le snapshot actif ;
2. que le `discovery_subject_id` et ses candidates appartiennent à ce snapshot ;
3. qu’aucun `SubjectDiscoveryOrigin` conflictuel n’existe déjà ;
4. que le TLP dérivé respecte les restrictions de l’édition et des candidates ;
5. qu’il existe la `SelectionDecision`, le `Subject` et son `SubjectDiscoveryOrigin`.

La décision et la matérialisation sont donc atomiques : un succès rend les deux visibles, et un
échec ne laisse ni décision de sélection appliquée ni `Subject` partiellement créé. Le workspace
et les fichiers de production restent des matérialisations non canoniques.

`IGNORE` est aussi une décision append-only, mais ne crée ni `Subject`, ni origine, ni workspace.
Il ne supprime pas une origine déjà matérialisée ; une nouvelle décision est nécessaire pour
corriger l’historique.

## Idempotence et concurrence optimiste

L’`Idempotency-Key` identifie une requête, pas une décision : elle couvre le lot entier confirmé
par l’opérateur. Le service enregistre, sous `(edition_id, Idempotency-Key)`, l’empreinte
canonique du snapshot et de l’ensemble des décisions. Rejouer exactement le même lot retourne le
même résultat canonique sans nouvelle matérialisation ; la même clé avec un sous-ensemble, un
sur-ensemble, une autre action ou un autre snapshot répond `409 selection_idempotency_conflict`
avant toute écriture.

La commande doit aussi fournir `snapshot_version`. Si le snapshot actif a changé, l’API répond
`409 selection_snapshot_stale` et le client recharge le board Fusion. Si l’état effectif ou la
décision a changé entre la lecture et l’écriture, l’API répond `409 selection_decision_stale`.
`expected_decision_id: null` est une attente explicite — « aucune décision » — et non l’absence
d’attente : si une décision est apparue entre-temps, la commande est refusée comme périmée.
Ces erreurs ne sont pas résolues par un écrasement client : il faut relire puis soumettre une
nouvelle décision avec une nouvelle clé.

## TLP, provenance et fusion/séparation

Le TLP du `Subject` est dérivé du TLP le plus restrictif entre l’édition, le snapshot, les
candidates et le TLP explicitement autorisé par la décision. Une décision ne peut jamais
déroger vers un TLP moins restrictif, et l’origine conserve les éléments ayant produit la valeur.

La provenance relie `SelectionDecision` à l’acteur vérifié, au `correlation_id`, au snapshot,
aux candidates et à leurs sources. Elle est append-only et est distincte des signaux de
compatibilité. Les IOC, scores ou recommandations de modèle ne déclenchent jamais
automatiquement un `SELECT`.

Fusion peut ensuite produire un nouveau snapshot par merge ou split, mais ne réécrit ni la
décision ni l’origine historiques. Un nouveau groupe issu d’un merge ou d’un split doit être
traité par Selection ; un `Subject` existant reste stable et son `SubjectDiscoveryOrigin`
continue d’identifier ce qui a été matérialisé. Toute décision de production ultérieure utilise
les identifiants canoniques et la provenance, jamais une identité reconstruite depuis un titre.

## API Selection

`GET /api/editions/{edition_id}/selection` retourne le snapshot actif, sa `snapshot_version`,
les groupes candidats, leur état effectif, les conflits issus de `SubjectDiscoveryOrigin` et les
décisions historiques nécessaires à l’audit.

`POST /api/editions/{edition_id}/selection/decisions` reçoit :

- `discovery_subject_id` ;
- `action: SELECT | IGNORE` ;
- `snapshot_version` ;
- `Idempotency-Key` ;
- le contexte de provenance et, pour `SELECT`, les paramètres autorisés de TLP.

La réponse contient la `SelectionDecision`, l’état effectif et, pour `SELECT`, le `subject_id`
stable ainsi que le `SubjectDiscoveryOrigin`. Les routes de Selection n’exposent aucune action
de merge, split, composition de lot ou lancement de production.

Selection ne choisit jamais le prochain lot et ne lance aucune production. La surface Production
lit séparément son `ProductionBoard` via `GET /api/editions/{edition_id}/production`, puis envoie
explicitement les identifiants canoniques :

```text
POST /api/editions/{edition_id}/production/batches
Idempotency-Key: <clé du lot>
{"subject_ids": ["<subject-a>", "<subject-b>"]}
```

Production ne rappelle donc aucune route Selection. Le board retourne `200` même sans sujet et une
édition `ARCHIVED` reste en lecture seule. Le batch conserve l’ordre du payload, rejoue exactement
le même résultat sous la même clé et refuse une clé/empreinte incompatible avec un batch actif.
