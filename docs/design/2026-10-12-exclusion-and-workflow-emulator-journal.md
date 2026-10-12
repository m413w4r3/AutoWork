# Journal — exclusion de composition et émulateur de workflow

Date : 2026-10-12. Ce journal sépare l’état du défaut initial, les correctifs
successifs et l’état du dépôt pendant le Lot D.

## 1. État de départ

- Le défaut d’exclusion est observé sur `8647207` (`feat: compose multi-batch
  editions from versioned composition`). C’est l’état avant le correctif.
- `4a1fa71` corrige l’exclusion auditable ; `374f1ce` restaure
  `exclusions=tuple(exclusions)`, capturé à tort comme `exclusions=()` pendant
  les preuves de mutation du Lot C. `9702c70` ajoute l’émulateur W1/W2 et ses
  marqueurs. `0a62337` corrige Review pour qu’un sujet différé bloque
  `can_accept`. `5effbfd` ajoute W3, W4, W6 et W8. Les fichiers du Lot D sont
  **à committer** ; ils n’ont pas de hash de commit.
- Le HEAD de départ de cette finalisation est `5effbfd`.
- La pièce jointe de mission n’était pas lisible depuis les sessions
  déléguées. Les exigences reproduites dans le brief utilisateur sont
  vérifiées ci-dessous ; seuls les détails dépendant du texte complet qui ne
  sont pas autrement vérifiables restent marqués **À CONFIRMER**.

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
parcours Playwright concernés simulaient les routes avec `route.fulfill()` et
un statut HTTP réussi ; ils ne validaient pas l’écriture PostgreSQL et le gel
du manifeste pour A inclus, B exclu, C inclus. L’emplacement exact du test
Playwright de cette simulation est **À CONFIRMER** : le fichier de mission
mentionne le comportement, mais la recherche locale n’a pas identifié de
scénario Playwright nommé explicitement « exclusion ».

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

L’orchestrateur a exécuté les cinq fichiers
`tests/test_publication_review.py`,
`tests/integration/test_edition_exclusion_publication_postgres.py`,
`tests/integration/test_edition_composition_postgres.py`,
`tests/integration/test_edition_multibatch_publication_postgres.py` et
`tests/integration/test_publication_freeze_retry.py` via
`make test-integration` : **20 passed**. Le fichier d’exclusion seul contient
neuf tests et son exécution isolée avait donné `9 passed in 2.87s`.

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

Le découpage ci-dessous nomme les sept responsabilités du correctif ; les
libellés littéraux de C1–C7 dans la pièce jointe non lisible depuis les
sessions déléguées sont **À CONFIRMER**.

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
les routers officiels, et son client utilise `ASGITransport`. Les services et
repositories concernés sont réels et écrivent dans PostgreSQL. L’app n’est
pas `main.app`.

| Scénario | Niveau démontré | Doubles / limites |
|---|---|---|
| `test_network_guard` | Unitaire : interdiction des sockets, DNS et HTTP externes ; pas de DB | Ne prouve pas les chemins natifs ni les sous-processus. |
| W1 `workflow_full` | Émulation workflow : Fusion réelle, route de sélection, transitions et jobs réels, Review, composition et release | Discovery est fixturée et persistée par repositories ; planner de Fusion, transport source, gateway modèle et compilateur D2 sont déterministes/factices. Pas de compilation PDF/Typst. |
| W2 `workflow_fast` | Émulation workflow : Composition, exclusion API, Review, manifeste et release réels | Runs `READY` et artefacts `VERIFIED` insérés directement par fixture ; pas de transitions de production. |
| W3 — test principal `workflow_fast` | Émulation workflow : Review indique `subject_deferred`, `can_accept=false` et le gel refuse avec `composition_incomplete` ; une décision explicite exclut ensuite B sans run | Discovery fixturée ; Fusion, Selection et production de A réelles. Vérifie aussi avec `checked>0` que la release historique reste inchangée, que B n’a pas de run/job/model run et n’entre pas dans le blob assemblé. |
| W3 — `test_w3_deferred_subject_review_does_not_offer_acceptance` | Émulation workflow : vérifie séparément que Review ne propose pas l’acceptation tant que B est différé | Le sujet reste différé ; ce n’est pas une exclusion. |
| W4 `workflow_fast` | Émulation workflow : D2, nouvelle Fusion cumulative et `discovery_update_available=true` après épinglage de la composition | Les lots Discovery sont fixturés ; la réconciliation cumulative et le merge sont réels. L’identité sélectionnée, le run, l’artefact et l’épingle restent vérifiés. |
| W6 `workflow_fast` | Émulation workflow : concurrence, reprise, acceptation et release ; vérifie `checked>0` et les hashes des manifestes historiques | Runs `READY` et artefacts `VERIFIED` préparés par fixture ; reprise d’écriture de blob simulée dans le test. |
| W8 `workflow_fast` | Émulation workflow : conflit de version d’acceptation, puis publication avec exclusion explicite | Seeds READY directs ; pas de transition de génération/retry réelle. |

La collecte pytest réelle a relevé **7 node IDs `workflow_fast`** et **8 au
total** avec `workflow_full` : garde réseau, W2, deux tests W3, W4, W6, W8 et
W1. Les planchers Make sont 7 et 8. W5, W7, W9 (Typst réel) et W10
(navigateur) ne sont pas couverts. Aucun scénario ne compile ni n’inspecte un
PDF/Typst réel. `WorkflowDriver.build_emulator_app()` utilise les routers
officiels via ASGI, mais cette app recomposée n’est pas `main.app`.

Node IDs retournés par `pytest --collect-only -q` :

```text
workflow_fast
tests/workflow_emulator/test_network_guard.py::test_network_guard_blocks_external_socket_dns_and_httpx
tests/workflow_emulator/test_w2_inclusion_exclusion.py::test_w2_inclusion_exclusion_is_persisted_through_release
tests/workflow_emulator/test_w3_unproduced_subject_excluded.py::test_w3_unproduced_subject_is_explicitly_excluded
tests/workflow_emulator/test_w3_unproduced_subject_excluded.py::test_w3_deferred_subject_review_does_not_offer_acceptance
tests/workflow_emulator/test_w4_discovery_after_production.py::test_w4_discovery_after_production_keeps_pinned_inputs_publishable
tests/workflow_emulator/test_w6_concurrency_and_resume.py::test_w6_concurrency_and_resume
tests/workflow_emulator/test_w8_stale_composition_version.py::test_w8_accept_rejects_stale_composition_version

workflow_full
tests/workflow_emulator/test_w1_progressive_abc.py::test_w1_progressive_abc_uses_real_selection_and_production_transitions
```

## 6. Audit anti-faux-positifs M1–M7

`/tmp/lotc-audit-report.md` conclut : **0 bloquant**, **3 majeurs corrigés
avant `9702c70`**, et les sept mutations détectées puis restaurées. Les majeurs
étaient F1, docstring W1 surévaluant Discovery ; F2, garde réseau incomplet et
DSN non limité au local ; F3, skips silencieux si le DSN manque.

| Mutation | Résultat relevé |
|---|---|
| M1 — Review retourne `exclusions=()` | W2 échoue : audit d’exclusion absent/inapplicable dans Review. |
| M2 — Exclusion omise du manifeste | W2 échoue : ligne d’exclusion PostgreSQL de B absente. |
| M3 — Décision non persistée | W2 échoue : route d’exclusion HTTP 409. |
| M4 — B inclus et exclu | W2 échoue : un sujet ne peut être inclus et exclu. |
| M5 — Motif modifié à l’écriture | W2 échoue : décision exacte absente en PostgreSQL. |
| M6 — Fusion invalide l’artefact A | W1 échoue en relisant l’artefact après D2/F2 et production de C. |
| M7 — Composition crée un job | W2 et W1 échouent sur le compteur jobs/model_runs. |

M1–M7 ne mutaient que le code couvert par W1/W2. W3, W4, W6 et W8 n’ont pas
encore été éprouvés par des mutations de `backend/src/` ; ces tests de mutation
restent à faire.

## 7. Incidents

- `4a1fa71` a capturé `exclusions=()` pendant une preuve de mutation du Lot C ;
  `374f1ce` a restauré `exclusions=tuple(exclusions)`.
- La collision pytest venait de `pytest_plugins = ["tests.integration.conftest"]`
  dans le `conftest.py` du dossier workflow_emulator. Collecter ensemble
  `tests/integration` et `tests/workflow_emulator` provoquait
  `ValueError: Plugin already registered under a different name:
  tests.integration.conftest` et cassait `uv run pytest` en CI. Elle a été
  corrigée avant `9702c70` en important les fixtures par nom. Ce n’était pas
  une collision dans les modules Typst.
- Trois processus pytest orphelins sont restés bloqués 7–10 minutes sur
  `test_goodware.py::test_measurement_prepares_once_and_reuses_uuid_and_fingerprint` ;
  l’incident ne s’est pas reproduit ensuite.
- `test_analysis_subprocess.py` a été bloqué dans les sessions Luna sandboxées,
  mais pas hors sandbox. Des tests Typst locaux dépendent de `/snap/bin/typst`
  et d’un `TMPDIR` sous `$HOME` ; leurs échecs observés sont des problèmes
  d’environnement, pas des défauts de code.
- Une première relance des mesures a reçu un DSN synchrone (`postgresql://`)
  au lieu du DSN async requis (`postgresql+asyncpg://`) et a échoué avant les
  scénarios sur l’import de `psycopg2`. Les résultats ci-dessous proviennent
  d’une série relancée avec le DSN local correct.

## 8. Mesures et arbitrages

La commande reproductible est `make measure-workflow`, avec
`TEST_POSTGRES_ADMIN_DSN` pointant vers le PostgreSQL local de test. Elle lance
trois fois chaque profil, conserve les sorties pytest `--durations=0`, les
rapports JUnit, les traces JSONL, le RSS mesuré autour de pytest et le temps
total de Make. Elle n’arrête ni ne démarre le conteneur PostgreSQL.

| Mesure | Résultat |
|---|---|
| Préparation DB + `alembic upgrade head` (setup session pytest) | `setup` de W2 (premier test DB du FAST) ou W1 (premier test DB du FULL) : 0,46 / 0,55 / 0,65 s min / médiane / max. Il s’agit de l’attribution pytest du setup de session, avec ses autres fixtures de setup. |
| Durée par scénario (min / médiane / max, secondes) | Garde réseau 0,08 / 0,08 / 0,09 ; W1 2,76 / 3,01 / 3,11 ; W2 0,57 / 0,61 / 0,82 ; W3 principal 1,48 / 1,55 / 1,74 ; W3 Review 0,98 / 1,11 / 1,35 ; W4 1,32 / 1,39 / 1,54 ; W6 1,35 / 1,44 / 1,56 ; W8 0,58 / 0,68 / 0,80. W1 figure dans les trois runs FULL agrégés ; les autres valeurs couvrent six runs. |
| FAST, 3 exécutions | Toutes passent. Temps total Make min / médiane / max : 11,81 / 11,85 / 11,94 s ; codes de sortie 0, 0, 0. |
| FULL (`workflow_fast or workflow_full`), 3 exécutions | Toutes passent. Temps total Make min / médiane / max : 14,77 / 14,86 / 15,20 s ; codes de sortie 0, 0, 0. |
| Étape CI FULL-only locale (`workflow_full`) | `1 passed, 7 deselected in 3.72s` ; garde OK pour 1 scénario ; temps mur `/usr/bin/time -v` 7,90 s, RSS maximal 152 124 KiB. |
| RSS maximal du processus pytest | `/usr/bin/time -v` : 162 476 KiB max sur FAST et 166 444 KiB max sur FULL agrégé. |
| `make backend-sync` à chaud | `uv sync --locked --python 3.12 --group dev --group analysis` : 0,03 s mur, 0,01 s utilisateur, 0,02 s système, RSS 22 560 KiB. |
| Démarrage d’un conteneur PostgreSQL sous projet Compose distinct | `docker compose up -d --wait postgres-test` : 5,91 s, 0,09 s utilisateur + 0,03 s système, 2 % CPU du client Compose, RSS client 29 120 KiB. Image `postgres:16-alpine` déjà présente ; aucune image téléchargée. Le conteneur et le réseau du projet éphémère ont été retirés. |
| Variabilité observée | FAST varie de 0,13 s (1,1 % de la médiane), FULL de 0,43 s (2,9 %). Les scénarios W2–W8 varient selon le cas de 0,10 à 0,37 s ; W1 de 0,35 s. Aucune cause n’a été isolée ; les six runs sont séquentiels avec PostgreSQL déjà démarré. |

L’objectif du brief est FAST sous une minute supplémentaire et FULL en
quelques minutes. Le profil FAST complet mesure 11,85 s médiane et le profil
FULL agrégé 14,86 s médiane ; les deux restent sous ces budgets.

Pour la CI, FAST mesure 11,85 s médiane et le replay local de l’étape FULL-only
mesure 7,90 s. Le pytest principal exécutait auparavant les huit scénarios dans
une session : la somme des médianes d’appels (9,87 s) et du setup DB (0,55 s)
est environ 10,42 s. Le surcoût net est donc estimé à **environ 9 s** à chaud
(11,85 + 7,90 − 10,42), sans comparaison A/B du job GitHub. `make
test-integration`, utilisé par les deux gates, dépend de `backend-sync` et
exécute `uv sync --locked --python 3.12 --group dev --group analysis` ; à chaud
ce sync prend 0,03 s. Le temps d’installation à froid des dépendances, surtout
du groupe `analysis` sur un runner GitHub, n’a pas été mesuré et peut allonger
le job.

La cible doit être rejouée quand W5/W7/W9/W10 seront ajoutés ou que leur code
évoluera. Les mesures de cette section proviennent du profil vert à huit tests.

## 9. Risques résiduels et non-vérifié

- Actions GitHub n’a pas été exécutée depuis cette session ; l’étape FULL-only
  a été rejouée localement. Parse YAML réussi ; `actionlint` indisponible.
- `make lint-backend` et `make typecheck-backend` passent sur le HEAD courant.
  L’orchestrateur rapporte 3 074 tests backend et 27 tests workflow/intégration
  verts ; ces deux comptes viennent de son exécution, pas de cette session.
- W5, W7, W9 et W10 restent absents. Discovery reste fixturée dans W1/W3/W4 ;
  aucun scénario ne compile un PDF/Typst réel. L’application du DDL live et le
  restamp n’ont pas été faits.
- Les mutations M1–M7 ne couvrent que W1/W2. Les mutations de code source qui
  éprouveraient W3/W4/W6/W8 restent à faire.
- La pièce jointe de mission n’était pas lisible depuis les sessions
  déléguées. Les exigences chiffrées du brief fourni ont été vérifiées ; les
  seuls points encore **À CONFIRMER** concernent des formulations ou détails
  accessibles uniquement dans la pièce jointe complète.
