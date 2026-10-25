# Mission 04 closure — registre compact des lots

**Base :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Branche de clôture :** `mission04/closure-after-main`
**Statut global :** **GO_MAIN_AUDIT** pour les lots autorisés ; gates locaux
PASS, merge/push non forcé après préflight. S3 reste bloqué et aucun cutover
n'est effectué.

| Lot | Owner | Branche / SHA | État | RED/GREEN réellement exécuté | Reviewer | Risque / dépendance |
|---|---|---|---|---|---|---|
| CI-0 petits correctifs | parent | `mission04/closure-after-main` / `ccff3444` | **COMPLETE sur HEAD final** | `pnpm lint` PASS ; import navigateur PASS ; compilation Typst + `pdftotext` PASS ; frontend 293 tests / 13 e2e | gate parent | dépend du HEAD final |
| CI-0a mypy mécanique | Luna high `1b63a297` | `mission04/ci0-mechanical` / `664674a4` (intégré `a3b2860f`) | **COMPLETE sur HEAD final** | mypy repo-wide **0 erreur / 820 fichiers** ; 104 tests ciblés ; intégration PG finale **343 passed** ; Ruff/format verts | parent | aucune |
| CI-0b workflow FAST | Luna xhigh `4a40947f` | `mission04/ci0-workflow` / `fe51494f` intégré `75d4943d` | **APPROVED, COMPLETE** | RED `4 failed` → **110 passed** sur la branche ; FAST final **22 passed** ; classification blocking/informational, rôles canoniques | Luna xhigh `a40c8f16` **APPROVED** | rôles mesurés obligatoires ; fixture PG retrohunt alignée |
| CI-0c workflow FULL | Luna xhigh `dcbc7694` | `mission04/ci0-protocol` / R6 `1f81fb13` intégré `378de279` | **APPROVED, COMPLETE** | service/B1/operator/W16 **183 passed** ; workflow FULL **32 passed** ; reprise RUNNING, verifier de preuve canonique, diagnostics opérateur | Luna xhigh `268f404e` **APPROVED** | preuves legacy/sans objet canonique et >64 KiB restent bloquées ; réconciliation humaine |
| L1-R5 + WIRE(P10) | parent | `mission04/closure-after-main` / `f92d848d` → `5a23a291` → `2a5987e4` → `aabf073b` | **APPROVED, COMPLETE** | 102 P10 passed ; R1 leak `}` corrigé par refus ; R2 sur-refus des valeurs fermées ponctuées corrigé par premier guillemet structurel ; sonde valide `}`/`]`/`,` acceptée, cas non bornés refusés ; linéarité 0,0138/0,0272 s | Luna xhigh R1 `REVISE`, R2 `REVISE`, **R3 `APPROVED`** (`96ecb538`) | secret/CTI, P10 v5 ; note LOW commentaire corrigée |
| L4 — D1–D4 | Luna xhigh `d92f6674` | `mission04/l4-context` / R2 `62c38baa` intégré `e930aafe` | **APPROVED, COMPLETE** | R2 **105 unit + 3 PG passed** (parent 108) ; offset legacy refusé 422, curseurs opaques liés au contenu, hash sémantique sans preuves de page | Luna xhigh `6e8b440b` **APPROVED** | gate d'intégration 401 passed avec L5/L21 |
| L5 — D5–D6 | Luna xhigh `ec7b863b` | `mission04/l5-pdf` / R3 `1c2ff3bc` intégré `02f4e764` | **APPROVED, COMPLETE** | R3 oracle PDF réel étendu (chaque source, date, URL, ordinal, timeline, citations) ; **79 passed** parent, base/avis, PG, IDs absents | Luna xhigh `cd40c7cf` **APPROVED** (compilation non reproduite dans son sandbox, parent l'a exécutée) | reviewer n'a pas pu compiler Typst dans son env ; parent 401 passed |
| L21 — D8–D9 | Luna xhigh `8d483738` | `mission04/l21-advisor` / `5707a8d9` intégré `6c212bad` | **APPROVED, COMPLETE** | 214 tests advisor sur HEAD intégrateur ; mypy 4 fichiers propre ; R2 ferme credentials configurés, wire incomplet, statuts v4 | Luna xhigh `fa293266` **APPROVED** | L22 prep plan écrit ; aucun cutover |
| SAMPLE — D7 | Luna xhigh `bf770d41` | `mission04/sample-d7` / R7 `825aabd2` + sécurité `10454e8f`, intégré `bfe2c534` | **APPROVED, COMPLETE** | **262 passed, 1 skipped** (archive Run12 absente) ; octets/hex/Base64/métadonnées/associations, vetos zéro appel, sources de restriction agrégées, URL signées IPv6/zone/chemins crochetés | Luna xhigh `2d8202c2` **APPROVED** (R5→R7 + 3 correctifs sécurité) | G3/license_restriction conservateur ; aucun cutover S3 |
| Replay transverse hors ligne | parent | HEAD `5f8a5ca3` | **PASS** | **338 passed** sur P10→P09, advisor prepare/parse/reparse, contexte full/paged, audit export, PDF V5/V6, PostgreSQL jetable + fakes + Typst réel | gate parent | aucun fournisseur/Bridge live |
| WIRE — inventaire D10 | scout Luna xhigh `e9bf3fb3` | lecture seule | **APPROVED, COMPLETE** | `BRIDGE_WIRE_INVENTORY.md` ; tous les prompts app actifs = texte/Markdown ; JSON modèle restant = extraction legacy dormante + normalizer sans caller | Luna xhigh `44f12811` **APPROVED** après 3 révisions | aucune modification de dépôt externe |
| S3 | — | — | **`S3_BLOCKED_DESIGN`** | aucun code/migration/route | — | hors périmètre |

## Règles de statut

- `IMPLÉMENTÉ` ne signifie pas approuvé : la revue indépendante et les gates
  croisés sur le HEAD intégrateur restent nécessaires.
- Un lot n'est `COMPLETE` que si ses oracles RED/GREEN, ses tests PostgreSQL /
  Typst réels et sa revue sont disponibles avec commandes et résultats.
- Un lot non approuvé reste sur sa branche ; aucun patch partiel masquant une
  preuve n'est intégré.
