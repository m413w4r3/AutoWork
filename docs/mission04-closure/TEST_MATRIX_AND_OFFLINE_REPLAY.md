# Mission 04 closure — matrice de tests et replay hors ligne

**Base :** `d7cc6b590d3be41dae10299cc03364771fe6bda1`
**Branche de clôture :** `mission04/closure-after-main`
**Environnement :** Python 3.12.3, PostgreSQL jetable `localhost:55432`
(service `cti-bulletin-postgres-test-1`, tmpfs), Typst 0.15.1 et D2 0.9.0
dans `/tmp/aw-tools`, bundle de polices verrouillé, `TMPDIR` sous `$HOME`.
Aucun accès fournisseur, Bridge, VT, Shodan, Retrohunt, acquisition ou pile
live ; seuls les transports/fakes et Typst local sont utilisés.

Un test non exécuté est marqué **NON EXÉCUTÉ**, jamais PASS. Les commandes et
compteurs ci-dessous ont été exécutés sur le HEAD intégrateur après merges
SAMPLE D7 et CI-0c.

## 1. Lots et oracles

| Lot | Commandes / oracles | Résultat |
|---|---|---|
| L1-R5 + WIRE (P10) | `pytest tests/test_invariant_proposals.py` ; revue Luna xhigh R3 | **102 passed** ; R3 `APPROVED` (`96ecb538`) ; fuite suffixe fermée, LF/CRLF bornés, `P10_PROMPT_VERSION=5` |
| L4 — D1–D4 | `pytest tests/test_operator_tool_invocation.py` + PG contexte ; revue Luna xhigh | **105 unit + 3 PG** ; offset legacy 422, curseurs opaques liés au contenu, stale 409, `context_sha256` sémantique ; **APPROVED** |
| L5 — D5–D6 | `pytest` ciblé + `test_analyst_opinion_publication_postgres.py` + Pdf réel | **79 + oracle PDF réel** ; lineage V5/V6 acteurs, textes/URL/dates/timeline, IDs reviewer/approver absents ; **APPROVED** |
| L21 — D8–D9 | `pytest tests/test_advisor_* tests/test_advisor_output_reparse.py` | Parse/reparse v4 partagé, legacy refusé, reparse sans nouvelle émission ; **APPROVED** |
| CI-0b — FAST | `make test-workflow` | **22 passed**, JUnit/traces vérifiés ; **APPROVED** |
| CI-0c — FULL | `pytest service/B1/operator/W16` ; `make test-workflow-full` | **183 passed** puis **32 passed** ; reprise RUNNING, verifier de preuve canonique, diagnostics opérateur ; **APPROVED** |
| SAMPLE — D7 | `pytest tests/test_investigation_advisor_tools.py tests/test_advisor_* tests/test_evidence_recorder.py` | **262 passed, 1 skipped** (archive Run12 absente) ; octets/hex/Base64/métadonnées/associations et vetos zéro appel ; URL signées IPv6/zone/chemins crochetés ; **APPROVED** |
| WIRE — inventaire D10 | revue Luna xhigh `44f12811` | **APPROVED** après 3 révisions ; aucun cutover |

## 2. Gates d'intégration

| Oracle | Commande | Résultat |
|---|---|---|
| Suite backend hors intégration | `make test-backend` | **4904 passed, 2 skipped, 31 deselected** |
| Tests PostgreSQL d'intégration | `make test-integration` (DSN jetable) | **343 passed** |
| Workflow FAST | `make test-workflow` | **22 passed** ; garde JUnit OK |
| Workflow FULL | `make test-workflow-full` | **32 passed** (fast+full) ; garde JUnit OK |
| Navigateur W10 | `make test-workflow-browser` (PG réel) | **1 passed** ; vérification SQL OK |
| Navigateur J2 | `make test-workflow-browser-investigation` (PG + Typst + Poppler) | **1 passed** ; PDF réel, lineage, repin |
| Frontend lint/typecheck/unitaires/build | `pnpm lint && pnpm typecheck && pnpm test --run && pnpm build` | **39 fichiers / 293 tests**, build Vite vert |
| Frontend e2e (mock) | `CHOKIDAR_USEPOLLING=true pnpm test:e2e` | **13 passed** |
| mypy repo-wide | `make typecheck-backend` | **Success: 820 source files** |
| Ruff + format | `make lint-backend` | **847 fichiers**, tous propres |
| Scripts de garde CI | `make test-ci-scripts` | **5 tests**, OK |

## 3. Replay transverse hors ligne

Commande exécutée (PG jetable, fakes, Typst/D2 local, aucun réseau) :

```bash
cd backend && uv run --python 3.12 pytest \
  tests/test_invariant_proposals.py \
  tests/test_signature_bridge.py \
  tests/test_investigation_advisor_tools.py \
  tests/test_advisor_output_reparse.py \
  tests/test_advisor_semantic_parser.py \
  tests/test_advisor_wire.py \
  tests/test_operator_tool_invocation.py \
  tests/integration/test_operator_context_summary_postgres.py \
  tests/integration/test_investigation_audit_export_postgres.py \
  tests/integration/test_analyst_opinion_publication_postgres.py \
  tests/integration/test_publication_render_postgres.py \
  -q --tb=short
```

**Résultat : `338 passed`.** Couverture effective :

- P10 refused/partial/succeeded → P09, provenance/version/SHA et replay sans
  second envoi (`test_invariant_proposals`, `test_signature_bridge`) ;
- advisor prepare avec octets/contenu CTI, hex/Base64, métadonnées, hashes et
  associations, transport fake intact ;
- parse/reparse v4, aliases, `source_ref` opinion versionné, refus
  invalid/partial/ambiguous, reparse depuis les seuls artefacts persistés ;
- contexte full/paged, même ordre et même `context_sha256`, stale 409, offset
  legacy 422, PostgreSQL réel ;
- audit export et projections scoped sans promotion d'une sortie modèle ;
- PDF V5 base et V6 augmenté réellement compilés, oracle d'occurrences,
  absence des identités reviewer/approver dans le texte visible.

**Statut : `OFFLINE_REPLAY_PASS`.** Aucune étape non atteinte. Aucun test live,
aucun skip masquant une étape.

## 4. Limites résiduelles

- Les anciennes observations de refus sans objet de preuve canonique, ou dont
  la preuve dépasse 64 KiB, restent bloquées en `NEEDS_REVIEW` jusqu'à
  réconciliation humaine (comportement conservateur CI-0c).
- Les issues externes ambiguës conservent une réservation et un diagnostic
  framework, sans archiver la sortie de l'adaptateur.
- S3 reste `S3_BLOCKED_DESIGN` ; aucun cutover advisor/Bridge réel.
- Les smokes live provider/Bridge ne sont pas exécutés par cette clôture.
