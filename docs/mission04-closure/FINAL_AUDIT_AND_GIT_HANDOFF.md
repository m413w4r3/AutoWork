# Mission 04 closure — audit final et handoff Git

**Date :** 2026-10-25
**Baseline :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Branche de clôture :** `mission04/closure-after-main`
**Statut global :** gates locaux PASS ; merge/push final renseignés après
répétition du préflight. Aucun lot partiel n'est présenté comme terminé.

## 1. Préflight des effets de push (audité le 2026-10-25)

| Élément | Constat | Source |
|---|---|---|
| Workflows GitHub versionnés | un seul : `.github/workflows/ci.yml` (jobs `backend`, `frontend`, `typst-renderer-smoke`, `workflow-browser`, `compose`) | `.github/workflows/` |
| Déclencheur | `push` et `pull_request` uniquement | `ci.yml` |
| Déploiement / publication / migration live / scan | aucun job, step, secret, variable d'environnement ni `environment:` de ce type | `ci.yml` |
| Workflows distants | `CI` (ce fichier) + `Dependency Graph` (workflow dynamique GitHub, pas un déploiement) | `gh api .../actions/workflows` |
| Protection de `main` | **non protégée** (404 « Branch not protected ») ; aucun ruleset | `gh api .../branches/main/protection`, `.../rulesets` |
| Hooks de dépôt | aucun | `gh api .../hooks` |
| Environnements GitHub | aucun | `gh api .../environments` |
| Runs récents sur `main` | tous de type `push`, workflow `CI`, aucun déploiement observé | `gh run list --branch main` |

**Conclusion préflight :** un push non forcé sur `main` déclenche uniquement le
workflow `CI` (lecture du code, tests, rendu Typst hors ligne, compose config).
Aucun effet opérationnel non autorisé n'est identifié. Aucune protection de
branche à contourner. Le préflight a été répété sur le HEAD final avant push.

## 2. État Git attendu

| Élément | Valeur |
|---|---|
| `origin/main` de départ | `d7cc6b590d3be41dae10299cc03364771fe6bda1` |
| Branche de clôture | `mission04/closure-after-main` |
| Merge final | **effectué localement après gates ; SHA dans le rapport final** |
| Push | **non forcé après préflight répété** |
| Force-push / réécriture | interdit et non utilisé |

## 3. Résultats des gates locaux

| Gate | Résultat |
|---|---|
| Backend hors intégration | **4904 passed, 2 skipped, 31 deselected** |
| PostgreSQL d'intégration | **343 passed** |
| Workflow FAST / FULL | **22 / 32 passed**, gardes JUnit OK |
| Navigateur W10 / J2 | **1 / 1 passed** (PG réel, Typst/Poppler pour J2) |
| mypy repo-wide | **0 erreur, 820 fichiers** |
| Ruff + format | **847 fichiers propres** |
| Frontend lint/typecheck/tests/build | **39 fichiers / 293 tests**, build vert |
| Frontend e2e mock | **13 passed** |
| Replay transverse hors ligne | **338 passed**, `OFFLINE_REPLAY_PASS` |
| Scripts de garde CI | **5 tests**, OK |

## 4. Verdict et blocages

**Verdict : `GO_MAIN_AUDIT`.**
Gates séparés : **`S3_BLOCKED_DESIGN`**, **`ADVISOR_CUTOVER_BLOCKED_BY_S3`**,
**`OFFLINE_REPLAY_PASS`**.

Risques résiduels :

- anciennes preuves de refus sans objet canonique ou >64 KiB : restent
  `NEEDS_REVIEW` jusqu'à réconciliation humaine ;
- issues externes ambiguës : réservation maintenue et diagnostic framework,
  aucune sortie d'adaptateur archivée ;
- J2 est un parcours navigateur réel long (timeout porté à 300 s) ;
- aucun smoke live provider/Bridge/stack/MinIO n'est exécuté par cette
  clôture ; S3 reste bloqué et aucun cutover n'est effectué.
