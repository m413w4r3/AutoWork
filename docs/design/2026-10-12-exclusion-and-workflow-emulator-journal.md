# Journal — exclusion de composition et émulateur de workflow

Date d’origine : 2026-10-12. Mise à jour de clôture : 2026-10-13.
Ce journal sépare l’état du défaut initial, les correctifs successifs et les
preuves finales du Lot D.

## 1. État de départ

- Le défaut d’exclusion est observé sur `8647207` (`feat: compose multi-batch
  editions from versioned composition`). C’est l’état avant le correctif.
- `4a1fa71` corrige l’exclusion (Lot B) ; `374f1ce` restaure
  `exclusions=tuple(exclusions)`, capturé à tort comme `exclusions=()` pendant
  les preuves de mutation du Lot C.
- `9702c70` ajoute l’émulateur W1/W2 et ses marqueurs ; `0a62337` corrige Review
  afin qu’un sujet différé bloque `can_accept` ; `5effbfd` ajoute W3, W4, W6 et
  W8.
- `86657f3` ajoute le Makefile, les gates CI, les scripts et ce journal ;
  `000c658` ajoute W5/W7/W9 et Discovery réelle dans W1 ; `71068be` conserve le
  code `subject_deferred` à côté de son libellé ; `73380c4` ajoute W10 et le job
  CI navigateur ; `df857f9` durcit W5 après l’audit par mutation.
- Le HEAD de départ de cette clôture est `df857f9`, arbre propre. Les commits
  listés sont locaux et rien n’a été poussé. Les changements de ce journal sont
  **à committer**.
- La pièce jointe de mission n’était pas lisible depuis les sessions
  déléguées. Les faits recopiés dans le brief et vérifiables dans le dépôt sont
  rapportés ci-dessous ; les exigences qui dépendraient uniquement du texte
  complet restent marquées **À CONFIRMER**.

## 2. Cause racine

Cinq divergences de contrat décrivaient l’exclusion :

1. Dans `EditionReviewService.from_rows()` (`backend/src/cti_app/application/edition_review.py`),
   `Review.items` représentait les documents inclus. Une exclusion lue depuis
   la composition n’était pas un élément de cette collection. Le modèle de
   lecture `list_for_edition()` se trouvait dans
   `backend/src/cti_app/infrastructure/database/repositories/publication_review.py`.
2. Dans `EditionReviewService.decide()` du même fichier, la recherche du sujet
   s’effectuait dans les lignes de Review renvoyées comme items. Un sujet
   exclu ne pouvait donc pas être traité comme une décision associée à
   l’exclusion.
3. Dans `freeze` (`_resolve_current_publication_inputs()` dans
   `backend/src/cti_app/application/edition_publication.py`), le gel construisait
   `review_by_subject` depuis `review.items`, puis cherchait aussi l’exclusion
   dans cette table. Il échouait avec
   `PublicationAcceptanceError("excluded_item_has_no_decision")`.
4. Dans `handleExclude()` (`frontend/src/features/edition-workflow/CompositionConsole.tsx`),
   le chemin en deux étapes enregistrait une entrée `excluded` sans l’épingle
   exacte ni décision liée. La décision côté API exigeait une identité de
   run/artifact cohérente ; le geste échouait avec
   `CompositionReferenceError("review_decision_identity_mismatch")`.
5. Dans `PublicationReviewDecisionRow`
   (`backend/src/cti_app/infrastructure/database/models/publication_review.py`),
   `production_run_id` et `pipeline_generation` étaient `NOT NULL`. Une
   exclusion d’un sujet sélectionné mais jamais produit ne pouvait pas être
   représentée.

La CI pouvait rester verte parce que les tests unitaires construisaient
directement des `EditionReviewReadItem` exclus, sans employer le read model
PostgreSQL qui sépare les scopes. Le test
`test_review_acceptance_requires_an_included_item()` dans
`backend/tests/test_publication_review.py` vérifie ces objets fabriqués. Les
parcours Playwright mockés simulaient les routes avec `route.fulfill()` et un
statut HTTP réussi ; ils ne validaient pas l’écriture PostgreSQL et le gel du
manifeste pour A inclus, B exclu, C inclus. W10 apporte maintenant un test
navigateur séparé qui exerce le vrai backend PostgreSQL, dans
`frontend/e2e-real/exclusion-real-backend.spec.ts`.

## 3. Preuve rouge → verte

Le test PostgreSQL de référence est
`backend/tests/integration/test_edition_exclusion_publication_postgres.py`.
Il crée un vrai dépôt, une vraie unité de travail et relit Review depuis la
composition persistée. Sur `8647207`, l’exécution réelle a donné
`review_decision_identity_mismatch` à `EditionCompositionService.exclude()`
(geste console) et `excluded_item_has_no_decision` à
`EditionPublicationService.accept` (exclusion épinglée).

Après `4a1fa71`, réparé par `374f1ce`, les fonctions présentes dans le fichier
couvrent notamment :

- `test_two_step_exclusion_without_identity_pin_is_rejected`
- `test_exclude_subject_atomically_publishes_a_c_and_exclusion_b`
- `test_exclude_selected_b_without_a_run_is_publishable`
- `test_deferred_subject_blocks_publication_as_incomplete`
- `test_exclusion_is_blocked_after_run_retry_until_a_new_decision`
- `test_stale_expected_version_does_not_append_an_exclusion_decision`
- `test_exclusion_idempotency_reuses_one_decision_and_rejects_changed_payload`
- `test_reinclusion_keeps_exclusion_history_and_does_not_change_frozen_manifest`
- `test_excluded_b_pinned_to_its_decided_document_is_accepted_and_published`

L’orchestrateur a exécuté cinq fichiers via `make test-integration` :
`tests/integration/test_publication_review.py`,
`tests/integration/test_edition_exclusion_publication_postgres.py`,
`tests/integration/test_edition_composition_postgres.py`,
`tests/integration/test_edition_multibatch_publication_postgres.py` et
`tests/integration/test_publication_freeze_retry.py` : **20 passed**. Le fichier
d’exclusion seul contient neuf tests. Les noms exacts incluent
`test_two_step_exclusion_without_identity_pin_is_rejected`,
`test_exclude_subject_atomically_publishes_a_c_and_exclusion_b`,
`test_exclude_selected_b_without_a_run_is_publishable`,
`test_deferred_subject_blocks_publication_as_incomplete`,
`test_exclusion_is_blocked_after_run_retry_until_a_new_decision`,
`test_stale_expected_version_does_not_append_an_exclusion_decision`,
`test_exclusion_idempotency_reuses_one_decision_and_rejects_changed_payload`,
`test_reinclusion_keeps_exclusion_history_and_does_not_change_frozen_manifest`
et `test_excluded_b_pinned_to_its_decided_document_is_accepted_and_published`.

Un second défaut, révélé par W3, concernait un sujet `deferred` :
`Review.can_accept` valait `True`, alors que le gel renvoyait HTTP 409
`composition_incomplete`. La console proposait donc « Accepter », puis
échouait. Le test rouge → vert est
`test_w3_deferred_subject_review_does_not_offer_acceptance` ; le correctif est
`0a62337`.

**Décision produit à confirmer par le responsable :** le test LOT 21
`test_composition_review_scopes_repairs_and_reports_nonincluded_subjects`
affirmait que Review pouvait accepter en présence d’un sujet reporté. Il a été
adapté pour que le report bloque ; un témoin vérifie toujours qu’une réparation
sur un sujet exclu ne bloque pas l’acceptation. L’alternative est que Review
ignore les reports et que seul le gel refuse la publication, ce qui conserverait
l’état antérieur de LOT 21 mais laisserait la console proposer une action qui
échoue ensuite.

## 4. Contrat corrigé C1–C7 et DDL live

Les sept responsabilités C1–C7 ci-dessous sont des descriptions fonctionnelles
vérifiées contre le modèle, le service, la route et le correctif.

1. **C1 — Identité nullable** : run et génération peuvent être absents pour
   une exclusion d’un sujet jamais produit.
2. **C2 — Garde de schéma** : `ck_publication_review_run_identity` n’autorise
   cette absence que pour une exclusion sans identité d’artifact.
3. **C3 — Lecture Review** : les exclusions sont exposées dans
   `EditionReview.exclusions`, séparément de `items`.
4. **C4 — Gel** : `_resolve_current_publication_inputs()` vérifie l’applicabilité
   de l’exclusion depuis cette collection et l’écrit comme exclusion du
   manifeste V2.
5. **C5 — Mutation atomique** : `EditionCompositionService.exclude_subject()`
   ajoute la décision et remplace l’entrée dans la même mutation transactionnelle.
6. **C6 — Rejeu concurrent** : idempotence, contrôle de version et conflits
   typés (`CompositionIdempotencyConflict`, `CompositionVersionConflict`)
   protègent les appels répétés ou obsolètes.
7. **C7 — Surface API et console** :
   `POST /api/editions/{edition_id}/composition/exclusions/{subject_id}` porte
   le motif, l’identité et la mutation dans un appel ; la console l’utilise.

Le modèle SQLAlchemy donne la contrainte et le nom exacts. DDL correspondant
à appliquer sur une base live, **NON APPLIQUÉ** :

```sql
ALTER TABLE publication_review_decisions
  ALTER COLUMN production_run_id DROP NOT NULL;

ALTER TABLE publication_review_decisions
  ALTER COLUMN pipeline_generation DROP NOT NULL;

ALTER TABLE publication_review_decisions
  ADD CONSTRAINT ck_publication_review_run_identity CHECK (
    (production_run_id IS NULL AND pipeline_generation IS NULL
      AND document_artifact_id IS NULL
      AND document_artifact_version IS NULL
      AND document_input_hash IS NULL
      AND decision = 'exclude')
    OR (production_run_id IS NOT NULL AND pipeline_generation IS NOT NULL)
  );
```

Le schéma de test utilisé par les tests d’intégration est créé à neuf depuis
`0001_baseline`. Aucun changement live ni restamp n’a été exécuté.

## 5. Émulateur et niveau de preuve

`WorkflowDriver.build_emulator_app()` construit une app FastAPI de test avec
les routers officiels et `ASGITransport`. L’app est recomposée, pas
`main.app`. Les services et repositories PostgreSQL sont réels là où le tableau
le précise ; les fixtures SQL et doubles restent visibles ci-dessous.

| Scénario | Niveau et chemin exercé | État préparé, doubles et limites |
|---|---|---|
| Garde réseau (`test_network_guard.py`) | Unitaire, sans DB : bloque sockets, DNS et HTTP externes. | Ne couvre pas les sous-processus, le code natif ni des sockets déjà ouvertes. |
| W1 (`workflow_full`) | DiscoveryService réel piloté par réponses locales via ScriptedModelGateway ; Fusion cumulative réelle avec planner déterministe ; Selection par route, jobs de production, Composition, Review, Accept et Assembly réels. | Les réponses de modèle/source sont déterministes ; modèle Bridge/distant absent, compilateur D2 factice. Typst/PDF non exercés par W1. |
| W2 (`workflow_fast`) | Routes de composition et d’exclusion, Review, manifeste V2 et release réels. | Helper SQL insère directement les runs `READY` et artefacts `VERIFIED` ; pas de transition de production. |
| W3 — exclusion sans run (`workflow_fast`) | Discovery préparée, Fusion et Selection réelles ; A passe par les jobs déterministes réels ; Review et gel refusent le report B, puis la route persiste l’exclusion sans `production_run_id`. Assembly réel exclut B. | B n’a pas de run/job/model run. Vérifie `checked>0` pour l’historique et le blob assemblé. Discovery est fixturée. |
| W3 — Review différé (`workflow_fast`) | Test séparé `test_w3_deferred_subject_review_does_not_offer_acceptance` : la route Review ne propose pas l’acceptation pendant le report. | B reste `deferred`, pas exclu. |
| W4 (`workflow_fast`) | Après production épinglée, vraie réconciliation Fusion cumulative rend `discovery_update_available=true`, sans changer l’identité produite ni la Composition épinglée. | Entrées Discovery D1/D2 fixturées ; production de départ `READY` préparée dans PostgreSQL. |
| W5 (`workflow_full`) | A et B sont produits par les vrais jobs déterministes ; la correction d’enrichissement crée une nouvelle PUBLICATION. Le vrai Review vérifie pin, diagnostics, décisions et codes d’acceptation. | Décisions de test insérées par le repository réel ; un état Review temporaire est fixturé pour isoler le garde. Preview remplacée par un double ; aucune preuve Typst/PDF. |
| W6 (`workflow_fast`) | Composition, Review, acceptation, Assembly, concurrence et reprise réels ; relit les hashes historiques avec `checked>0`. | Runs `READY`/artefacts `VERIFIED` insérés ; interruption de l’écriture blob simulée. |
| W7 (`workflow_full`) | Vraie Fusion cumulative met une proposition conflictuelle en revue ; la résolution `SEPARATE` passe par `FusionService.resolve_review`. Relit Composition, runs et artefacts inchangés. | A/B et production préparés ; la revue Fusion n’a pas de route HTTP et est donc prouvée au niveau service. |
| W8 (`workflow_fast`) | Routes Composition/acceptation et Assembly réelles : conflit de version, puis release avec B exclu. | Runs `READY` et artefacts `VERIFIED` insérés directement. |
| W9 (`workflow_full`) | Composition, Review, acceptation, Assembly puis vrai rendu Typst, inspection Poppler et relecture SQL des liens release/manifeste/document/rendu. | Trois publications synthétiques minimales insérées. Typst, templates, polices et Poppler manquants font échouer explicitement le test. La qualité visuelle n’est pas évaluée. |
| W10 (job `workflow-browser`) | Playwright visite la console avec serveur/backend réels : GET composition, POST exclusion de B, Review, puis acceptation A/C ; vérification SQL du manifeste et de la décision. | PostgreSQL de service isolé. La page montre « Exclu », le motif, l’acteur, l’UUID de décision et la version de composition. Job séparé de l’émulateur. |

La collecte réelle trouve **7 tests `workflow_fast`**, **4 tests
`workflow_full`**, soit **11** dans `workflow_fast or workflow_full`. Les 7 FAST
sont le garde réseau, W2, les deux tests W3, W4, W6 et W8. Les quatre FULL-only
sont W1, W5, W7 et W9. Make impose `WORKFLOW_FAST_MIN=7` et
`WORKFLOW_FULL_MIN=11` ; la CI impose `--min-count 4` à son étape FULL-only.
W10 est collecté séparément dans le job navigateur.

Les node IDs mesurés sont :

```text
tests/workflow_emulator/test_network_guard.py::test_network_guard_blocks_external_socket_dns_and_httpx
tests/workflow_emulator/test_w1_progressive_abc.py::test_w1_progressive_abc_uses_real_selection_and_production_transitions
tests/workflow_emulator/test_w2_inclusion_exclusion.py::test_w2_inclusion_exclusion_is_persisted_through_release
tests/workflow_emulator/test_w3_unproduced_subject_excluded.py::test_w3_unproduced_subject_is_explicitly_excluded
tests/workflow_emulator/test_w3_unproduced_subject_excluded.py::test_w3_deferred_subject_review_does_not_offer_acceptance
tests/workflow_emulator/test_w4_discovery_after_production.py::test_w4_discovery_after_production_keeps_pinned_inputs_publishable
tests/workflow_emulator/test_w5_late_correction.py::test_w5_late_correction_requires_explicit_repin
tests/workflow_emulator/test_w6_concurrency_and_resume.py::test_w6_concurrency_and_resume
tests/workflow_emulator/test_w7_discovery_fusion_conflicts.py::test_w7_selected_discovery_fusion_requires_human_resolution
tests/workflow_emulator/test_w8_stale_composition_version.py::test_w8_accept_rejects_stale_composition_version
tests/workflow_emulator/test_w9_real_typst_render.py::test_w9_real_typst_render_contains_frozen_article_titles
```

Limites de fidélité : ASGI recomposé, pas `main.app` ; aucun appel à Bridge ni
modèle distant ; D2 est factice. W2, le départ de W3, W4, W6 et W8 partent de
runs `READY` insérés plutôt que d’exercer une génération complète. W3 produit
A par vrais jobs mais B n’est pas produit. Discovery est fixturée dans W3/W4 ;
W1 exécute le vrai service Discovery avec réponses déterministes locales. W7
prouve la revue Fusion au niveau service, sans route HTTP. Le garde réseau ne
prouve pas les sockets en processus enfant ou en code natif.

## 6. Audits indépendants par mutation

`/tmp/lotc-audit-report.md` relève **0 bloquant** et **3 majeurs** corrigés avant
`9702c70` : F1, docstring W1 qui surévaluait Discovery ; F2, garde réseau/DSN
local insuffisants ; F3, skip silencieux. Les mutations M1–M7 sur W1/W2 ont
toutes été détectées : Review sans exclusion, exclusion absente du manifeste,
décision non persistée, B à la fois inclus et exclu, motif altéré, artefact A
invalidé par Fusion, ou job créé par Composition. Elles ont été restaurées.

`/tmp/c3-mutation-report.md` : **15/15 mutations réalisables tuées** pour
W3/W4/W6/W8. X-b n’est pas une mutation survivante : le trigger append-only de
PostgreSQL rejette l’UPDATE de `publication_manifests` avant l’assertion
applicative.

`/tmp/c4-mutation-report.md` : audit W5/W7/W9/W1-Discovery/W10, initialement
**15 tuées sur 18**. Les trois survivantes W5-b/W5-c/W5-d ont été traitées dans
`df857f9`. W5-d est une mutation équivalente : la PUBLICATION v1 est déjà
`STALE` après la correction officielle. Les mutations non équivalentes qui
annulent le blob historique ou altèrent `input_hash` sont tuées. L’auteur a
rejoué les mutations W5 selon la procédure de sauvegarde/restauration ;
`git diff HEAD --stat -- backend/src` est vide.

## 7. Incidents

- `4a1fa71` a capturé une mutation temporaire `exclusions=()` ; `374f1ce` a
  restauré `exclusions=tuple(exclusions)`.
- Collision de plugin : `pytest_plugins = ["tests.integration.conftest"]` dans
  le `conftest.py` workflow provoquait `ValueError: Plugin already registered
  under a different name: tests.integration.conftest` quand les deux dossiers
  étaient collectés ensemble. Corrigé avant `9702c70` en important les fixtures
  par nom ; ce n’était pas une collision des modules Typst.
- Trois processus pytest orphelins ont bloqué 7–10 minutes sur
  `test_goodware.py::test_measurement_prepares_once_and_reuses_uuid_and_fingerprint` ;
  non reproduit ensuite.
- `test_analysis_subprocess.py` et Playwright (`connect EPERM 127.0.0.1`) ont
  bloqué dans des sessions déléguées sandboxées et passé hors sandbox. Lors de
  cette clôture, une première exécution backend sandboxée a aussi échoué sur le
  subprocess puis cessé d’avancer dans `test_goodware`; relancée hors sandbox,
  la suite a passé.
- `0a62337` a remplacé le code visible `subject_deferred` par un libellé seul,
  cassant le test E2E mocké `frontend/e2e/multibatch-composition.spec.ts:514`.
  Vitest ne l’avait pas détecté. `71068be` affiche de nouveau le code à côté du
  libellé. Leçon : lancer aussi `pnpm test:e2e` après un changement frontend.
- Le spec W10 cassait `pnpm lint` (TypeScript/ESLint) et `pnpm test` (Vitest
  collectait le spec Playwright). `73380c4` a corrigé `tsconfig.node.json` et
  `vite.config.ts`.
- Un programme étranger occupait le port `8765`; W10 utilise maintenant
  `18765`. Les trois exécutions de cette clôture ont gardé ce port par défaut et
  n’ont pas touché `8765`.
- Des bases `cti_browser_*` étaient restées après un arrêt par signal. Le
  serveur s’arrête maintenant via son endpoint de contrôle ; aucune base de
  test résiduelle n’a été observée après le nettoyage.
- Sans `TMPDIR` sous `$HOME`, 23 tests Typst/rendu hors workflow échouaient avec
  le Typst snap (`/snap/bin/typst`). Cette clôture a utilisé
  `TMPDIR=$HOME/typst-tmp` ; le backend complet a passé.
- Le premier appel de mesure de cette clôture a échoué avant les tests, car le
  cache UV par défaut était en lecture seule ; les tentatives sandboxées ne
  pouvaient pas ouvrir le socket PostgreSQL local. Les six profils ont passé
  avec `UV_CACHE_DIR=/tmp/lotd-uv-cache` et l’accès autorisé au seul DSN local
  de test.

## 8. Mesures et arbitrages

Commande reproductible, avec le conteneur de test déjà actif :

```sh
export TEST_POSTGRES_ADMIN_DSN=postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres
UV_CACHE_DIR=/tmp/lotd-uv-cache TMPDIR="$HOME/typst-tmp" make measure-workflow
```

`measure-workflow` fait trois exécutions de chaque profil, garde les sorties
`--durations=0`, JUnit, traces JSONL, temps Make et RSS pytest. Il ne démarre ni
n’arrête PostgreSQL. Les durées ci-dessous sont secondes, min / médiane / max.
Un scénario partagé apparaît six fois (trois FAST et trois FULL) ; FULL-only,
trois fois.

| Node ID / mesure | Runs | Min / médiane / max |
|---|---:|---:|
| Garde réseau | 6 | 0,08 / 0,08 / 0,09 |
| W1 — Discovery/Selection/jobs réels | 3 | 3,04 / 3,18 / 3,41 |
| W2 — inclusion/exclusion | 6 | 0,59 / 0,63 / 0,67 |
| W3 — Review différé | 6 | 1,08 / 1,15 / 1,23 |
| W3 — exclusion sans run | 6 | 1,38 / 1,57 / 1,62 |
| W4 — Discovery après production | 6 | 1,29 / 1,38 / 1,51 |
| W5 — correction tardive | 3 | 2,41 / 2,64 / 2,66 |
| W6 — concurrence et reprise | 6 | 1,36 / 1,43 / 1,75 |
| W7 — conflits Fusion | 3 | 1,07 / 1,19 / 1,45 |
| W8 — version obsolète | 6 | 0,58 / 0,66 / 0,85 |
| W9 — rendu Typst réel | 3 | 1,64 / 1,66 / 1,73 |
| Setup première fixture DB + Alembic | 6 | 0,50 / 0,55 / 0,65 |
| Make FAST — 7 tests, code 0/0/0 | 3 | 11,70 / 12,01 / 12,01 |
| RSS maximal pytest FAST | 3 | 164 696 KiB |
| Make FULL — 11 tests, code 0/0/0 | 3 | 20,22 / 21,13 / 21,22 |
| RSS maximal pytest FULL | 3 | 363 776 KiB |

Le setup affiché est le `setup` pytest du premier test DB (W2 en FAST, W1 en
FULL) et comprend les autres fixtures de setup. La variation FAST est de 0,31 s
(2,6 % de la médiane) ; FULL varie de 1,00 s (4,7 %). Le setup varie de 0,15 s.
W1 et W7 ont les plus grands écarts individuels (0,37 et 0,38 s). Les runs sont
séquentiels avec PostgreSQL déjà actif ; aucune cause précise de variation n’a
été isolée.

| W10 / mesure navigateur | Résultat |
|---|---|
| Exécution 1 | Make 7,88 s ; Playwright `1 passed (2.8s)`, test 1,4 s ; SQL verification passed. |
| Exécution 2 | Make 7,79 s ; Playwright `1 passed (2.8s)`, test 1,4 s ; SQL verification passed. |
| Exécution 3 | Make 8,05 s ; Playwright `1 passed (2.9s)`, test 1,5 s ; SQL verification passed. |
| Total des trois exécutions | 23,72 s Make ; 8,5 s cumulés pour les invocations Playwright. |

Coûts de référence mesurés à chaud : `make backend-sync` **0,02 s** ;
`make test-backend` **52,59 s** au total (**50,88 s pytest**, 3 074 passés,
10 désélectionnés, RSS maximal 398 716 KiB) ; `make test-integration
INTEGRATION_TEST_PATH=tests/integration` **88,89 s** (**85,56 s pytest**,
274 passés, RSS maximal 609 560 KiB). Le DSN fourni a empêché `test-integration`
de posséder ou supprimer le conteneur actif.

L’étape CI FULL-only a aussi été rejouée localement : 4 passés, 7 désélectionnés,
pytest en 9,18 s ; Make `test-integration` en 11,42 s, puis le garde a affiché
`Garde workflow OK: 4 scénario(s) collecté(s), tous présents et passed dans le JUnit.`
Le garde a pris 3,1 s avec `uv run`. À chaud, les deux étapes workflow du job
`backend` représentent donc environ **26,5 s** (FAST Make 12,01 + FULL-only Make
11,42 + garde FULL-only 3,1). Dans l’ancien pytest principal, les médianes
d’appels des 11 tests totalisent environ 15,57 s, plus un setup DB de 0,55 s.
Le coût net estimé du découpage en deux gates est donc **environ 10 s** au-delà
de cette exécution unique, principalement pour un second démarrage de session,
setup DB et garde. C’est une estimation locale, pas une mesure du job GitHub.

Le job `workflow-browser` est à environ 8 s localement avec les dépendances déjà
installées. Le coût froid de `pnpm install` et de `playwright install --with-deps
chromium` sur GitHub n’a pas été mesuré. Le `backend-sync` chaud à 0,02 s ne
permet pas d’estimer le `uv sync` à froid du groupe `analysis` sur un runner.

L’objectif §23 est FAST à moins d’une minute supplémentaire et FULL en quelques
minutes. Les médianes mesurées sont 12,01 s pour FAST et 21,13 s pour le FULL
agrégé : FAST reste environ 48 s sous sa limite et FULL est sous le budget de
quelques minutes. Les résultats reflètent le jeu vert actuel de 11 tests et
doivent être rejoués à l’ajout ou au changement d’un scénario.

## 9. Risques résiduels et non vérifié

- GitHub Actions n’a pas été exécuté. Les suites locales backend et intégration
  ont passé ; le job `frontend` et les jobs GitHub `backend`/`workflow-browser`
  restent à observer sur runner.
- Le pytest principal du job `backend` exclut `workflow_fast` et `workflow_full` ;
  FAST puis FULL-only ont des étapes séparées `if: always()` et une publication
  d’artefacts à l’échec. W10 est un job séparé, avec PostgreSQL et installation
  navigateur propres.
- Le commentaire de `.github/workflows/ci.yml` au-dessus de l’étape FULL-only
  annonçait « only W1 » et « floor is 1 » alors que le code applique
  `--min-count 4` ; il a été aligné sur le code après la clôture du Lot D.
- Les installations froides UV/Playwright du runner ne sont pas mesurées. Les
  sessions déléguées sandboxées ne reproduisent pas l’environnement utilisateur.
- Discovery est fixturée dans W3/W4 ; une partie de FAST (W2, départ W3, W4,
  W6, W8) commence sur des runs `READY` insérés. W7 exerce la résolution Fusion
  au niveau service, sans route HTTP.
- Le DDL de la section 4 et tout restamp live restent **NON APPLIQUÉS**. Aucune
  base live ni donnée réelle n’a été utilisée. Les lectures d’historiques sont
  testées sur PostgreSQL de test, pas sur les releases d’une base live.
- La qualité visuelle/éditoriale du PDF W9, toutes les permutations de Review /
  Fusion et la décision produit sur les sujets reportés restent à confirmer.
  Le test LOT 21 a été ajusté : un report bloque maintenant `can_accept` ;
  l’alternative est que Review ignore les reports et que seul le gel refuse.
- L’absence de première exécution GitHub et le coût froid des dépendances sont
  les points opérationnels restants. Les scénarios
  W5/W7/W9 et W10 sont présents et ont passé localement.

## 10. Critères d’acceptation §31

| # | Critère | État | Preuve / limite |
|---:|---|---|---|
| 1 | Défaut reproduit par un test PostgreSQL réel | OUI | `test_edition_exclusion_publication_postgres.py`, rouge sur `8647207` : `review_decision_identity_mismatch` et `excluded_item_has_no_decision`. |
| 2 | Rouge avant, vert après | OUI | Échecs initiaux ci-dessus ; après `4a1fa71` et réparation `374f1ce`, les cas ciblés passent. |
| 3 | A inclus / B exclu / C inclus, manifeste V2 correct | OUI | Tests PostgreSQL Lot B, W2 et W10 ; W10 relit le manifeste en base. |
| 4 | Exclusion sans run est canonique | OUI | `EditionCompositionService.exclude_subject`, test d’intégration et W3 ; `production_run_id IS NULL`. |
| 5 | Décisions et motifs auditables | OUI | PostgreSQL conserve la décision ; W10 vérifie et affiche motif, acteur et UUID. |
| 6 | Review lit les exclusions hors de `Review.items` | OUI | `EditionReview.exclusions` et tests de lecture/gel ; `items` contient les inclus. |
| 7 | Frontend exclut et montre le résultat exact | OUI | W10 affiche `Exclu`, motif, acteur, UUID, version de composition et état acceptable ; `subject_deferred` reste affiché avec son libellé. |
| 8 | Conflits de version sûrs et idempotents | OUI | Tests Lot B, W6 et W8 ; conflits typés et rejeu idempotent. |
| 9 | Émulateur exécute de vraies transitions | PARTIEL | W1/W5 exécutent Selection/jobs et corrections réelles ; plusieurs FAST partent de runs `READY` insérés. |
| 10 | Fournisseurs simulés sans réseau | OUI | Garde réseau et providers déterministes ; limites des sous-processus et code natif documentées. |
| 11 | FAST local et CI | PARTIEL | FAST local 7/7 avec garde JUnit ; CI GitHub non exécutée. |
| 12 | FULL Typst réel et navigateur/backend réel | PARTIEL | W9 et W10 passent localement ; exécution CI GitHub non vérifiée. |
| 13 | Coûts mesurés | OUI | Six profils, trois W10, sync chaud et suites backend/intégration mesurés §8. |
| 14 | Aucun scénario requis sauté ou absent | OUI | `WORKFLOW_EMULATOR_REQUIRED=1`, garde JUnit et planchers Make 7/11 ; CI FULL-only `--min-count 4`. |
| 15 | CI entière verte | NON VÉRIFIÉ | Local : backend 3 074 passés + 10 désélectionnés, intégration 274 passés ; frontend rapporté par l’orchestrateur : 267 Vitest, 12 E2E mockés et W10. GitHub Actions non exécuté. |
| 16 | Aucune base/donnée réelle modifiée | OUI | Seulement PostgreSQL de test local ; DDL live non appliqué. |
| 17 | Manifests/releases historiques lisibles | PARTIEL | Hash historiques avec `checked>0` dans W6 et tests Lot B ; base live non testée. Les colonnes de run/génération deviennent nullable, lecture historique conservée. |

### À faire par le responsable

- Appliquer le DDL live après vérification finale contre le modèle de la section 4.
- Confirmer la décision produit sur le blocage Review d’un sujet reporté.
- Pousser les commits puis observer la première CI GitHub (`backend`, `frontend`,
  `workflow-browser`).
- Relever `WORKFLOW_FAST_MIN`, `WORKFLOW_FULL_MIN` et le `--min-count` FULL-only
  à chaque ajout de scénario.
