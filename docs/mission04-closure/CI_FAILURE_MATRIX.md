# Mission 04 closure — matrice des échecs CI

**Run de référence :** GitHub Actions
[40129845019](https://github.com/m413w4r3/AutoWork/actions/runs/40129845019),
sur `d7cc6b590d3be41dae10299cc03364771fe6bda1`, conclusion `failure`.
**Date du run :** 2026-10-24.
**Statut global de clôture :** en cours — voir la colonne « clôture » mise à
jour après chaque lot.

## 1. Jobs du run

| Job | Conclusion | Étape en échec | Classification initiale | Clôture |
|---|---|---|---|---|
| `compose` | success | — | n/a | vert, ne prouve pas l'applicatif |
| `backend` | failure | `uv run mypy src tests` | PREEXISTING (dette de types) | en cours (CI-0a + L4) |
| `backend` | failure | Workflow emulator FAST | PREEXISTING (contrat obsolète/mesures) | en cours (CI-0b) |
| `backend` | failure | Workflow emulator FULL | PREEXISTING (observations/ambiguïté/config) | en cours (CI-0c) |
| `frontend` | failure | `pnpm lint` (`prettier --check .`) | PREEXISTING (formatage) | corrigé localement (ccff3444) |
| `typst-renderer-smoke` | failure | compile production renderer | PREEXISTING (fixture sans `document_label`) | corrigé localement (ccff3444) |
| `workflow-browser` | failure | J2 investigation/PDF | PREEXISTING (`_S2_BYTES` privé supprimé) | corrigé localement (ccff3444) |

`d7cc6b59` est un merge dont les parents sont `e120a36f` et `b4a82ac0`.
`git diff e120a36f..d7cc6b59` ne touche **aucun** fichier des tests workflow
emulator, du renderer Typst, du frontend ni des rule tools. Ces échecs sont
donc antérieurs au merge Mission 04 ; ils sont néanmoins un gate CI
obligatoire de la clôture (`G_CI_MAIN`).

## 2. Détail des échecs reproduits localement

### 2.1 mypy repo-wide — 60 erreurs / 13 fichiers

Reproduit par `cd backend && uv run --python 3.12 mypy src tests` :

| Fichier | Erreurs | Propriétaire |
|---|---:|---|
| `src/cti_app/application/research_context_builder.py` | 25 | L4 |
| `src/cti_app/application/bridge_investigator.py` | 7 | CI-0a |
| `src/cti_app/application/investigation_persistence.py` | 5 | CI-0a |
| `src/cti_app/application/investigation_audit_export.py` | 5 | CI-0a |
| `src/cti_app/infrastructure/database/investigation_audit_reader.py` | 4 | CI-0a |
| `tests/test_mission01_l2_context_pagination.py` | 4 | L4 |
| `src/cti_app/application/context_pagination.py` | 3 | L4 |
| `src/cti_app/application/operator_tool_invocation.py` | 2 | L4 |
| `tests/test_research_context_builder.py` | 1 | L4 |
| `tests/test_model_gateway.py` | 1 | CI-0a |
| `tests/test_investigation_vt_tools.py` | 1 | CI-0a |
| `tests/test_advisor_consultation.py` | 1 | CI-0a |
| `tests/integration/test_investigation_audit_export_postgres.py` | 1 | CI-0a |

### 2.2 Frontend — Prettier

`pnpm lint` échoue uniquement sur
`frontend/src/features/investigation/InvestigationLabViews.test.tsx`.
Correction limitée au formatage Prettier verrouillé ; aucun changement d'AST
ni de comportement.

### 2.3 Typst smoke — `document_label`

Le fixture inline de `.github/workflows/ci.yml` omettait `document_label`,
alors que `chpTypst/RENDERER/publication_helpers.typ` l'exige depuis le
durcissement du renderer. La fixture canonique a été enrichie ; l'exigence de
production n'a pas été assouplie. Compilation locale vérifiée avec Typst
0.15.1 et le bundle de polices verrouillé : PDF non vide et texte extrait.

### 2.4 Workflow browser J2 — `_S2_BYTES`

`backend/tests/workflow_emulator/investigation_browser_server.py` importait
`_S2_BYTES` et `_S2_HASH`, symboles privés supprimés de
`tests/integration/test_investigation_vertical_postgres.py`. Le serveur
importe désormais les constantes publiques `DEFAULT_SAMPLE_BYTES` /
`DEFAULT_SAMPLE_HASH` de
`tests/workflow_emulator/investigation_support.py` ; le test n'est ni sauté
ni affaibli.

### 2.5 Workflow emulator FAST

| Test | Symptôme exact | Cause | Correctif |
|---|---|---|---|
| `W13-D5` | `rule.test` attendu `SUCCEEDED`, obtenu `PARTIAL` | limitations de corpus informatives (holdout sans rôle, absence de baseline goodware) traitées comme échec de mesure | CI-0b |
| `W13-D7` | `vt.file_report` attendu `UNAVAILABLE`, obtenu `NEEDS_REVIEW` | durcissement d'ambiguïté externe `d1580a93` : une erreur d'adaptateur sans preuve framework devient `NEEDS_REVIEW` | CI-0b |
| `W14-A` | `candidate_status` `candidate` au lieu de `tested` | même cause que W13-D5 (`corpus.limitations` bloque `TESTED`) + clés de rôle obsolètes | CI-0b |
| `W14-E` | `KeyError: 'confirmed_positive'` | les valeurs persistées canoniques sont `human_confirmed` / `new_candidate_unconfirmed` / `holdout_positive` ; les anciennes valeurs sont des alias | CI-0b |

### 2.6 Workflow emulator FULL

| Test | Symptôme exact | Cause | Correctif |
|---|---|---|---|
| `W15` | 2 observations `network.protocol.verify` au lieu de 1 | diagnostic en cours | CI-0c |
| `W18` | `response_refs` vide sur une invocation `vt.download_sample` `NEEDS_REVIEW` | diagnostic en cours (références d'observation partielle possiblement perdues lors de la conversion d'ambiguïté) | CI-0c |
| `FULL pilot` | approbation d'enveloppe 422 `provider_unavailable: vt` | diagnostic en cours (configuration de capacité fake vs gate de production) | CI-0c |

## 3. Règle de clôture

Chaque échec doit être reproduit localement, corrigé sans supprimer
d'assertion, sans `skip`/`xfail`/`deselect` et sans désactivation de job.
Les commandes équivalentes doivent être vertes sur le HEAD intégrateur avant
push. Le passage GitHub Actions n'est constatable qu'après push ; il est
rapporté séparément dans `FINAL_AUDIT_AND_GIT_HANDOFF.md`.
