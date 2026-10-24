# Mission 03 / Phase 1 — Baseline et périmètre

**Date :** 2026-10-24
**Nature :** analyse architecturale, conception et préparation d'implémentation.
Aucun refactoring de production n'est exécuté dans cette phase.

## 1. SHA, branche et delta

| Élément | Valeur vérifiée |
|---|---|
| HEAD local au démarrage | `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` |
| `origin/main` (`git ls-remote`) | `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` |
| Branche de travail | `mission03/architecture-design` (créée depuis `main`) |
| État initial | arbre propre, aucun changement suivi |
| Delta vs SHA de référence | aucun delta de code ; branche documentaire uniquement |

Le SHA de référence de la mission est donc confirmé à l'ouverture. Toute
observation `path:line` des livrables porte sur ce SHA. La branche ne contient
que des documents ; aucun code, test, contrat ou migration n'est modifié.

## 2. Environnement réellement disponible

| Élément | État observé | Conséquence |
|---|---|---|
| Python backend | 3.12.3 via `backend/.venv` | conforme à la règle 3.12 |
| uv | 0.12.5 | `make`/`uv run --python 3.12` utilisables |
| Node / pnpm | v24.18.0 / 11.20.0 | frontend non requis par cette phase |
| Typst | `/snap/bin/typst` cassé (`snap-confine`) ; `/snap/typst/current/bin/typst` 0.15.1 | 1 test Typst réel exécuté avec `TYPST_BINARY` fonctionnel ; le binaire par défaut reste inutilisable |
| Bundle de polices | `chpTypst/` présent (CascadiaCode, HankenGrotesk, manifestes) | rendu hors ligne possible |
| PostgreSQL | serveur 16.15 en écoute locale ; `TEST_POSTGRES_ADMIN_DSN` non défini ; profil compose `postgres-test` (`postgres:16-alpine`, tmpfs) | 3 tests PostgreSQL jetables exécutés ; aucune base live touchée |
| Docker | 27.1.1 | utilisé uniquement pour le service jetable `postgres-test` (tmpfs, port local 55432) ; aucune autre stack |
| Fournisseurs / Bridge réels | non appelés | aucune preuve live dans cette mission |

Aucun accès à `.env`, `.llms_key`, `.secrets`, `var/`, `chatGPT_Answers/`,
archives CTI privées ni caches de dépendances n'a été effectué.

## 3. Sources d'autorité lues

- `AGENTS.md`, `backend/AGENTS.md` ; `docs/README.md` pour la précédence.
- `docs/adr/ADR-0007-frozen-baseline-and-c2-migration.md` (migrations).
- `.agents/skills/codebase-design/SKILL.md`, `DEEPENING.md`,
  `DESIGN-IT-TWICE.md`.
- Contrats actifs pertinents : `docs/investigation_governance.md`,
  `docs/persistence_and_storage.md`, `docs/investigation_contracts.md`,
  `docs/model_conversations.md`, `docs/investigation_operations.md`.
- Conclusions des Missions 01–02 (voir §5), utilisées comme indices, jamais
  comme autorité sur le runtime actuel.
- Aucun `GLOSSARY.md` applicable n'existe au SHA étudié ; le vocabulaire vient
  des contrats, des ADR et du skill `codebase-design`. Aucun glossaire
  canonique n'est inventé.

## 4. Périmètre exécuté

### Autorisé et effectivement prévu

- Lecture ciblée du code, des tests, des ADR et des rapports existants.
- Sous-sessions Paseo GPT-6 Luna xhigh indépendantes, en lecture seule, avec
  artefacts sous `docs/mission03-architecture/_work/`.
- Tests hors ligne ciblés, si nécessaires et sûrs.
- Commits documentaires locaux sur `mission03/architecture-design`, puis merge
  local dans `main` sur demande explicite de l'utilisateur. Aucun push.

### Explicitement exclu

- Modification du code métier, des tests, contrats ou migrations.
- Push, déploiement, modification du run CTI existant.
- Appels fournisseurs, scans, acquisitions, soumissions, publication G3.
- Mutation PostgreSQL/MinIO live, restauration ou opération destructive.
- Inspection des répertoires de secrets, archives CTI privées et caches.
- Utilisation d'identifiants opérationnels ou de preuves privées dans les
  prompts, journaux ou rapports.

## 5. Résultats historiques non rejoués (Mission 02)

Ces valeurs sont des observations historiques, pas des résultats de cette
mission :

- Backend : 4 708 passed, 2 skipped, 30 deselected ; Ruff vert ; 43 tests
  Typst ciblés ; mypy global 58 erreurs préexistantes dans 11 fichiers.
- S2 : `REVISE`, résidu de credential dans du texte JSON échappé.
- S3 : `BLOCKED_DESIGN`, aucune implémentation.
- Export d'audit réel PostgreSQL : validation incomplète (`UNKNOWN`).
- E2E CTI complet avec/sans échantillon : non rejoué.
- PDF réel inspecté : `nature=base_publication`, `opinion=None` ; ce PDF ne
  valide pas un avis final.

La suite complète n'est pas rejouée dans cette phase : la mission est
architecturale et les tests réellement exécutés sont consignés dans
`TEST_SURFACE.md` avec leur commande et leur résultat exacts.

## 6. Baseline de tests de cette mission

Voir `TEST_SURFACE.md` §2. Résumé vérifié : **375 tests offline uniques**
(414 exécutions), **3 tests PostgreSQL jetables** (concurrence
`operation_key`, contexte opérateur, publication V6) et **43 tests Typst
ciblés**. L'échec initial du test de publication V6 est un défaut
d'environnement (`/snap/bin/typst` cassé), corrigé par
`TYPST_BINARY=/snap/typst/current/bin/typst` ; il n'est pas compté comme un
échec de code.

Règle appliquée : un test n'est déclaré passé que s'il a été exécuté pendant
la mission ; les résultats historiques restent étiquetés comme tels ; un fake
n'est jamais une preuve PostgreSQL.

## 7. Contradictions et conflits documentaires

| Source A | Source B | Nature du conflit | Traitement |
|---|---|---|---|
| `docs/README.md` (hiérarchie documentaire) | journaux datés antérieurs | instructions historiques parfois obsolètes | les ADR/contrats et le code testé priment ; les journaux restent des indices |
| `docs/investigation_governance.md` : plafonds `CONTEXT_BYTES`/`CONTEXT_TOKENS` inscrits mais consommation « pas encore branchée » | politique S2 Mission 02 : aucun plafond de prompt AutoWork | deux couches différentes (enveloppe G2 vs prompt P10) | à confronter au code dans `ARCHITECTURE_MAP.md` ; aucune fusion silencieuse |
| Mission 01 : `mypy` global non vert | `backend/AGENTS.md` : `make typecheck-backend` | dette préexistante vs gate attendu | documenté comme risque de validation, hors périmètre de refactoring |

Aucun conflit n'est arbitré silencieusement dans les livrables.

## 8. Blocages et autorisations manquantes

- `BLOCKED_AUTHORIZATION` : aucune preuve live fournisseur, Bridge, VT,
  Shodan, Retrohunt ou acquisition ; aucune mutation PostgreSQL/MinIO.
- `UNKNOWN` : concurrence PostgreSQL multi-processus non exécutée dans cette
  phase ; idempotence/canonicalisation côté fournisseur non établie.
- `BLOCKED_DESIGN` : S3 conserve ce statut tant que le point de décision
  durable et les états consommés ne sont pas spécifiés et revus (voir
  `REVIEW_AND_HANDOFF.md`).

## 9. Sorties attendues

Six livrables sous `docs/mission03-architecture/` :
`BASELINE_AND_SCOPE.md`, `ARCHITECTURE_MAP.md`, `TEST_SURFACE.md`,
`DESIGN_OPTIONS.md`, `IMPLEMENTATION_PLAN.md`, `REVIEW_AND_HANDOFF.md`.
Les artefacts détaillés des sous-agents restent sous `_work/` et ne remplacent
pas ces six synthèses.
