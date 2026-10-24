# Mission 01 V3.2 — audit final de reprise

**État :** **PARTIAL**, clôturé le 2026-10-24 avec réserves et preuves.
Aucun push vers `origin`, déploiement, accès live, appel fournisseur/CTI,
acquisition, scan, upload ou donnée CTI privée n'a été effectué. Le merge
local dans `main` (`db9aa1d3`) a été effectué sur demande utilisateur
explicite et n'a **pas** été poussé ; `origin/main` reste `31ceeac`.

**Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`.
**Branche d'intégration :** `chore/mission01-integration`.
**État code vérifié :** `9523cc4c` (dernier merge de code : B1B, L2, A1, Q1).
**`main` local fusionné :** `db9aa1d3` (merge de `chore/mission01-integration`,
non poussé). Les branches F6 et B1A1 atomique ne sont **pas** dans `main`.
Cette documentation est committée après ce merge.

## Périmètre réellement exécuté

- Tests exclusivement hors ligne, fixtures synthétiques/expurgées.
- PostgreSQL réel, concurrence multi-processus, transport Bridge réel,
  providers CTI et validateurs Typst réels restent **UNKNOWN**.
- Aucune migration, aucun changement de `0001_baseline`/`0002`, aucun secret.
- Les branches non acceptées (F6, B1A1 atomique) ne sont **pas** fusionnées.

## Matrice des lots

| Lot | Verdict final | Code / intégration | Revue indépendante | Restes |
|---|---|---|---|---|
| S0 baseline | PASS (runtime `UNKNOWN`) | `e26c9a7` → `48ba143` | PASS | runtime |
| B0A F1–F5 | acceptés | `cd69eca`/`498c2da` → `f67181e` | FAIL/PARTIAL historique | F6 |
| **B0A F6** | **BLOCKED/PARTIAL** | `83aa615`, **non intégré** | `2e22ca1b` **FAIL** : 3 HIGH + 2 MEDIUM | contournements `dns.raw`, base64 avec espaces, budget non cumulatif ; voir `F6_REVIEW.md` |
| B0B | PASS_WITH_CHANGES | `41c8869`+`5add5df` → `a772cc9` | `230380d` | PostgreSQL réel `UNKNOWN` |
| B1A1 (protections) | FAIL/PARTIAL historique | `2f88a42` → `2cafb37` | `4317f46` FAIL | clé canonique + atomicité |
| **B1A1 clé canonique** | **BLOCKED** | aucune identité inter-appels | `95f180aa` PARTIAL | seule `ModelRun run.id:aN` par tentative ; `operation_key` client + fingerprint heuristique ; ne pas rouvrir l'heuristique |
| **B1A1 atomicité** | **BLOCKED** | `699200b`→`7af1d62`→WIP `32513ca`, **non intégré** | `5d82f90d` FAIL puis `c69e51e3` FAIL | guard advisor non forgeable absent ; chemin positif au préflight ; voir `B1A1_REVIEW.md` |
| B1A2 | PASS_WITH_CHANGES | `01346e2`/`5484375` → `591eeca` | `8597053` | `turn` sans deadline (≤300 s) |
| **B1B temporalité/reprise** | **PASS** | `e4850d2`→`fd397de`→`4604ac1`→`a94986f`, intégré `10b0e35` | `b1a2f600` FAIL, `66c9488e` PASS_WITH_CHANGES, `e8bd922a` PASS_WITH_CHANGES, `772a15d5` **PASS** | PostgreSQL concurrent, transport réel |
| L1 / L06 | PASS_WITH_CHANGES | `e4772fa` → `0483f61` | `bb7b8b4` | — |
| P10 divulgation | **DEFER** (préexistant) | aucun override | 3 FAIL historiques | garde incomplète ; défaut 96 000 restauré |
| **L2 pagination/keyset** | **PASS_WITH_CHANGES** | `ef1bc61`→`4f2a941`→`6143099`→`0c54ed9`, intégré `36eb5326` | `fd227a85` FAIL, `d676bd6a` PASS_WITH_CHANGES, `45b350c4` PASS_WITH_CHANGES, `24b810f2` PASS_WITH_CHANGES | 1 MEDIUM résiduel : catégorie d'omission reprise comme attendue (limite 3 réparations) ; PostgreSQL/perf `UNKNOWN` |
| **A1 projection stratégie** | PASS_WITH_CHANGES → **résiduel corrigé** | `1f0e813` → `545d32a` ; résiduel `fbe5d36`+`bd35278`+`749a3f2` intégré `69d3b904` | `2e75d94` PASS_WITH_CHANGES ; `fe7f765f` PASS_WITH_CHANGES ; `cdb8f69e` **PASS** | PostgreSQL/lifespan `UNKNOWN` |
| **Q1 éval anti-slop** | **PASS_WITH_CHANGES** | `dd925ff`→`e1779f8`→`a3976c3`, intégré `9523cc4` | `1c095748` PASS_WITH_CHANGES ; `cac542a0` PASS_WITH_CHANGES | 16 cas, SHA corpus `a73fdb9…684e` ; baseline et patch 2 passed |
| **Q2 patch prompt** | **DEFERRED/NO-GO offline** | — | scout `a4991603` | aucune preuve sémantique offline ; changerait `INVESTIGATION_PROMPT_VERSION`/idempotence |
| T Typst | rapport PASS ; **BLOCKED_MISSING_REFERENCE** | `cae3115` → `7dc4b0e` | rapport | bundle `CascadiaCode` absent, `var/` non inspectable ; V4 derrière validation éditoriale |

## Tests réellement exécutés

| Vérification | Résultat | Portée |
|---|---|---|
| **Suite backend complète (intégration)** | **4667 passed, 34 failed, 2 skipped, 30 deselected** (64,50 s) | `pytest --ignore=tests/integration -m "not integration"` sur l'intégration B1B+L2+A1+Q1 (`9523cc4`) |
| **Suite backend complète (main fusionné)** | **4668 passed, 33 failed, 2 skipped, 30 deselected** (80,65 s) | même commande sur `main` @ `db9aa1d3` ; un test Typst `w17` passe sur main, aucun nouvel échec |
| B1B oracle | **241 passed** | 8 fichiers B1B/B1A1/B1A2/gateway/bridge |
| B1B élargi | **294 passed, 6 skipped** | sélection bridge/composition/service |
| L2 oracle | **342 passed** | 14 fichiers contexte/operator/API |
| B1B+L2 combiné | **556 passed** | intégration `36eb5326` |
| A1 oracle | **79 passed** | API/expériences/invitation/operator tool |
| A1+L2+B1B combiné | **194 passed** | intégration `69d3b904` |
| Q1 baseline | **2 passed**, seuils verts | 16 cas |
| Q1 patch | **2 passed** | `ANTI_SLOP_EVAL_VARIANT=patch` |
| F6 (branche) | **162 passed** | 4 suites advisor ; revue FAIL malgré l'oracle |
| B1A1 atomique (branche) | **148 passed** | 5 suites ; branche non intégrée |
| B1A1 atomique WIP advisor | **1 failed, 4 passed** (`-k advisor`) | chemin positif `advisor_preflight_insufficient_evidence` |
| Frontend | **293 passed + typecheck** | **rejoué sur main fusionné** (`pnpm test --run`, `pnpm typecheck`) |
| PostgreSQL intégration | **non exécuté** | `UNKNOWN` |
| Typst réel | **non exécuté** | bundle polices absent |

### Triage des échecs de la suite complète (34 intégration / 33 main)

- **32** échecs Typst réels dans l'intégration : `FontBundleInvalidError:
  Manifest-listed file is missing: 'CascadiaCode/ttf/CascadiaCode.ttf'`,
  reproduits sur `058bb4a`.
- **2** échecs préexistants : `test_confined_analysis_dispatch`
  (`test_tool_registry_dispatch_runs_capa_and_strings_through_worker`,
  CAPA indisponible ; `test_mwcp_allowlisted_fixture_parser_is_invoked_in_worker`,
  parseur MWCP absent).
- Sur `main` fusionné, `test_w17_real_typst_draft_preview_preserves_structure_and_limits`
  passe ; les **33** échecs restants sont 31 Typst + les 2 confined/mwcp.
- **2 skips** : image analysis-worker non construite ; Ghidra absent.
- Aucun échec nouveau attribuable aux lots B1B/L2/A1/Q1.

## Revues indépendantes

Toutes les revues ci-dessus ont été exécutées par des sessions GPT-6 Luna
xhigh distinctes des implémenteurs, en lecture seule, avec rejeu de l'oracle.
Les verdicts et SHA sont consignés dans `B1B_REVIEW.md`, `L2_REVIEW.md`,
`A1_REVIEW.md`, `F6_REVIEW.md`, `B1A1_REVIEW.md`, `Q_ANTI_SLOP_EVAL.md`.
Aucun `PASS` n'est déclaré sans test exécuté ou revue effective.

## Résidus et gates

- **F6** : 3 HIGH confirmés (chemin `dns.raw`, base64 avec espaces, budget
  décodé non cumulatif) + 2 MEDIUM. Lot arrêté après 3 tentatives, branche
  non intégrée. Une reprise doit traiter structurellement ces chemins.
- **B1A1 clé canonique** : `BLOCKED`. Aucun identifiant d'effet inter-appels
  fourni par l'adaptateur ; seule une clé par tentative `ModelRun run.id:aN`.
- **B1A1 atomicité** : `BLOCKED`. La séquence SQL du service/dispatcher est
  correcte et revue, mais l'état du guard advisor est porté par des booléens
  forgeables ; il faut un capability/lease non forgeable créé par le
  dispatcher, plus une fixture de préflight valide. Branche non fusionnée.
- **P10** : `DEFER` préexistant, aucun override ; garde de divulgation
  incomplète.
- **T** : `BLOCKED_MISSING_REFERENCE` ; aucun bundle `CascadiaCode` complet
  dans le dépôt et `var/` n'est pas inspectable. V4 non restauré.
- **Q2** : `DEFERRED/NO-GO offline` ; nécessite une évaluation modèle
  autorisée séparément.
- **PostgreSQL réel, concurrence multi-processus, pression du pool,
  acceptation fournisseur, transport Bridge réel, providers CTI** :
  `UNKNOWN`.
- **Coûts** fournisseur, tokens et Paseo : **`UNKNOWN`**. Aucun reçu ni
  facture consulté.

## Décision de clôture

La mission reste **PARTIAL**. Sont livrés et acceptés : B1B (PASS), L2
(PASS_WITH_CHANGES avec résidu documenté), A1 + résiduel (PASS), Q1
(PASS_WITH_CHANGES), les lots antérieurs B0B/B1A2/L1/T (rapport). Sont
explicitement **non acceptés** : F6 (BLOCKED), B1A1 clé canonique (BLOCKED),
B1A1 atomicité (BLOCKED), P10 (DEFER), Q2 (DEFER/NO-GO), T suite
(BLOCKED_MISSING_REFERENCE).

Prochaines actions permises, sous autorisation humaine distincte : reprise
F6 avec nouvelle conception, capability/lease de dispatch, évaluation Q2 avec
modèle autorisé, validation PostgreSQL concurrente, bundle de polices Typst
et validation éditoriale V4, politique P10. Toute mesure live Bridge/VT/
Shodan, réconciliation `needs_review`, migration, publication/G3, modification
de budget G2 et tout **push vers `origin/main`** exigent une autorisation
séparée. Le merge local dans `main` (`db9aa1d3`) a été effectué sur demande
utilisateur explicite ; il n'a pas été poussé. F6 et B1A1 atomicité restent
hors de `main`.
