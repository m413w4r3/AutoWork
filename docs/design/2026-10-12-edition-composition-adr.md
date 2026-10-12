# ADR — Composition éditoriale multibatch et inspection des entrées Typst

Date : 2026-10-12 · Branche : `chantier/multibatch-composition` · Base : `main@a0556c4`

Statut : **Lots A à F implémentés dans l'arbre de travail ; validation et preuves consignées dans le journal de clôture.**

## 1. Diagnostic Phase 0 — points de couplage au « dernier batch »

| # | Emplacement | Symbole | Couplage |
|---|---|---|---|
| 1 | `infrastructure/database/repositories/publication_review.py` | `SqlAlchemyEditionReviewReadRepository.list_for_edition` | ancien périmètre limité au dernier batch ; corrigé au Lot B pour lire en un statement la composition courante et ses pins exacts. |
| 2 | `application/edition_publication.py:715` | `_resolve_current_publication_inputs` | `get_latest_for_edition` ; refuse `edition_has_no_production_batch` ; renvoie `batch_id` dans `_ResolvedPublicationInputs`. |
| 3 | `domain/edition_publication.py:111-235` | `PublicationManifestV1` | `batch_id` obligatoire, intégré au `content_sha256`. |
| 4 | `application/edition_publication.py:812-820` | `_publication_inputs_match` | l'égalité manifeste/état courant compare `batch_id`. |
| 5 | `application/edition_publication.py:287-293` | `_freeze` | écrit `batch_id` du dernier batch. |
| 6 | `application/production_review_recovery.py` | `prepare_batch_for_recovery` | ancienne règle refusait tout batch antérieur ; corrigée au Lot B pour refuser uniquement les incompatibilités actives du run visé. |
| 7 | `application/edition_preview.py` | aperçu | réutilise la Review (donc le point 1). |
| 8 | `models/edition_publication.py` | `publication_manifests` | colonne `batch_id` à rendre nullable / retirer pour V2. |

Contrats déjà favorables : `build_edition_document()` (liste ordonnée de références d'artefacts), `PublicationManifestEntryV1` (déjà run/génération/artefact/version/hash par entrée), `EditionDocumentV2`, exclusions liées à une décision de Review.

À relever avant le Lot A (non lu à ce stade) : `edition_preview.py`, `edition_document.py`, `edition_review.py::_build_item` (lignes 995-1072) et la table `edition_production_batch_items`.

## 2. Décisions (réponses aux 10 questions de la Phase 1)

1. **Batch** = unité d'exécution et de provenance. Jamais autorité sur le contenu d'une édition.
2. **Composition** = projection persistée, versionnée, mutable : `edition_compositions` (id, edition_id, version, status, actor, timestamps) + `edition_composition_entries` (slot ordonné) + journal append-only `edition_composition_events`. Unicité `(composition, subject)` et `(composition, position)`.
3. **Version exacte** : une entrée *incluse* épingle `run_id, pipeline_generation, artifact_id, artifact_version, input_hash`. Si ce run produit une nouvelle version, l'entrée devient `superseded_artifact_available` (diagnostic) ; le changement de pin est une mutation explicite de composition (ou une option « suivre la version courante » **explicite, jamais implicite**). Un run ultérieur du même sujet ne remplace pas le pin. Une décision de Review reste liée à l'identité exacte.
4. **Sujet non produit** : entrée avec `disposition = deferred`, sans artefact. Trois dispositions distinctes : `included`, `deferred`, `excluded` (+ `review_decision_id` obligatoire pour `excluded`). L'absence de ligne n'est jamais une exclusion ; la validation la signale `subject_unaddressed`.
5. **Publiabilité après correction** : calculée par la validation de composition à partir de l'artefact épinglé (statut `VERIFIED`, non `STALE`, compatibilité réparation existante `publication_is_compatible_with_current_effective_inputs`) et des réparations bloquantes *des seuls sujets inclus*.
6. **Nouvelle Discovery** : n'écrit rien sur runs/artefacts/composition. Le contrat de composition avait déjà le champ `discovery_update_available`, mais le service le laissait à `null`; il est calculé en lecture seule depuis l'identité Discovery canonique et les IDs de candidats du snapshot d'entrée contre le snapshot actif. `production_validity`, `discovery_update_available`, `composition_eligibility` sont trois champs orthogonaux de la lecture.
7. **Acceptation** : action humaine authentifiée (`actor_id`), sur `expected_composition_version`, sous verrou d'édition, reposant sur une validation sans bloquant.
8. **Manifeste V2** : pas de `batch_id` racine. Racine = `edition_id`, `composition_id`, `composition_version`, `entries[]` (position, subject, run, génération, artefact, version, hash, `provenance.batch_id` nullable, décision de Review), `exclusions[]` (subject + décision), acteur, instant, `content_sha256`. Provenance batch **nullable** : jamais inventée pour une reprise hors batch.
9. **V1** : lecture/vérification inchangées (classe V1 conservée, discriminée par `schema_version`). Aucune migration de données V1 → V2. Les releases V1 gardent leur PDF.
10. **Statique vs visuel** : l'inspection d'entrées Typst (fichiers du bundle, JSON effectif, diagnostics compilateur, hashes) est *statique et sans recompilation* ; les défauts de layout restent du ressort de `typst query` + inspection PDF. Les deux sont reliés par `render_id` + SHA-256 du PDF, jamais fusionnés.

### Décision Recovery (Lot B)
#### Décision implémentée

Review lit uniquement les entrées `included` de la composition courante et épingle chaque entrée sur `(run_id, pipeline_generation, artifact_id, artifact_version, input_hash)`. Les décisions de Review sont jointes sur cette identité complète ; une décision d'une version antérieure reste dans l'historique, mais ne devient pas la décision effective du nouveau pin. La lecture est un statement SQL set-based, sans requête par sujet.

Une composition absente produit `items=[]`, `composition_version=null` et le diagnostic `composition_missing`; `can_accept` est faux. Les sujets non inclus sont signalés par les diagnostics `subject_unaddressed`, `subject_deferred` et `subject_excluded`. Une exclusion sans décision de Review exactement applicable ajoute `exclusion_decision_missing`. Les réparations des entrées incluses alimentent `can_accept`; celles des sujets reportés ou exclus restent visibles dans leurs compteurs de diagnostic sans bloquer l'acceptation.

La reprise vérifie le run visé sous le verrou de création du sujet. Elle refuse un autre run du sujet en `queued` ou `running`, un sibling `running` dans le batch, un batch cible `cancelled` ou `paused`, un autre batch actif lors de la réouverture, ainsi que les ambiguïtés de soumission et générations incompatibles déjà protégées par les services de production. Un batch terminé du run visé peut être rouvert même si des batches terminés plus récents existent : le lien run→batch est exact et la chronologie ne décide pas du contenu. Aucun nouveau batch de récupération n'est créé.

Le Lot B ne lance pas de production et ne crée pas de second mécanisme : les transitions de run et les jobs idempotents existants restent propriétaires de l'exécution.

## 3. Décisions écartées

- *Garder `batch_id` dans V2 « pour compat »* : exactement la fausse compatibilité interdite.
- *Copier les artefacts dans la composition* : seconde vérité documentaire.
- *Composition dérivée « tous les runs READY les plus récents »* : réintroduit une autorité implicite et rend l'ordre non explicite.
- *Étendre le filtre SQL à tous les batches* : corrige le symptôme, pas l'autorité (cf. consigne).
- *Moteur de workflow générique* : hors périmètre.
- *Mettre automatiquement un ancien run en `STALE` lorsque Discovery/Fusion avance* : rejeté, car cela rendrait la chronologie Discovery propriétaire d'un snapshot de production immuable.
- *Rapprocher deux identités par leur titre ou par similarité* : rejeté ; une collision entre Subjects matérialisés est retournée comme conflit métier avec leurs IDs et empêche la mutation Fusion.
- *Créer une seconde API de correction ou écrire dans un workspace temporaire historique* : rejeté ; les inspections restent en lecture seule et les corrections réutilisent le canal de révision ciblée existant.
- *Ajouter une migration Alembic* : rejeté tant que `0001_baseline` reste mutable avant le gate AW-024.

## 4. Schéma (baseline mutable `0001_baseline`, aucune migration additionnelle)

```
editions 1─* edition_compositions (version, status)
edition_compositions 1─* edition_composition_entries
    (subject_id, disposition, position?, production_run_id?, pipeline_generation?,
     artifact_id?, artifact_version?, artifact_input_hash?, document_sha256?,
     provenance_batch_id?, review_decision_id?)
edition_compositions 1─* edition_composition_events (append-only, actor, correlation, idempotency_key)
publication_manifests (+ schema_version, composition_id?, composition_version?; batch_id nullable, interdit en V2)
```

La lecture de validation expose séparément `production_validity`,
`discovery_update_available` et `composition_eligibility`. Le signal Discovery est calculé en
lecture seule depuis le snapshot d'entrée immuable du run épinglé et le snapshot Discovery actif.
La comparaison suit l'identité Discovery canonique et les IDs exacts des candidats; elle ne lit
jamais un titre pour décider de l'identité et n'altère aucune ligne de production.

## 5. État final du chantier

- **Lot A** — composition canonique versionnée, entrées épinglées, événements append-only et
  invariants persistés dans la baseline mutable.
- **Lot B** — Review set-based sur les entrées incluses et Recovery sur le run exact; aucune règle
  ne dépend du dernier batch de l'édition.
- **Lot C** — manifestes V2 sans `batch_id` racine, assembly depuis les pins gelés, retries
  idempotents et lecture des manifestes/releases V1 historiques.
- **Lot D** — snapshots vérifiés des entrées Typst, render-data et diagnostics en lecture seule,
  avec sélection stricte par IDs canoniques.
- **Lot E** — console de composition et inspection d'édition intégrées à l'interface opérateur.
- **Lot F** — preuves S3, S7, S11 et retry d'assemblage renforcées; documentation opérationnelle
  et note de déploiement préparées.

`discovery_update_available` est maintenant calculé sans réécrire l'état : le cas courant A reste
`READY`, son snapshot, son artifact et ses hashes restent identiques, et aucune tâche de
production n'est créée par l'arrivée d'une nouvelle information. Un nouveau run exige une
commande explicite.

La validation exacte, les IDs des fixtures A/B/C et les critères d'acceptation 1–14 sont consignés
dans `2026-10-12-multibatch-composition-proof-journal.md`. La procédure proposée pour une base
existante est dans `2026-10-12-multibatch-composition-deployment-note.md`.

## 6. Limites connues

- `discovery_update_available` n'est qu'une indication de nouvelles preuves par identité et
  candidats exacts. Il ne rend pas une production invalide, ne crée pas de run et ne remplace pas
  une décision éditoriale.
- Les layouts restent évalués par compilation/requête Typst puis inspection PDF. Une lecture
  statique des entrées ne prouve pas l'apparence visuelle.
- Les avertissements Typst des rendus réussis ne sont pas tous persistés par l'ancien modèle de
  rendu; l'inspection indique explicitement quand ils sont indisponibles.
- Aucun accès, restamp, upgrade ou écriture n'a été effectué sur la base réelle.
