# Journal de preuves — composition éditoriale multibatch

Date : 2026-10-12 · Branche : `chantier/multibatch-composition` · Lot F

## Scénario A/B/C (S1)

Test PostgreSQL jetable :
`backend/tests/integration/test_edition_multibatch_publication_postgres.py::test_multibatch_abc_publication_freezes_and_renders_ordered_release`.
Les trois articles proviennent de trois batches et sont gelés, assemblés puis rendus dans l'ordre
éditorial A/B/C. Le test écrit également le JSON de preuve avec les mêmes IDs et hashes que le
manifeste et le release. Aucun modèle réel n'est appelé.

Fixture de cette exécution : édition `f9ae2e8d-6c86-4db7-affc-09b6969dced3`.

| Sujet | Subject ID | Run ID | Batch ID | Artifact PUBLICATION (v1) | `input_hash` |
| --- | --- | --- | --- | --- | --- |
| A | `7b5786f9-a296-4ad3-a2d9-b7ee2723db80` | `f594d812-86ca-42c1-be6f-3dfa81e08057` | `fb43eb10-beb0-4c2f-bf37-14570d9e8865` | `c5c68dd8-1323-4462-bb2e-acc990a931e5` | `e9991ad270989f519db30f91f46acc934d44b7c99e43dd04897fb8e325a77a67` |
| B | `f3f9eedd-131e-420d-95f9-60a7c79f345a` | `1413c0c3-aa48-4416-9d4a-9c940cae2031` | `f01b5f72-8da5-4887-bdc5-38664538cb75` | `b97a0e82-e2d6-4458-8547-8bae455dcc56` | `c33570e763dd5c73bbcea86ec571cd2ea1846a7a39f795dc3b449d6b3a20c4cc` |
| C | `6412c89e-3698-4e72-9911-77b8aa854d17` | `57d2f621-0705-4363-9650-31e4a20d8360` | `42c8d448-979a-4f5c-8dfb-106eea1d8434` | `57430d75-6cc3-45b8-be87-d7c877a2b540` | `f2eace7aeb8e2340eadf65e6f555faeb6a1e6fd4f66e53deaeb89bfc3599acf2` |

Le test a passé une fois et le module contenait un autre test non sélectionné. Le fichier de
sortie éphémère était `/tmp/autowork-lotf-proof/test_multibatch_abc_publicatio0/multibatch-composition-proof.json`.
Le test frère `test_multibatch_publication_order_is_independent_of_production_order` inverse l'ordre
de production et vérifie que l'ordre de composition reste indépendant.

## Scénarios complétés au Lot F

- **S3 additive** — `test_later_discovery_and_rename_never_rewrite_an_existing_run` persiste A en
  `READY`, son snapshot, artifact vérifié, hashes et pin de composition. Une Discovery/Fusion
  ultérieure rend `discovery_update_available=true`; le run reste valide et éligible, ses données
  restent identiques et aucun batch, job, run modèle ou dispatch de production n'est ajouté.
  Une nouvelle production de A reste une commande explicite. Le test PostgreSQL passe : 6 tests du
  module.
- **S7 Fusion** — `test_selected_subject_conflict_leaves_active_snapshot_unchanged` conserve les deux
  runs historiques, retourne le conflit métier avec les deux `subject_ids` et laisse le snapshot
  actif intact; l'opérateur garde ensuite explicitement les deux entrées distinctes. Le test
  PostgreSQL passe. `test_manual_merge_and_split_after_production_preserve_historical_run_identity`
  vérifie aussi que merge/split ne réécrit pas les identités de run/artifact historiques. Le module
  Fusion passe 8 tests.
- **S11 correction** — `test_postgres_localized_diagram_correction_and_rerender_loop` part d'un défaut
  `block_too_small`, lit le document canonique, les entrées/render-data Typst et les diagnostics,
  sélectionne la cible exacte, génère un preview, applique avec les fences, puis compare les hashes
  avant/après. Aucun workspace n'est modifié et aucun appel modèle n'est nécessaire à la boucle.
  Le test compile réellement avec Typst puis inspecte le PDF; il passe avec `TMPDIR` dans le
  workspace (`.tmp/typst`) pour le lanceur Typst snap.
- **S6 compléments** — `test_postgres_rejects_artifact_from_another_edition_and_detects_later_change`
  prouve l'édition exacte; `test_duplicate_subjects_are_rejected_and_reorder_preserves_production_pins`
  prouve l'unicité du sujet; `test_assembly_retry_after_blob_write_interruption_keeps_one_frozen_release`
  injecte une interruption après écriture blob et prouve que retry/replay réutilisent le même
  manifeste et release (1 passé, 3 autres tests du module non sélectionnés).
- **S5 / S8 vérifiés** — Recovery garde le run examiné même si un batch terminal plus récent existe
  (`test_a_terminal_newer_batch_does_not_supersede_the_reviewed_run`); les contrôles de validation
  ne mutent pas le batch (`test_the_validation_pass_never_mutates_the_batch`). La chronologie des
  snapshots/release est couverte par
  `test_same_version_publication_snapshots_and_releases_are_chronological`. La lecture V1 reste
  couverte par `test_edition_render_repository_round_trip_and_failed_release_intact`, qui persiste
  un `PublicationManifestV1` et relit son release historique.

## Inspection Typst

- Succès et lecture des sources effectives :
  `test_persisted_inspection_lists_exact_inputs_links_pdf_and_never_executes` et la boucle S11.
- Échec et diagnostic typé : `test_missing_asset_and_template_failure_are_typed_diagnostics` et
  `test_compiler_error_location_is_relative_and_blob_hash_mismatch_is_typed`.
- Les inspections restent en lecture seule. Les décisions de layout s'appuient aussi sur
  `typst query` et le PDF rendu; les tests d'intégration n'utilisent pas de contenu CTI réel.

## Critères d'acceptation 1–14

| # | Preuve |
| --- | --- |
| 1 | Prouvé — S1 et `test_multibatch_publication_order_is_independent_of_production_order`; aucune composition n'est déduite du dernier batch. |
| 2 | Prouvé — `test_postgres_composition_persists_provenance_idempotency_and_append_only_events`; Review lit les pins inclus de la composition. Le dépôt des manifestes a maintenant une méthode explicite `get_latest_manifest_for_edition`; le grep ciblé Review/Publication/Preview ne trouve aucun `get_latest_for_edition`. |
| 3 | Prouvé — S1 : trois `batch_id` distincts dans le tableau ci-dessus et manifeste V2 ordonné. |
| 4 | Prouvé — `test_multibatch_publication_order_is_independent_of_production_order`. |
| 5 | Prouvé — régression S3, run A reste `READY` après Discovery additive. |
| 6 | Prouvé — régression S3, indicateur de nouvelle preuve vrai sans écriture/invalidation; nouveau run explicite. |
| 7 | Prouvé — S11, révision ciblée d'un artifact antérieur et conservation des fences. |
| 8 | Prouvé — `test_composition_change_at_freeze_boundary_writes_no_manifest`, `test_artifact_change_under_freeze_lock_writes_no_manifest` et tests d'idempotence du gel. |
| 9 | Prouvé — S1 compose des artifacts prêts sans gateway modèle; S3 compare les compteurs de tâches, `model_runs` et dispatch avant/après. |
| 10 | Prouvé — tests d'inspection Typst positive et boucle S11 lisent les entrées effectives du rendu. |
| 11 | Prouvé — S11 compile et inspecte le PDF; les diagnostics statiques ne sont pas présentés comme validation du layout. |
| 12 | Prouvé par le périmètre d'exécution — aucun DSN/base réelle n'a été consulté; PostgreSQL d'intégration est le service jetable du Makefile. |
| 13 | Prouvé — test du repository de rendu avec manifeste V1 historique et release intact. |
| 14 | Prouvé — S1 automatise la production, composition, gel V2, assembly et rendu PDF A/B/C dans PostgreSQL jetable. |

## Validation globale

| Cible Make | Résultat final observé |
| --- | --- |
| `make lint` | Passé — Ruff check/format, ESLint et Prettier. |
| `make typecheck` | Passé — mypy (528 fichiers) et TypeScript. |
| `make test-backend` | **3 065 passés**, 0 échec, 0 ignoré. Exécuté hors bac à sable après reproduction d'un blocage de réveil asyncio/threadpool dans le bac. |
| `make test-integration` | **265 passés**, 0 échec, 0 ignoré — PostgreSQL éphémère du Makefile. Les consumers renommés ont aussi leurs reruns ciblés ci-dessous. |
| `make test-frontend` | **264 passés**, 32 fichiers de test, 0 échec. |
| `make test-frontend-e2e` | **12 passés**, 0 échec — Chromium/Playwright, serveur Vite local. Exécuté hors bac après `EPERM` sur la boucle locale en mode bac. |

Reruns ciblés après corrections : migrations `2 passed, 86 deselected`; production reuse
`2 passed, 7 deselected`; publication freeze/retry `4 passed`; atomicité Lot 34 `6 passed`;
consommateurs unitaires du dépôt manifeste `99 passed`; les trois tests initialement en échec
`3 passed`; scénario A/B/C `1 passed, 1 deselected`; scénario S3 `6 passed`; Fusion S7 `8 passed`;
S11 correction Typst réelle `1 passed`; retry d'assemblage `1 passed, 3 deselected`. Les
désélections ne concernent que ces reruns bornés; aucun test n'a été retiré, ignoré ou marqué
`skip`/`xfail`.

La première intégration globale a détecté l'attendu de trigger incomplet et deux consommateurs de
Review utilisant encore l'ancien type liste; ils sont corrigés et la suite intégration complète
passe. Le backend complet a révélé trois attentes de fixtures anciennes : les deux doubles de
manifestes ont été renommés avec le contrat de dépôt explicite, et le test de rendu Typst vérifie
désormais le snapshot JSON canonique des entrées (avec son contenu et son hash exacts).
