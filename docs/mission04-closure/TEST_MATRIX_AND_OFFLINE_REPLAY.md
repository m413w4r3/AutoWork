# Mission 04 closure — matrice de tests et replay hors ligne

**Base :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Environnement :** Python 3.12.3, PostgreSQL jetable `localhost:55432`
(service `cti-bulletin-postgres-test-1`, tmpfs), Typst 0.15.1 et D2 0.9.0
installés dans `/tmp/aw-tools`, bundle de polices verrouillé
`/tmp/aw-tools/typst-fonts`, `TMPDIR` sous `$HOME`. Aucun accès fournisseur,
Bridge, VT, Shodan, Retrohunt ou acquisition.

Cette matrice est mise à jour à chaque lot. Un test non exécuté est marqué
**NON EXÉCUTÉ**, jamais PASS.

## 1. L1-R5 + WIRE(P10)

| Oracle | Commande | Résultat |
|---|---|---|
| Suite P10 (L1 + L2 + rendu) | `cd backend && uv run --python 3.12 pytest tests/test_invariant_proposals.py -q --tb=short` | **99 passed** après le correctif R5 |
| R5 premier verdict | revue indépendante Luna xhigh | **REVISE (HIGH)** : suffixe `}FIXTURE_AUTH_SUFFIX` fuyait après un scalaire non fermé |
| Correctif R5 | commit `5a23a291` | délimiteurs bruts avant quote de fermeture = ambigus ; scalaire non borné sans LF/CRLF → `ProposalContractError`, zéro tour externe |
| Sonde R5 stricte | `_normalize_context → _render_p10_prompt` | **44/44 PASS** ; refus sur `}`/`]`/`,` non bornés ; LF/CRLF et preuve CVE préservés ; linéarité 0,0132/0,0266 s |
| R5 re-revue | Luna xhigh | **en attente** |
| Régressions P10/composition/signature | `pytest tests/test_invariant_proposals.py tests/test_investigation_composition.py tests/test_signature_bridge.py -q` | **111 passed** |
| R4 seam réel : LF et CRLF, secret absent, CVE byte-for-byte, pas de marqueur non borné | sonde indépendante `_normalize_context → _render_p10_prompt` | **8/8 PASS** |
| Guillemet interne + `}`/`]`, 8 variantes | sonde indépendante | **8/8 PASS** |
| Antislashs finaux 0–7, délimiteur conservé | sonde indépendante | **16/16 PASS** |
| Auteur innocent, champs CTI `api_key`/`auth` conservés | sonde indépendante | **3/3 PASS** |
| Repli non borné observable | sonde indépendante | **2/2 PASS** |
| Coût linéaire 1 600 / 3 200 labels | sonde indépendante | **0,0049 s / 0,0107 s** |
| Identité v5 distincte de v4 | sonde indépendante | **PASS** |
| Revue indépendante R5 | Luna xhigh | **en attente** |

## 2. CI-0 — correctifs mécaniques

| Oracle | Commande | Résultat |
|---|---|---|
| Frontend lint | `cd frontend && pnpm lint` | **PASS** (ESLint + Prettier) |
| Frontend typecheck/tests/build | `pnpm typecheck && pnpm test --run && pnpm build` | **PASS** : 39 fichiers / **293 tests**, build Vite vert |
| Frontend e2e Playwright | `pnpm test:e2e` | **NON EXÉCUTÉ — BLOCKED_ENV** : le webServer Vite échoue sur `EMFILE` d'inotify après deux tentatives, limites fd élevées ; l'environnement local héberge de nombreux watchers. À vérifier en CI. |
| Import du serveur navigateur | parse AST + import des constantes publiques | **PASS** |
| Smoke Typst inline (fixture canonique corrigée) | compilation Typst 0.15.1 + polices verrouillées + `pdftotext` | **PASS**, PDF non vide |
| mypy repo-wide | `cd backend && uv run --python 3.12 mypy src tests` | **60 erreurs au départ → 35 après CI-0a, toutes dans les 5 fichiers L4 (attendu : 0 après L4)** |
| CI-0a tests ciblés | `pytest tests/test_model_gateway.py tests/test_investigation_vt_tools.py tests/test_advisor_consultation.py -q` | **104 passed** (vérifié parent) |
| CI-0a integration audit export | collecte seulement | **NON EXÉCUTÉ en entier** (à rejouer sur HEAD intégrateur) |
| Workflow FAST (W13-D5, W13-D7, W14-A, W14-E) | workflow emulator + PostgreSQL jetable | **en cours (CI-0b)** |
| Workflow FULL (W15, W18, pilote) | workflow emulator + PostgreSQL/Typst/D2 jetables | **en cours (CI-0c)** |

## 3. Lots en cours

| Lot | Oracles prévus | Statut |
|---|---|---|
| L4 (D1–D4) | full/paged, stale cursor, `context_sha256`, latest findings, tie timestamps, PostgreSQL réel | en cours |
| L5 (D5–D6) | lineage acteurs, PDF réellement compilé, occurrences, avis vs base | en cours |
| L21 (D8–D9) | parse/reparse v4, refused/partial/invalid/ambiguous, alias, source_ref, legacy explicite | en cours |
| SAMPLE (D7) | transport fake permissif, vetos explicites → zéro appel | en attente de L4 |

## 4. Replay transverse hors ligne

**Statut : NON EXÉCUTÉ à ce stade.** Il doit au minimum couvrir P10
`refused/partial/succeeded` → P09, advisor prepare/parse/reparse, contexte
full/paged, export d'audit et PDF avec/sans avis, sur PostgreSQL jetable et
fakes, sans réseau. Toute étape non atteinte sera rapportée comme
`BLOCKED_OFFLINE_E2E` ou couverture partielle précise.
