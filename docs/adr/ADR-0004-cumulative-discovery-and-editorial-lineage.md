# ADR-0004 — Découverte cumulative et lignée éditoriale immutable

Statut : accepté — 2026-08-19

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

Chaque rapport brut reste archivé dans son `DiscoveryBatch`. Il produit en plus un
`DiscoveryIntake` immutable, séquencé et idempotent. Un planner ne produit qu'un
`DiscoveryMergePlanV1`; l'unique `DiscoveryMergeApplier` applique le plan sans lire sa
justification ni ses diagnostics. Les candidats et sujets sont présentés par poignées
opaques `Cn` et `Xn`. Les identifiants sont créés localement :

```text
candidate_key = uuid5(NAMESPACE_URL, "discovery-candidate:{intake_id}:{local_ref}")
origin_key    = concat(candidate_key du groupe créateur, triées)
subject_id    = uuid5(NAMESPACE_URL, "discovery-subject:{edition_id}:{origin_key}")
```

`SubjectContribution.subject_id` désigne pour toujours l'identité à laquelle le
candidat a été rattaché lors de son apparition. Une fusion ultérieure ajoute un
`SubjectMergeEvent`; elle ne repointe aucune contribution. `status` et
`merged_into_id` sont une projection reconstruisible de ce log.

La concurrence est sérialisée par job et protégée par le verrouillage du snapshot
actif, la vérification de `parent_snapshot_id`, l'index actif unique et deux rebases
au maximum. Un plan stale n'est jamais appliqué.

## Matérialisation progressive

- Incrément 1 : identités, intakes, merge runs, contributions et snapshots ; planner
  heuristique ; bootstrap déterministe ; lecture nominale depuis le snapshot.
- Incrément 2 : planner ChatGPT sans Web, blocking, validation/repair, revue humaine
  et fusion explicite.
- Incrément 3 : couverture des contributions par les packs, signal calculé
  `UPDATE_AVAILABLE`, décisions append-only et amendements.
- Incrément 4 : lignée de replay, mapping d'identités et interface complète.

La migration noyau est additive. Le `subject_id` historique des groupes éditoriaux
cible déjà la table de production `subjects` et ne peut pas changer de sens sans
rupture. Le pont vers la nouvelle identité est donc la FK additive
`editorial_groups.discovery_subject_id`. Le backfill est déterministe à partir des
références batch/candidat présentes dans chaque snapshot. Une migration future ne
pourra supprimer l'ancien lien qu'après migration de tous ses consommateurs.

## Conséquences et invariants

- `DiscoveryBatch`, intake, contribution, événement de fusion et contenu d'un
  snapshot ne sont jamais réécrits.
- Un sujet absent d'un plan est reporté bit pour bit dans le snapshot suivant.
- Le titre et le résumé canoniques d'un sujet existant sont conservés.
- Les sources, IOC et références membres viennent exclusivement du parent ou du
  delta et ne peuvent pas être perdus.
- L'idempotence repose sur les hashes et contraintes relationnelles, pas sur le
  déterminisme futur d'un modèle.
- `GET /candidates` lit exclusivement le snapshot actif ; aucune consolidation ni
  mutation n'est effectuée pendant une lecture.
- L'état `STALE` garde sa sémantique actuelle. `UPDATE_AVAILABLE` sera un signal
  orthogonal calculé lors de l'incrément 3.
