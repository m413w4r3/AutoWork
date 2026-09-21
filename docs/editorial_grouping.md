# Projection éditoriale historique

Ce document décrit uniquement la compatibilité legacy autour de `EditorialGroup`. Il ne décrit
pas l’autorité canonique de Selection et ne définit aucune API utilisateur actuelle.

`LegacyEditorialProjectionService` est une projection reconstructible à partir de
`SubjectDiscoveryOrigin`, du `DiscoverySnapshot` actif, de `DiscoveryCandidate` et de `Subject`.
Il est non canonique : il ne remplace ni `DiscoveryCandidate`, ni le snapshot de Fusion, ni
`selection_decisions`, ni `subject_discovery_origins`. Il ne porte et n’écrit aucune décision
humaine de sélection.

Le service legacy peut recalculer des rapprochements explicables (URLs, dates, domaines, titres,
entités et autres signaux de provenance) pour les anciens consommateurs. Les opérations
structurelles `merge` et `split` restent celles de Fusion ; les décisions `SELECT` et `IGNORE`
restent celles de Selection. Une suggestion de modèle ou un score de compatibilité n’est jamais
une sélection automatique.

La projection est reconstruite par un seul point d’entrée,
`LegacyEditorialProjectionService.synchronize`, déclenché après chaque activation d’un
`DiscoverySnapshot` (nouvelle vague Discovery, `merge` ou `split` Fusion) et après chaque lot de
décisions Selection. Ni Fusion ni la couche Discovery cumulative n’écrivent `EditorialGroup`
directement : elles ne connaissent la matérialisation éditoriale d’une identité que par
`SubjectDiscoveryOrigin`.

`EditorialGroup` n’a pas d’API utilisateur et n’est consommé que par `production legacy` et
`collection legacy`. Il n’est pas une source de vérité, ne lance pas la production et ne décide
pas du `subject_id` du prochain batch. La frontière canonique est : Fusion décide la structure ;
Selection décide s’il faut matérialiser un Subject ; Subject est stable ; Production décide quand
produire ce Subject.

TODO AW-009: delete LegacyEditorialProjection
