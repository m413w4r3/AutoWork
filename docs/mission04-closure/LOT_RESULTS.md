# Mission 04 closure — registre compact des lots

**Base :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Branche de clôture :** `mission04/closure-after-main`
**Statut global :** en cours — aucun lot n'est déclaré COMPLETE avant son
GREEN réel et sa revue indépendante.

| Lot | Owner | Branche / SHA | État | RED/GREEN réellement exécuté | Reviewer | Risque / dépendance |
|---|---|---|---|---|---|---|
| CI-0 petits correctifs | parent | `mission04/closure-after-main` / `ccff3444` | **CORRIGÉ, à vérifier sur HEAD intégrateur** | `pnpm lint` PASS ; import navigateur PASS ; compilation Typst + `pdftotext` PASS | gate parent | dépend du HEAD final |
| CI-0a mypy mécanique | Luna high `1b63a297` | `mission04/ci0-mechanical` / `664674a4` (intégré `a3b2860f`) | **VÉRIFIÉ PARENT** | mypy changé : 8 fichiers `Success` ; repo-wide **35 erreurs restantes, toutes L4** ; 104 tests ciblés passed ; Ruff/format/diff-check verts | parent | intégration test PG non exécutée par le worker, à rejouer sur HEAD |
| CI-0b workflow FAST | Luna xhigh `4a40947f` | `mission04/ci0-workflow` | **EN COURS** | W13-D5 et scénario quota verts au premier rerun ; 2 nœuds restants | parent | sémantique des statuts |
| CI-0c workflow FULL | Luna xhigh `dcbc7694` | `mission04/ci0-protocol` | **EN COURS** | W15 reproduit : 2 vérifications intentionnelles (avant/après révision) | parent | W18/FULL à diagnostiquer |
| L1-R5 + WIRE(P10) | parent | `mission04/closure-after-main` / `f92d848d` → `5a23a291` → `2a5987e4` | **IMPLÉMENTÉ, R5 R1/R2 REVISE corrigés, R5 R3 en attente** | 102 P10 passed ; R1 leak `}` corrigé par refus ; R2 sur-refus des valeurs fermées ponctuées corrigé par premier guillemet structurel ; sonde valide `}`/`]`/`,` acceptée, cas non bornés refusés ; linéarité OK | Luna xhigh R1 `REVISE`, R2 `REVISE`, R3 en cours | secret/CTI, P10 v5 |
| L4 — D1–D4 | Luna xhigh `d92f6674` | `mission04/l4-context` | **EN COURS** | oracles RED en cours sur PostgreSQL jetable | Luna xhigh à lancer | full/paged, curseur, SHA |
| L5 — D5–D6 | Luna xhigh `ec7b863b` | `mission04/l5-pdf` / `c71e5c4a` | **IMPLÉMENTÉ, revue en cours** | RED `KeyError: actors` puis **56 passed** (PDF réels base+avis, PG V5/V6, API lineage) ; Ruff/mypy/diff-check verts ; vérifié parent 56 passed | Luna xhigh `cd40c7cf` en cours | oracle PDF couvre titre/lead/référence/avis mais pas encore heading/qualification/citations/timeline ; gap D6 documenté |
| L21 — D8–D9 | Luna xhigh `8d483738` | `mission04/l21-advisor` / `981632a7` | **REVISE, réparation en cours** | 202 advisor passed, 1 deselected (corpus Run12 absent) ; Ruff/mypy/diff verts | Luna xhigh `fa293266` : **REVISE** (1 HIGH credentials .env, 2 MEDIUM wire/status, 2 LOW coupling) | aucune émission, S3 bloqué |
| SAMPLE — D7 | parent | à créer après L4 | **NON COMMENCÉ** | matrice positive/négative à exécuter | Luna xhigh obligatoire | conflit de fichier avec L4 |
| WIRE — inventaire D10 | scout Luna xhigh `e9bf3fb3` | lecture seule | **INVENTAIRE FAIT** | `BRIDGE_WIRE_INVENTORY.md` | revue de lot à faire | un seul chemin JSON dormant |
| Offline replay transverse | à déléguer | à créer après intégration | **NON EXÉCUTÉ** | — | parent | dépend de tous les lots |
| S3 | — | — | **`S3_BLOCKED_DESIGN`** | aucun code/migration/route | — | hors périmètre |

## Règles de statut

- `IMPLÉMENTÉ` ne signifie pas approuvé : la revue indépendante et les gates
  croisés sur le HEAD intégrateur restent nécessaires.
- Un lot n'est `COMPLETE` que si ses oracles RED/GREEN, ses tests PostgreSQL /
  Typst réels et sa revue sont disponibles avec commandes et résultats.
- Un lot non approuvé reste sur sa branche ; aucun patch partiel masquant une
  preuve n'est intégré.
