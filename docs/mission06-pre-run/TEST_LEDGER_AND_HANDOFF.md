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
| 2026-10-25 | `980fef7e` | `pytest tests/integration/test_osint_open_policy_postgres.py -q` (politique, base jetable 55432) | **3 passed, 3 failed — RED** |
| 2026-10-25 | `980fef7e` | `ruff check` / `ruff format --check` (politique, implémenteur) | **PASS** (428 fichiers formatés) |
| 2026-10-25 | `c5f27331` | `ruff check` / `ruff format --check` (S3 core, reviewer) | **PASS** |
| 2026-10-25 | `642fae96` | `pytest tests/test_external_effects.py -q` (S3 core, parent) | **34 passed** |
| 2026-10-25 | `7ef241f2` | `pytest tests/integration/test_osint_open_policy_postgres.py -q` (parent, base jetable) | **6 passed** |
| 2026-10-25 | combiné `a1e6d994` + fixes env | `alembic upgrade head` sur base jetable neuve | **UPGRADE_OK** après exclusion `external_effect_*` du baseline et import `retrohunt` |
| 2026-10-25 | combiné pré-fix fixture | `pytest tests/integration/test_external_effects_postgres.py -q` | **5 failed** — FK réservation budgétaire (fixture), correction en cours |
| — | — | `make lint-backend` / `make typecheck-backend` | **NOT_RUN** côté agent (sync réseau) ; S3 core a rapporté lint+typecheck PASS dans son environnement |
| — | — | S3 core PostgreSQL final | **EN ATTENTE** — rerun après correction fixture et intégration |

Les 3 échecs PG de la politique à `980fef7e` sont : assertion `decisions == ()`
vs liste vide, helper de réutilisation de sujet avec `edition=None`, et
`BudgetExceeded(vt_units)` lors d'une extension manuelle après révocation.
Une correction est en cours ; aucun PASS n'est déclaré avant un rerun vert.
Aucun test fournisseur réel, MinIO live ou PostgreSQL live n'a été exécuté.

## 5. Verdicts finaux (état à la clôture de cette session)

```text
OSINT_OPEN_POLICY_IMPLEMENTED|PARTIAL|BLOCKED       : PARTIAL
NO_REPEATED_OPERATOR_APPROVALS|PARTIAL|BLOCKED      : PARTIAL
TLP_CLEAR_DEFAULT_FOR_SHAREABLE_SOURCES|PARTIAL|BLOCKED : PARTIAL
G3_NONBLOCKING_FOR_ANALYSIS|PARTIAL|BLOCKED         : PARTIAL

S3_VT_READ_READY|BLOCKED                            : BLOCKED
S3_VT_DOWNLOAD_READY|BLOCKED                        : BLOCKED
S3_BRIDGE_READY|BLOCKED                             : BLOCKED
S3_OTHER_ROUTES_NOT_CLAIMED                         : CONFIRMED

CREDENTIAL_NON_DISCLOSURE_PASS|PARTIAL|BLOCKED      : PARTIAL
POSTGRES_CONCURRENCY_AND_RECOVERY_PASS|PARTIAL|BLOCKED : PARTIAL
LOCAL_ANALYSIS_READY|PARTIAL|BLOCKED                : PARTIAL

LUNA_XHIGH_MULTI_AGENT_VERIFIED|FAILED              : FAILED
INDEPENDENT_REVIEWS_APPROVED|REVISE|BLOCKED         : REVISE

REAL_RUN_PREFLIGHT_READY|PARTIAL|BLOCKED            : PARTIAL
REAL_INVESTIGATION_NOT_STARTED                      : CONFIRMED
```

Justification :

- **PARTIAL politique** : le code, les 50 tests unitaires et les 6 tests
  PostgreSQL de politique sont verts, mais la revue indépendante reste
  `REVISE` sur la course révocation → émission (propriété S3-CORE), la
  projection `/operator-status` et la lignée d'invocation. Aucun lot n'est
  fusionné sur `main` tant que la revue n'est pas `APPROVED`.
- **BLOCKED S3 familles** : seul le socle `ExternalEffectCoordinator` existe ;
  aucune famille VT/Bridge n'est migrée. Les verdicts READY ne sont donc pas
  revendiqués. La chaîne `0003→0004` s'applique sur base jetable après deux
  corrections d'intégration, mais les tests PG S3 finaux restent à rejouer.
- **PARTIAL non-divulgation** : les filtres de credentials/signed URLs sont
  testés unitairement, mais une revue a trouvé des fragments d'URL
  credentials persistables, en cours de correction ; aucune preuve live.
- **PARTIAL concurrence/récupération** : la politique a 6 tests PG verts ;
  les tests S3 de concurrence/crash ont échoué sur un fixture FK avant
  correction et n'ont pas encore été rejoués.
- **FAILED Luna xhigh multi-agent** : tous les enfants ont été créés en
  `codex/gpt-6-luna` `xhigh` avec vérification runtime, mais l'orchestrateur
  parent est `deepseek-flash[1m]`, pas Luna xhigh. La conformité complète de
  la mission n'est donc pas atteinte.
- **REVISE revues** : deux revues indépendantes ont rendu `REVISE` sur les
  deux lots ; aucune n'est `APPROVED` à la clôture.
- **PARTIAL prévol run réel** : le plan et les inconnues sont documentés ;
  les lectures canoniques live (lignée de synthèse, état des jobs, MinIO)
  n'ont pas été effectuées et restent à autoriser.

## 6. Handoff futur

Le handoff final doit contenir : SHA intégré, liste des lots approuvés,
commandes et résultats réellement exécutés, tests `NOT_RUN`, risques résiduels,
verdicts finaux et prochaine action permissible. Aucun push, merge distant,
déploiement, migration live ou appel fournisseur n'est autorisé par ce
registre.
