# ADR-0004 — Découverte cumulative et lignée éditoriale immutable

Statut : accepté — 2026-08-19 ; amendé — 2026-09-19 (AW-007 : identité canonique
`DiscoveryCandidate` et capacité Fusion)

## Contexte

La découverte était archivée sous forme de `DiscoveryBatch`, puis refusionnée par
`consolidate_discovery_batches()` à chaque lecture. Une référence éditoriale désignait
un couple `(batch_id, candidate_id)`, qui n'est pas une identité durable. Cette
projection ne permet ni concurrence optimiste, ni replay, ni preuve stable de ce qui
a servi à produire un texte.

## Décision

Le modèle conceptuel est séparé en quatre couches qui ne se substituent jamais :

1. `DiscoverySubjectIdentity` est l'identité relationnelle durable d'un sujet dans
   une édition. Son contenu n'est jamais stocké dans cette table. Une identité
   absorbée reste présente et résolvable.
2. `DiscoverySubject` est l'état complet d'une identité dans un
   `DiscoverySnapshot` immutable et versionné. Un seul snapshot de la lignée
   `operational` est actif à la fois.
3. `BriefEvidencePack` est une photographie immutable des preuves choisies, liée à
   un snapshot explicite.
4. Un artefact éditorial est une version de texte liée à un evidence pack précis ;
   une publication n'est jamais réécrite par une nouvelle découverte.

Chaque rapport brut reste archivé dans son `DiscoveryBatch`, qui est une provenance et une
trace d'audit, jamais une identité. Il produit en plus un `DiscoveryIntake` immutable,
séquencé et idempotent. L'identité fonctionnelle canonique d'un sujet découvert est
`DiscoveryCandidate.id` : un UUID métier persisté dans `discovery_candidates`, stable entre
les lectures, les décisions et les projections, et jamais dérivé d'un intake ni d'une
position. Une correction ciblée crée une nouvelle ligne portant
`supersedes_candidate_id`; elle ne réidentifie pas les autres candidats.

Un planner ne produit qu'un `DiscoveryMergePlanV1`; l'unique `DiscoveryMergeApplier` applique
le plan sans lire sa justification ni ses diagnostics. Les poignées opaques `Cn` et `Xn`
restent internes au merge run — elles servent au prompt et à sa correspondance handle ↔ UUID,
et ne sont jamais exposées. Les identités de fusion sont dérivées localement des UUID métier :

```text
origin_key = concat(DiscoveryCandidate.id du groupe créateur, triés)
subject_id = uuid5(NAMESPACE_URL, "discovery-subject:{edition_id}:{origin_key}")
```

Une séparation humaine dérive son identité de la même manière, à partir d'un `origin_key` de
forme `split:{merge_run_id}:{candidate_ids séparés}`, afin qu'une re-séparation identique reste
idempotente sans réutiliser l'identité du sujet d'origine.

Les membres d'un sujet de snapshot sont référencés par le seul UUID métier
(`DiscoveryMemberReference(candidate_id)`); le batch d'origine se retrouve via
`DiscoveryCandidate.discovery_batch_id`.

`SubjectContribution.subject_id` désigne pour toujours l'identité à laquelle le
candidat a été rattaché lors de son apparition, et `SubjectContribution.candidate_id` est une
FK vers `discovery_candidates`, unique par candidate. Une fusion ultérieure ajoute un
`SubjectMergeEvent`; elle ne repointe aucune contribution. `status` et
`merged_into_id` sont une projection reconstruisible de ce log.

Une décision structurelle humaine (fusion, séparation, résolution de revue) produit un
`DiscoveryMergeRun` `human` puis un nouveau `DiscoverySnapshot` sans intake : `intake_id` est
alors nul sur le merge run et sur le snapshot. Seule une réconciliation d'intake porte un
`intake_id`.

La concurrence est sérialisée par job et protégée par le verrouillage du snapshot
actif, la vérification de `parent_snapshot_id`, l'index actif unique et deux rebases
au maximum. Un plan stale n'est jamais appliqué. Une mutation humaine de Fusion porte le
`snapshot_version` affiché ; une version obsolète est refusée avec HTTP `409` et le code
`fusion_snapshot_stale`, sans rebase implicite.

## Matérialisation progressive

- Incrément 1 : identités, intakes, merge runs, contributions et snapshots ; planner
  heuristique ; bootstrap déterministe ; lecture nominale de la fusion depuis le snapshot.
- Incrément 2 : planner ChatGPT sans Web, blocking, validation/repair, revue humaine
  et fusion explicite.
- Incrément 3 : couverture des contributions par les packs, signal calculé
  `UPDATE_AVAILABLE`, décisions append-only et amendements.
- Incrément 4 : lignée de replay, mapping d'identités et interface complète.

La migration noyau est additive. Le `subject_id` historique des groupes éditoriaux
cible déjà la table de production `subjects` et ne peut pas changer de sens sans
rupture. Le pont vers la nouvelle identité est donc la FK additive
`editorial_groups.discovery_subject_id`. Le backfill est déterministe à partir des
références de candidates (`candidate_id`) présentes dans chaque snapshot, le batch
d'origine étant retrouvé via `DiscoveryCandidate.discovery_batch_id`. Une migration future ne
pourra supprimer l'ancien lien qu'après migration de tous ses consommateurs.

## Conséquences et invariants

- `DiscoveryBatch`, intake, contribution, événement de fusion et contenu d'un
  snapshot ne sont jamais réécrits.
- Un sujet absent d'un plan est reporté bit pour bit dans le snapshot suivant.
- Deux blocs `SUBJECT` distincts produisent deux `DiscoveryCandidate` distincts : aucune
  déduplication cross-candidate n'a lieu avant une décision de Fusion.
- Le titre et le résumé canoniques d'un sujet existant sont conservés.
- Les sources, IOC et références membres viennent exclusivement du parent ou du
  delta et ne peuvent pas être perdus.
- L'idempotence repose sur les hashes et contraintes relationnelles, pas sur le
  déterminisme futur d'un modèle.
- `GET /candidates` lit exclusivement les `DiscoveryCandidate` persistés de l'édition —
  l'identité canonique, pas une projection de fusion. Il ne consolide ni ne déduplique rien
  et n'effectue aucune mutation pendant une lecture. L'état de fusion (snapshot actif,
  `snapshot_version`, groupes, revues en attente) est lu par la capacité Fusion via
  `GET /api/editions/{edition_id}/fusion`, dont le read model est reconstruit à chaque
  lecture et n'est pas persisté.
- L'état `STALE` garde sa sémantique actuelle. `UPDATE_AVAILABLE` sera un signal
  orthogonal calculé lors de l'incrément 3.
