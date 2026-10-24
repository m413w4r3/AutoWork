# Mission 04 closure — registre compact des lots

**Base :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Branche de clôture :** `mission04/closure-after-main`
**Statut global :** en cours — aucun lot n'est déclaré COMPLETE avant son
GREEN réel et sa revue indépendante.

| Lot | Owner | Branche / SHA | État | RED/GREEN réellement exécuté | Reviewer | Risque / dépendance |
|---|---|---|---|---|---|---|
| CI-0 petits correctifs | parent | `mission04/closure-after-main` / `ccff3444` | **CORRIGÉ, à vérifier sur HEAD intégrateur** | `pnpm lint` PASS ; import navigateur PASS ; compilation Typst + `pdftotext` PASS | gate parent | dépend du HEAD final |
| CI-0a mypy mécanique | Luna high `1b63a297` | `mission04/ci0-mechanical` / `664674a4` (intégré `a3b2860f`) | **VÉRIFIÉ PARENT** | mypy changé : 8 fichiers `Success` ; repo-wide **35 erreurs restantes, toutes L4** ; 104 tests ciblés passed ; Ruff/format/diff-check verts | parent | intégration test PG non exécutée par le worker, à rejouer sur HEAD |
| CI-0b workflow FAST | Luna xhigh `4a40947f` | `mission04/ci0-workflow` / `fe51494f` | **IMPLÉMENTÉ, revue en cours** | RED `4 failed` → **99 passed** (W13-D5/D7, W14-A/E + suites associées) ; Ruff/mypy/diff verts | Luna xhigh `a40c8f16` en cours | classification blocking/informational, rôles canoniques |
| CI-0c workflow FULL | Luna xhigh `dcbc7694` | `mission04/ci0-protocol` | **EN COURS (commit à faire)** | W15/W18/pilote verts ; oracle PDF normalisé sans perte de texte ; FLOSS probe corrigé | à lancer après commit | reprise/reconciliation, observations ambiguës |
| L1-R5 + WIRE(P10) | parent | `mission04/closure-after-main` / `f92d848d` → `5a23a291` → `2a5987e4` → `aabf073b` | **APPROVED, COMPLETE** | 102 P10 passed ; R1 leak `}` corrigé par refus ; R2 sur-refus des valeurs fermées ponctuées corrigé par premier guillemet structurel ; sonde valide `}`/`]`/`,` acceptée, cas non bornés refusés ; linéarité 0,0138/0,0272 s | Luna xhigh R1 `REVISE`, R2 `REVISE`, **R3 `APPROVED`** (`96ecb538`) | secret/CTI, P10 v5 ; note LOW commentaire corrigée |
| L4 — D1–D4 | Luna xhigh `d92f6674` | `mission04/l4-context` / R2 `62c38baa` intégré `e930aafe` | **APPROVED, COMPLETE** | R2 **105 unit + 3 PG passed** (parent 108) ; offset legacy refusé 422, curseurs opaques liés au contenu, hash sémantique sans preuves de page | Luna xhigh `6e8b440b` **APPROVED** | gate d'intégration 401 passed avec L5/L21 |
| L5 — D5–D6 | Luna xhigh `ec7b863b` | `mission04/l5-pdf` / R3 `1c2ff3bc` intégré `02f4e764` | **APPROVED, COMPLETE** | R3 oracle PDF réel étendu (chaque source, date, URL, ordinal, timeline, citations) ; **79 passed** parent, base/avis, PG, IDs absents | Luna xhigh `cd40c7cf` **APPROVED** (compilation non reproduite dans son sandbox, parent l'a exécutée) | reviewer n'a pas pu compiler Typst dans son env ; parent 401 passed |
| L21 — D8–D9 | Luna xhigh `8d483738` | `mission04/l21-advisor` / `5707a8d9` intégré `6c212bad` | **APPROVED, COMPLETE** | 214 tests advisor sur HEAD intégrateur ; mypy 4 fichiers propre ; R2 ferme credentials configurés, wire incomplet, statuts v4 | Luna xhigh `fa293266` **APPROVED** | L22 prep plan écrit ; aucun cutover |
| SAMPLE — D7 | Luna xhigh `bf770d41` | `mission04/sample-d7` depuis `6c212bad` | **EN COURS** | matrice positive/négative + bytes/Base64 + vetos à exécuter | Luna xhigh obligatoire | G3/license_restriction : mapping conservateur à challenger |
| WIRE — inventaire D10 | scout Luna xhigh `e9bf3fb3` | lecture seule | **APPROVED, COMPLETE** | `BRIDGE_WIRE_INVENTORY.md` ; tous les prompts app actifs = texte/Markdown ; JSON modèle restant = extraction legacy dormante + normalizer sans caller | Luna xhigh `44f12811` **APPROVED** après 3 révisions | aucune modification de dépôt externe |
| Offline replay transverse | à déléguer | à créer après intégration | **NON EXÉCUTÉ** | — | parent | dépend de tous les lots |
| S3 | — | — | **`S3_BLOCKED_DESIGN`** | aucun code/migration/route | — | hors périmètre |

## Règles de statut

- `IMPLÉMENTÉ` ne signifie pas approuvé : la revue indépendante et les gates
  croisés sur le HEAD intégrateur restent nécessaires.
- Un lot n'est `COMPLETE` que si ses oracles RED/GREEN, ses tests PostgreSQL /
  Typst réels et sa revue sont disponibles avec commandes et résultats.
- Un lot non approuvé reste sur sa branche ; aucun patch partiel masquant une
  preuve n'est intégré.
