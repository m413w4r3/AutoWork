# Registre des tests et handoff — Mission 06-P

Statut : **EN COURS**. Toute ligne non exécutée reste `NOT_RUN`. Aucun test
n'est déclaré PASS sans commande et sortie réelles. Baseline de départ :
`505c1cd2` ; docs de conception : `7059ebbd`.

## 1. Règles de preuve

- Un `PASS` exige : commande exacte, environnement (Python 3.12 via
  `uv run --python 3.12`), date, résultat et SHA du code testé.
- Les tests PostgreSQL exigent une base jetable réelle via
  `TEST_POSTGRES_ADMIN_DSN` ou le profil Compose `integration-test`.
  Un fake en mémoire ne prouve ni `FOR UPDATE`, ni l'unicité, ni les courses
  entre sessions.
- Un test fournisseur réel n'est **jamais** exécuté dans cette mission.
  Les transports sont simulés ; un compteur de sends à zéro prouve l'absence
  d'appel AutoWork, pas la réception côté fournisseur.
- Un test non exécuté est `NOT_RUN`, jamais `PASS` par inférence.
- Ne jamais supprimer une assertion, ajouter un skip/xfail ou masquer une
  exception pour faire passer un test sans changement de comportement validé.

## 2. Lots et tests prévus

### 2.1 Politique OSINT_OPEN

| ID | Oracle | Type | Statut | Preuve |
|---|---|---|---|---|
| P-01 | Activation sans acteur humain authentifié refusée | unit/API | NOT_RUN | — |
| P-02 | Activation avec acteur audité, version et événement persistés | PG | NOT_RUN | — |
| P-03 | Rejeu même corrélation = une version ; corrélation différente = conflit | unit/PG | NOT_RUN | — |
| P-04 | Extension croissance uniquement ; pas d'élargissement TLP | unit | NOT_RUN | — |
| P-05 | Héritage au démarrage : une enveloppe `authority_kind=OSINT_POLICY`, un pin | PG | NOT_RUN | — |
| P-06 | Aucune décision `HUMAN`/`PIVOT_APPROVE` fabriquée pour l'héritage | PG/audit | NOT_RUN | — |
| P-07 | Deux héritages concurrents = une seule enveloppe | PG 2 sessions | NOT_RUN | — |
| P-08 | Investigation ancienne/non épinglée : pas d'héritage, externe refusé | PG | NOT_RUN | — |
| P-09 | Élargissement de politique ne met pas à niveau un cas épinglé | PG | NOT_RUN | — |
| P-10 | Révocation propage aux enveloppes épinglées ; nouveau héritage refusé | PG | NOT_RUN | — |
| P-11 | Enveloppe étendue manuellement survit à la révocation comme `SUPERSEDED` | PG | NOT_RUN | — |
| P-12 | TLP : source GREEN/RED, `do_not_submit`, `external_llm_allowed=false` conservés | unit/API | NOT_RUN | — |
| P-13 | TLP inconnu fail-closed | unit | NOT_RUN | — |
| P-14 | G3 agent : candidate/related/uncertain acceptés ; confirmed/rejected refusés | unit/API | NOT_RUN | — |
| P-15 | Aucun chemin Bridge/LLM sans enveloppe en RESTRICTED | unit/PG | NOT_RUN | — |
| P-16 | Aucun secret dans politique, événements, logs | unit/audit | NOT_RUN | — |
| P-17 | Migration 0002→0003 sur base jetable ; downgrade gardé | PG migration | NOT_RUN | — |

### 2.2 S3-CORE

| ID | Oracle | Type | Statut | Preuve |
|---|---|---|---|---|
| S3-01 | Refus sans permis : aucun claim consommé, zéro send | unit/fake | NOT_RUN | — |
| S3-02 | Permis lié : invocation/famille/digest/occurrence/envelope altérés → refus | unit | NOT_RUN | — |
| S3-03 | Révocation avant consume → refus, zéro send | PG 2 sessions | NOT_RUN | — |
| S3-04 | Deux workers concurrents sur même effet → un seul claim/send | PG 2 sessions | NOT_RUN | — |
| S3-05 | Même opération rejouée → réutilisation, zéro second send | PG | NOT_RUN | — |
| S3-06 | Occurrence explicite distincte → nouveau claim autorisé | PG | NOT_RUN | — |
| S3-07 | Crash avant consume → aucun claim, aucun send au restart | PG | NOT_RUN | — |
| S3-08 | Crash après consume / réponse ambiguë → `NEEDS_REVIEW`, pas de resend | PG/fake | NOT_RUN | — |
| S3-09 | Mutation des octets préparés → transport refuse | unit | NOT_RUN | — |
| S3-10 | Appel direct transport sans permis → refus typé, zéro send | unit | NOT_RUN | — |
| S3-11 | Retry/redirect/pagination → chaque send physique reconsumé ou refusé | fake instrumenté | NOT_RUN | — |
| S3-12 | Budgets et preuves conservés ; réutilisation budget delta 0 | PG | NOT_RUN | — |
| S3-13 | Migration 0003→0004 sur base jetable ; downgrade gardé | PG migration | NOT_RUN | — |

### 2.3 Familles S3

| ID | Oracle | Type | Statut | Preuve |
|---|---|---|---|---|
| VT-R-01 | File report/relationship/search : un permis par GET, fallback compris | fake | NOT_RUN | — |
| VT-R-02 | Pagination : chaque page consomme un permis | fake | NOT_RUN | — |
| VT-D-01 | `vt.download_sample` : direct/ticket/CDN, chaque requête physique permis | fake | NOT_RUN | — |
| VT-D-02 | Réutilisation locale vérifiée : zéro send, zéro unité, lignée | PG/fake | NOT_RUN | — |
| BR-01 | Tour Bridge : chaque submit/retry/correction consomme un permis | fake | NOT_RUN | — |
| BR-02 | `advisor.consult` : permis au transport, archive prompt conservée | fake | NOT_RUN | — |
| BR-03 | Révocation entre deux tentatives Bridge → refus, pas de send | PG/fake | NOT_RUN | — |
| AD-01 | Famille non migrée explicitement identifiée, pas de fallback silencieux | audit source | NOT_RUN | — |

### 2.4 E2E P2

| ID | Oracle | Type | Statut | Preuve |
|---|---|---|---|---|
| E2E-01 | Création d'investigation sous OSINT_OPEN sans G2 répété | PG | NOT_RUN | — |
| E2E-02 | Recherche VT sans approbation supplémentaire | PG/fake | NOT_RUN | — |
| E2E-03 | Plusieurs pivots VT successifs sans interruption | PG/fake | NOT_RUN | — |
| E2E-04 | Acquisition d'un `candidate` admissible sans G3 artificiel | PG/fake | NOT_RUN | — |
| E2E-05 | Analyse locale d'un `candidate` | PG/fake | NOT_RUN | — |
| E2E-06 | Consultation Bridge de données partageables sans nouvelle approbation | PG/fake | NOT_RUN | — |
| E2E-07 | Aucune fuite de secret AutoWork | audit/fake | NOT_RUN | — |
| E2E-08 | Révocation explicite respectée | PG | NOT_RUN | — |
| E2E-09 | Erreurs, refus légitimes et résultats partiels correctement gérés | fake | NOT_RUN | — |
| E2E-10 | Chaîne hypothèse → raison → preuve → résultat → décision suivante lisible | audit | NOT_RUN | — |

## 3. Gates de validation

| Gate | Commande | Statut |
|---|---|---|
| Lint backend | `make lint-backend` | NOT_RUN |
| Typecheck backend | `make typecheck-backend` | NOT_RUN |
| Tests backend ciblés | `make test-backend PYTEST_ARGS="…"` | NOT_RUN |
| Tests backend complets | `make test-backend` | NOT_RUN |
| Intégration PostgreSQL | `make test-integration` | NOT_RUN |
| Workflow FAST | `make test-workflow` | NOT_RUN |
| Workflow FULL | `make test-workflow-full` | NOT_RUN |
| Scripts CI | `make test-ci-scripts` | NOT_RUN |
| Frontend | `make test-frontend` / lint | NOT_RUN (non requis si aucun chemin UI) |
| Absence de secrets | revue ciblée des diffs | NOT_RUN |
| Migration sur base jetable | `alembic upgrade head` + tests migration | NOT_RUN |

## 4. Tests réellement exécutés

| Date | SHA | Commande | Résultat |
|---|---|---|---|
| 2026-10-25 | `c5f27331` | `pytest tests/test_external_effects.py -q` (S3 core, parent) | **19 passed** |
| 2026-10-25 | `f0787d52` | `pytest tests/test_osint_open_policy.py -q` (politique, parent) | **22 passed** |
| 2026-10-25 | `980fef7e` | `pytest tests/test_osint_open_policy.py -q` (politique, parent) | **49 passed** |
| 2026-10-25 | `33104968` | `pytest tests/test_osint_open_policy.py -q` (politique, parent) | **51 passed** |
| 2026-10-26 | `main@efc38e8a` | politique intégrée : `pytest tests/test_osint_open_policy.py` | **51 passed** |
| 2026-10-26 | `main@efc38e8a` | politique intégrée : `pytest tests/integration/test_osint_open_policy_postgres.py` (base jetable) | **6 passed** |
| 2026-10-25 | `980fef7e` | `pytest tests/integration/test_osint_open_policy_postgres.py -q` (politique, base jetable 55432) | **3 passed, 3 failed — RED** |
| 2026-10-25 | `980fef7e` | `ruff check` / `ruff format --check` (politique, implémenteur) | **PASS** (428 fichiers formatés) |
| 2026-10-25 | `c5f27331` | `ruff check` / `ruff format --check` (S3 core, reviewer) | **PASS** |
| 2026-10-25 | `642fae96` | `pytest tests/test_external_effects.py -q` (S3 core, parent) | **34 passed** |
| 2026-10-25 | `7ef241f2` | `pytest tests/integration/test_osint_open_policy_postgres.py -q` (parent, base jetable) | **6 passed** |
| 2026-10-25 | combiné `a1e6d994` + fixes env | `alembic upgrade head` sur base jetable neuve | **UPGRADE_OK** après exclusion `external_effect_*` du baseline et import `retrohunt` |
| 2026-10-25 | combiné pré-fix fixture | `pytest tests/integration/test_external_effects_postgres.py -q` | **5 failed** — FK réservation budgétaire (fixture), corrigé |
| 2026-10-26 | combiné `e2b05cfd` | `pytest tests/integration/test_external_effects_postgres.py -q` (base jetable) | **3 passed, 2 failed** — `effect_occurrence_required` et fixture budget `limits` ; correction finale en cours |
| — | — | `make lint-backend` / `make typecheck-backend` | **NOT_RUN** côté agent (sync réseau) ; S3 core a rapporté lint+typecheck PASS dans son environnement |
| 2026-10-26 | combiné `e2b05cfd` | `pytest tests/integration/test_external_effects_postgres.py -q` (base jetable) | **3 passed, 2 failed** — replay `effect_occurrence_required` et fixture budget ; revue S3 `REVISE` (3 MAJOR) |

Les 3 échecs PG de la politique à `980fef7e` sont : assertion `decisions == ()`
vs liste vide, helper de réutilisation de sujet avec `edition=None`, et
`BudgetExceeded(vt_units)` lors d'une extension manuelle après révocation.
Une correction est en cours ; aucun PASS n'est déclaré avant un rerun vert.
Aucun test fournisseur réel, MinIO live ou PostgreSQL live n'a été exécuté.

## 5. Verdicts finaux (état à la clôture de cette session)

```text
OSINT_OPEN_POLICY_IMPLEMENTED|PARTIAL|BLOCKED       : PASS (couche politique)
NO_REPEATED_OPERATOR_APPROVALS|PARTIAL|BLOCKED      : PARTIAL
TLP_CLEAR_DEFAULT_FOR_SHAREABLE_SOURCES|PARTIAL|BLOCKED : PASS (logique politique testée)
G3_NONBLOCKING_FOR_ANALYSIS|PARTIAL|BLOCKED         : PARTIAL

S3_VT_READ_READY|BLOCKED                            : BLOCKED
S3_VT_DOWNLOAD_READY|BLOCKED                        : BLOCKED
S3_BRIDGE_READY|BLOCKED                             : BLOCKED
S3_OTHER_ROUTES_NOT_CLAIMED                         : CONFIRMED

CREDENTIAL_NON_DISCLOSURE_PASS|PARTIAL|BLOCKED      : PARTIAL
POSTGRES_CONCURRENCY_AND_RECOVERY_PASS|PARTIAL|BLOCKED : PARTIAL
LOCAL_ANALYSIS_READY|PARTIAL|BLOCKED                : PARTIAL

LUNA_XHIGH_MULTI_AGENT_VERIFIED|FAILED              : FAILED
INDEPENDENT_REVIEWS_APPROVED|REVISE|BLOCKED         : REVISE (politique APPROVED ; S3 REVISE)

REAL_RUN_PREFLIGHT_READY|PARTIAL|BLOCKED            : PARTIAL
REAL_INVESTIGATION_NOT_STARTED                      : CONFIRMED
```

Justification :

- **PASS politique** : la politique est intégrée sur `main` (`4a97fa58`),
  avec **51 tests unitaires** et **6 tests PostgreSQL** verts, et une revue
  indépendante **APPROVED** pour le lot politique. La course révocation →
  émission, le plafond d'effets et la lignée d'invocation restent des gates
  S3-CORE avant toute release externe.
- **BLOCKED S3 familles** : seul le socle `ExternalEffectCoordinator` existe
  (candidat `2c6c6074`, non intégré) ; aucune famille VT/Bridge n'est migrée.
  Les verdicts READY ne sont donc pas revendiqués. La chaîne `0003→0004`
  s'applique sur base jetable après deux corrections d'intégration, mais le
  gate PG S3 est à 3/5 et la revue S3 reste `REVISE` (3 MAJOR).
- **PARTIAL non-divulgation** : les filtres de credentials/signed URLs sont
  testés unitairement, mais la revue S3 a trouvé des fragments `id_token` /
  `refresh_token` persistables ; correction non intégrée (S3 `REVISE`) ;
  aucune preuve live.
- **PARTIAL concurrence/récupération** : la politique a 6 tests PG verts et
  la suite de migrations **120/120** ; le gate S3 combiné est à 3/5, avec
  deux échecs corrigés dans le candidat `2c6c6074` mais non rejoués/reviewés
  sur `main`. Aucun verdict de concurrence S3 n'est revendiqué.
- **FAILED Luna xhigh multi-agent** : tous les enfants ont été créés en
  `codex/gpt-6-luna` `xhigh` avec vérification runtime, mais l'orchestrateur
  parent est `deepseek-flash[1m]`, pas Luna xhigh. La conformité complète de
  la mission n'est donc pas atteinte.
- **REVISE revues** : la revue politique est `APPROVED` (lot intégré) ; la
  revue S3 reste `REVISE` (3 MAJOR : gate sans I/O physique, fragments de
  token, preuve no-handoff forgeable). Le socle S3 n'est pas intégré.
- **Gates finaux réellement exécutés sur `main`** : suite backend
  **4931 passed, 5 failed** (tous Typst : `/snap/bin/typst --version` échoue
  avec `snap-confine ... cap_dac_override`), 31 skipped ; suite d'intégration
  **355 passed, 7 failed** (tous Typst/environnementaux) ; migrations
  **120 passed** ; `ruff check`/`format` PASS ; `mypy` **success on 420
  source files**. Les échecs Typst ne sont pas des régressions politiques ;
  ils ne sont pas déclarés PASS.
- **PARTIAL prévol run réel** : le plan et les inconnues sont documentés ;
  les lectures canoniques live (lignée de synthèse, état des jobs, MinIO)
  n'ont pas été effectuées et restent à autoriser.

## 6. Handoff concret

### État Git

- `main` local : `4a97fa58` — **politique OSINT_OPEN intégrée, approuvée et
  vérifiée** (51 unitaires + 6 PostgreSQL verts, revue `APPROVED`, suites
  backend/intégration vertes hors Typst). Aucun push, merge distant,
  déploiement ou migration live.
- Branche candidate S3 : `m06/s3-core-impl` à `2c6c6074` — **NON INTÉGRÉE**.
  46 unitaires verts, lint+typecheck PASS ; revue indépendante `REVISE`
  (3 MAJOR) ; gate PG combiné 3/5 avant la correction `2c6c6074`, non
  rejoué/reviewé sur `main`.
- Gate combiné temporaire : `/tmp/m06-combined-gate`, branche
  `m06/combined-gate` à `e2b05cfd` (politique + S3 jusqu'à `de8b7451` +
  correctifs d'intégration `env.py`). Chaîne `0001→0004` : `UPGRADE_OK` ;
  politique PG 6/6 ; S3 PG 3/5.

### Prochaine action permissible

1. Corriger les 3 MAJOR S3 (gate sans I/O physique, fragments `id_token` /
   `refresh_token`, preuve no-handoff forgeable), puis faire une revue
   indépendante du candidat corrigé.
2. Si et seulement si la revue S3 passe `APPROVED` : intégrer le socle S3,
   appliquer les deux correctifs `env.py` (exclusion `external_effect_*` du
   baseline, import `retrohunt`), rejouer `alembic upgrade head` et les cinq
   tests PG S3.
3. Si la revue S3 reste `REVISE` : conserver la branche candidate, ne pas
   fusionner, et traiter les constats avant toute release externe.
4. Toute prévol live (lecture canonique Run 12, G2/TLP, MinIO) exige une
   autorisation séparée ; aucun appel fournisseur, téléchargement ou
   exécution d'échantillon n'est autorisé par ce handoff.

### Risques résiduels

- Course révocation → émission : propriété du consume atomique S3-CORE, pas
  encore prouvée en PostgreSQL combiné.
- Plafond `max_effect_claims_per_investigation` et lignée d'invocation :
  validation politique présente, application runtime à câbler dans S3.
- Identité de production : les providers locaux/statiques collapsent les
  utilisateurs en IDs génériques ; attribution humaine non vérifiée.
- Toutes les capacités VT/Bridge/analyse locale restent `UNKNOWN` ou
  `BLOCKED` en live ; aucun test ne prouve une réception fournisseur.
- Le parent n'est pas Luna xhigh ; les enfants le sont.

## 6. Reprise M06-P/R (2026-10-26) — tests réellement exécutés par le parent

Environnement local corrigé (aucune modification de code) : Typst épinglé
`0.15.1` = `~/.local/share/autowork-tools/typst` via `TYPST_BINARY` et `PATH`
(`/snap/bin/typst --version` échoue : snap-confine `cap_dac_override`),
`FONT_BUNDLE_ROOT=<checkout principal>/chpTypst` pour les worktrees,
`TMPDIR` sous `$HOME`, PostgreSQL jetable `postgres-test` (127.0.0.1:55432,
profil `integration-test`, distinct de la stack live).

**Correction du handoff précédent.** La CI GitHub de `af819b2e` (run
`40284320309`) est **rouge** : 11 tests workflow (FAST 9, FULL 2) en 403
`verified_human_operator_required` et mypy en échec
(`tests/test_osint_open_policy.py:97`). Ces gates n'étaient pas couverts par
le handoff (« 355 passed » = intégration seule ; « mypy 420 PASS » inexact).
Localement, l'intégration de `af819b2e` avait aussi 1 régression de même
nature (`test_operator_context_cas_and_advisor_egress_guard_postgres`, revoke
d'enveloppe exigeant désormais un humain vérifié). Les 5 + 7 échecs
« Typst » du handoff étaient bien environnementaux.

| SHA | Commande | Résultat |
|---|---|---|
| `af819b2e` | `make test-backend` (Typst épinglé) | **4965 passed, 2 skipped, 0 failed** |
| `af819b2e` | `make test-integration` | 361 passed, **1 failed** (revoke 403) |
| `af819b2e` | workflow FAST+FULL (local) | 403 reproduits (W15) |
| `db9b19f9` | workflow FAST+FULL + `FONT_BUNDLE_ROOT` | **31 passed** |
| `e775b30a` | `make test-integration` | **362 passed** |
| `ff5f3d5e` | `mypy src tests` | **Success, 834 fichiers** ; `ruff` PASS |
| `a832902b` | test de refus non-loopback, mutation (clause loopback retirée → échec ; rétablie → succès, y compris `APP_ENV=production`) | PASS |
| `a1f0d0c2` | S3 unit / PG S3+politique | 60 / 13 passed ; suites complètes : backend 3 failed (1 réel : liste des révisions), intégration 6 failed (têtes `0003` attendues) |
| `2d39fe34` | `alembic upgrade head → downgrade 0003 → head → downgrade 0002 → head` (base jetable, supprimée ensuite) | **OK**, tête `0004`, 2 tables `external_effect_*` |
| `2d39fe34` | PG S3 + migrations | 134 passed, **3 failed** (nouveaux tests non exécutés par l'auteur) |
| `8a8bc7da` | ruff/mypy ; backend ; intégration ; workflow | PASS ; 5025 passed + 2 env ; **373 passed** ; **31 passed** |
| `de4f6603` | idem | PASS ; 5029 + 2 env ; **373** ; **31** |
| **`796292a7`** | ruff/format ; mypy ; backend ; intégration ; workflow FAST+FULL | **PASS ; 840 fichiers ; 5031 passed + 2 env ; 373 passed ; 31 passed** |

Les 2 échecs « env » sont `tests/test_local_analysis_commands.py`
(`test_catalogued_commands_read_synthetic_elf_with_bounded_output`,
`test_metacharacters_in_sample_strings_are_inert_and_filenames_are_fixed`) :
ils échouent à l'identique sur une archive `git archive` de `main@a832902b`
hors checkout principal et passent dans le checkout principal ; non liés à S3.
`backend/migrations` est inchangé entre `2d39fe34` et `796292a7`.

État `main` local : `af819b2e` (= `origin/main`) → `a832902b`
(workflow-gate-fix) → `796292a7` (S3-CORE) → `044178b2` (docs). **Aucun push.**

### 6.1 Suite des gates M06-P/R

| SHA | Commande | Résultat |
|---|---|---|
| `c36bb116` | backend ; intégration | 5037 + 2 env ; 377 passed |
| `911f8738` | ruff, mypy ; backend ; intégration | PASS ; 5060 + 2 env ; 378 passed |
| `90f73648` | idem + workflow | PASS ; 5074 + 2 env ; 379 ; 31 |
| `0d067f91` | idem | PASS ; 5075 + 2 env ; 389 + 1 instable (`test_publication_render_repository[source_blob_id]`, 3/3 isolément) ; 31 |
| `1df683cb` | idem | PASS ; 5075 + 2 env ; **394** ; **31** |
| `15ee7e7b` | idem | PASS ; **12 ❌** backend (10 réels) ; 396 / 1 ❌ ; 28 / 3 ❌ → REVISE |
| `bbe4c342` | idem | PASS ; **5084 + 2 env** ; **409** ; **31** |
| `b89a9191` | ruff, mypy ; `test_investigation_acquisition.py` ; workflow | PASS ; 20 ; **31** |

## 7. Verdicts M06-P/R (2026-10-26)

```text
OSINT_OPEN_POLICY_IMPLEMENTED : PASS conservé
NO_REPEATED_OPERATOR_APPROVALS : PARTIAL   (VT lectures démontrées ; Bridge/download non migrés)
G3_NONBLOCKING_FOR_ANALYSIS : PARTIAL      (non démontré via routes migrées)

S3_CORE : APPROVED                          (796292a7 ; extensions 1df683cb)
S3_VT_READ : READY                          (b89a9191 APPROVED, intégré main@1c28892c, suites complètes vertes)
S3_VT_DOWNLOAD : BLOCKED                    (non commencé ; migration 0005 requise)
S3_BRIDGE : BLOCKED                         (783c0ebb non revu, câblage absent)

CREDENTIAL_NON_DISCLOSURE : PARTIAL         (core + VT lectures prouvés ; Bridge/download non migrés)
POSTGRES_RACE_AND_RECOVERY : PARTIAL        (core + VT : concurrence, révocation, crash, rejeu ; pas Bridge/download)
FULL_TEST_GATES : PARTIAL                   (local vert hors 2 tests env ; CI GitHub non rejouée sans push ; frontend non exécuté, non touché)
LUNA_XHIGH_MULTI_AGENT_VERIFIED : FAILED    (parent claude-opus-5-5 ; enfants Luna vérifiés)
REAL_RUN_PREFLIGHT : PARTIAL                (code/docs seulement ; lectures live non autorisées)
REAL_INVESTIGATION_NOT_STARTED : CONFIRMED
```

READY pour VT-READ signifie : tranche, tests fakes + PostgreSQL, frontière
d'émission et revue indépendante vérifiés **hors ligne**. Aucun appel VT réel
n'a été fait ; la réception côté fournisseur n'est pas prouvée.
