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

## 4. Tests déjà exécutés

Aucun test de code Mission 06-P n'a encore été exécuté. Les reconnaissances
POLICY, S3, RUNTIME et RUN étaient en lecture seule et n'ont lancé aucun test.

## 5. Handoff futur

Le handoff final doit contenir : SHA intégré, liste des lots approuvés,
commandes et résultats réellement exécutés, tests `NOT_RUN`, risques résiduels,
verdicts finaux et prochaine action permissible. Aucun push, merge distant,
déploiement, migration live ou appel fournisseur n'est autorisé par ce
registre.
