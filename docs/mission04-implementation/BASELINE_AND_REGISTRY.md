# Mission 04 / Phase 2 — Baseline et registre

**Date d'ouverture :** 2026-10-24
**Nature :** implémentation locale incrémentale, validation hors ligne, résolution
de conception S3. Aucun déploiement, aucun run CTI réel, aucune migration live.

## 1. SHA et delta vérifiés au démarrage

| Élément | Valeur vérifiée |
|---|---|
| HEAD local au démarrage | `e120a36f1afa08261fdf0276e0200872973ad8f2` |
| Branche au démarrage | `main` |
| Arbre de travail | propre (`git status --porcelain` vide) |
| `origin/main` (ref de suivi locale, `git rev-parse`) | `e120a36f1afa08261fdf0276e0200872973ad8f2` |
| `git fetch origin --dry-run` | aucune mise à jour annoncée |
| SHA de code étudié par Mission 03 | `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` |
| Delta `aa0cef3e..e120a36f` | 29 fichiers, +6815 lignes, **tous** sous `docs/mission03-architecture/` |
| Fichiers de code/test/migration dans le delta | **aucun** (filtre `grep -v '^(docs/|\.prompts/|_work/)'` vide) |

Conclusion : les références `path:line` et les oracles de Mission 03 restent
valides ; le delta est purement documentaire. Aucun travail préexistant n'a été
écrasé.

## 2. Branches et worktrees créés

| Rôle | Branche | Worktree | Base | SHA final |
|---|---|---|---|---|
| Intégration locale | `mission04/implementation` | `/home/nill/perso/work/AutoWork` | `e120a36f` | `e8b80c5d` |
| L1 (non intégré) | `mission04/l1-p10-escaped` | `/home/nill/perso/work/AutoWork_m04_l1` | `e120a36f` | `b9115d0e` |
| L3 | `mission04/l3-audit-pg` | `/home/nill/perso/work/AutoWork_m04_l3` | `e120a36f` | `1b645ecb` |
| L20 | `mission04/l20-advisor-prepare` | `/home/nill/perso/work/AutoWork_m04_l20` | `e120a36f` | `f667369a` |
| S3-D (conception) | `mission04/s3d-design` | `/home/nill/perso/work/AutoWork_m04_s3d` | `e120a36f` | `e120a36f` |
| L2 | `mission04/l2-p10-refusal` | `/home/nill/perso/work/AutoWork_m04_l2` | `df9e0910` | `81b59e2d` |
| L6 | `mission04/l6-suricata-reparse` | `/home/nill/perso/work/AutoWork_m04_l6` | `df9e0910` | `d13ac067` |
| Scouts L4–L6 | `mission04/optional-scouts` | `/home/nill/perso/work/AutoWork_m04_opt` | `df9e0910` | `df9e0910` |

Aucun push, aucun merge sur `main`, aucun déploiement. `main` et
`origin/main` restent à `e120a36f`.

## 3. Environnement réellement disponible

| Élément | État observé | Conséquence |
|---|---|---|
| Python backend | 3.12.4 (`uv sync --locked --python 3.12` dans chaque worktree) | conforme à la règle 3.12 |
| uv | 0.12.5 | commandes `uv run --python 3.12` |
| PostgreSQL jetable | service `cti-bulletin-postgres-test-1` déjà `healthy` (profil `integration-test`, tmpfs, port 55432) | utilisable pour L3 |
| Test d'intégration de contrôle | `tests/integration/test_operator_context_summary_postgres.py` → **1 passed in 1.90s** | l'infrastructure jetable fonctionne |
| Stack live | `postgres`, `minio`, backend et workers sont up | **non touchée** : aucune commande ne cible ces services |
| Typst | non requis par les lots fermes L1/L2/L3/L20 | L5 seulement si activé |
| Fournisseurs / Bridge | non appelés | aucune preuve live |

## 4. Baseline de tests au SHA de départ

| Commande | Résultat |
|---|---|
| `cd backend && uv run --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short` | **58 passed in 0.53s** |
| `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_operator_context_summary_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"` | **1 passed in 1.90s** |

Ces deux exécutions sont des baselines réelles de Mission 04, pas des résultats
historiques de Mission 02/03.

## 5. Registre compact des lots

| Lot | Owner | Base SHA | État | RED/GREEN | Reviewer | Artefact | Risque | Dépendance |
|---|---|---|---|---|---|---|---|---|
| L1 | Luna xhigh `c8e8ed99` | `e120a36f` | **PARTIAL — NON INTÉGRÉ** ; branche `b9115d0e` préservée | 3 tentatives ; R4 REVISE (MEDIUM : claim non fermé + LF efface une preuve CTI jusqu'à EOF) ; 83 passed sur la branche | Luna xhigh `016a51ad` | `_work/M04_L1_NOTES.md`, `_work/M04_L1_REVIEW*.md` | fuite corrigée mais perte de preuve résiduelle ; `NEEDS_DECISION_P10_VERSION` | condition R4 : borner le repli à LF/CRLF |
| L3 | Luna xhigh `92e1c7ee` | `e120a36f` | **APPROVED + INTÉGRÉ**, commit `600a317f`, merge `eea2c1f5`, tête de branche `1b645ecb` | PG `1 passed in 1.02s` après 2 corrections d'oracle ; revalidations `1.19s`/`1.30s`/`1.36s` | Luna xhigh `b36fafce` (R2 `APPROVED`) | `_work/M04_L3_NOTES.md`, `_work/M04_L3_REVIEW*.md` | Docker refusé côté agent ; MinIO non prouvé | aucune |
| L20 | Luna xhigh `d60394d4` | `e120a36f` | **APPROVED + INTÉGRÉ**, commit `d2a2029f`, merge `df9e0910`, tête `f667369a` | `16 passed` ; régressions `168 passed / 1 deselected` ; Ruff vert | Luna xhigh `b2c96a9e` (R2 `APPROVED`) | `_work/M04_L20_NOTES.md`, `_work/M04_L20_REVIEW_R2.md` | module dormant ; test de collision observation-only à ajouter | aucune |
| S3-D A | Luna xhigh `9214b4e1` | `e120a36f` | ANALYSE COMPLÈTE | n/a | couvert par la revue du contrat | `_work/S3D_A_LOCKS_WRITERS.md` | conception seulement ; isolation UNKNOWN | aucune |
| S3-D B | Luna xhigh `129cb16b` | `e120a36f` | ANALYSE COMPLÈTE | n/a | couvert par la revue du contrat | `_work/S3D_B_OWNER_ROUTE_GATEWAY.md` | conception seulement | aucune |
| S3-D C | Luna xhigh `54a209cf` | `e120a36f` | ANALYSE COMPLÈTE | n/a | couvert par la revue du contrat | `_work/S3D_C_VERSION_SETTLEMENT.md` | conception seulement | aucune |
| S3-D synthèse | Luna xhigh `beebbb1c` | `e120a36f` | CONTRAT RÉDIGÉ + REVUE INDÉPENDANTE | verdict indépendant `S3_BLOCKED_DESIGN` ; F1–F4 PARTIAL | Luna xhigh `a64e5f34` | `S3_UNBLOCKING_CONTRACT.md`, `_work/S3D_CONTRACT_REVIEW.md` | 5 findings HIGH ; décisions humaines | A/B/C |
| L2 | Luna xhigh `3b2398e5` | `df9e0910` (sans L1) | **APPROVED + INTÉGRÉ**, commit `5ab00368`, merge `e8b80c5d`, tête `81b59e2d` | `69 passed` ; 12 ciblés ; Ruff/mypy ciblés verts | Luna xhigh `a9de7356` (R3 `APPROVED`) | `_work/M04_L2_NOTES.md`, `_work/M04_L2_REVIEW*.md` | statut absent documenté ; refus terminal ; replay | L1 non intégré (indépendant) |
| L4–L6 | Luna xhigh `8a945fcc` (impl) ; scope `8af0ac62` | `df9e0910` | L5 **NO_GO** ; L6 **APPROVED + INTÉGRÉ** (`40f1ac27`, merge `6db6b71c`, tête `d13ac067`) | 18 tests reparse+composition ; Ruff/mypy ciblés verts | Luna xhigh `303a8f64` | `_work/L5_SCOUT.md`, `_work/L6_SCOUT.md`, `_work/L6_SCOPE_REVIEW.md`, `_work/M04_L6_*.md` | P3 : forme partielle générique sur échec de parsing ; worker non exposé | après lots fermes |

## 6. Autorisations et interdictions en vigueur

Autorisé : code backend local, tests hors ligne, nouveau test L3 sur PostgreSQL
jetable, worktrees isolés, documentation et commits locaux, analyses S3 en
lecture seule.

Interdit : push, merge sur `main`, déploiement, migration live, appel
fournisseur/Bridge/VT/Shodan/Retrohunt, scan, upload, exécution d'échantillon,
run CTI réel, mutation PostgreSQL/MinIO live, publication/G3, implémentation
S3 de production, cutover advisor.

Le verdict d'entrée `GO_NARROW_ONLY` et le statut `S3_BLOCKED_DESIGN` restent
la référence. Aucun lot de code S3 n'est autorisé par cette mission.

## 7. État final vérifié (2026-10-24)

| Élément | Valeur finale |
|---|---|
| `main` / `origin/main` | `e120a36f1afa08261fdf0276e0200872973ad8f2` (inchangés) |
| `mission04/implementation` (intégration code/tests) | `e8b80c5d35e2c14a23cb521e9d899f1301a15056` |
| Lots intégrés | L2, L3, L20, L6 |
| Lots non intégrés | L1 (`PARTIAL`, branche `b9115d0e`), L5 (`NO_GO`), L4 (non lancé) |
| Validation croisée | **103 passed** (P10+L2+L6+L20) ; **152 passed / 1 deselected** (advisor) ; PostgreSQL L3 **1 passed** |
| Lint/type | Ruff **845 fichiers** ; mypy ciblé **4 modules** ; `git diff --check` propre |
| Contrat S3 | SHA-256 final du fichier livré (addendum parental inclus, espaces de fin de ligne normalisés) : `9c7539050c3804a3cf1143f5f7c784c46c9f69665cde7b5d0e2a89693a01ae03` ; hash historique du corps avant addendum, contrôlé par la revue : `b2a1bc94…e24ce8` |
| Gate global | **`GO_NARROW_PARTIAL`** |
| Gate S3 | **`S3_BLOCKED_DESIGN`** |
| Gate replay | **`BLOCKED_OFFLINE_E2E`** |
