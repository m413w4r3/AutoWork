# Mission 01 V3.2 — audit final (provisoire)

**État :** en cours de clôture, 2026-10-24. Ce document ne déclare aucun `PASS`
non vérifié. Les verdicts des revues finales Luna et le résultat des cinq
régressions en cours y seront reportés après réception des comptes rendus.

**Base :** `main@058bb4af2278e3941956e523c5a1d560208de228`.
**Intégration locale :** `chore/mission01-integration` @ `3e15193`.
**Branche de correction en cours :** `fix/mission01-regressions`
(worktree `wks_cbcdaa104d853d74`).
**WIP parent :** `wip/mission01-parent-fixes` @ `849cfa0`.

## Périmètre réellement exécuté

- Aucun push, merge sur `main`, déploiement, accès live, appel fournisseur,
  acquisition, scan, upload ou donnée CTI privée.
- Tests exclusivement hors ligne, avec fixtures synthétiques/expurgées ;
  PostgreSQL et le transport Bridge réels restent `UNKNOWN`.
- Les artefacts Mission 00 ont été lus en lecture seule ; ils ne sont ni
  committés ni recopiés dans les livrables.

## Matrice des tests

| Vérification | Résultat | Portée |
|---|---|---|
| Suite croisée 9 lots | **220 passed** | `test_b1_admission_durability`, `test_b1_g2_bounded_timeout`, `test_advisor_completeness`, `test_bridge_investigator`, `test_investigation_advisor_tools`, `test_investigation_strategy_scenarios`, `test_investigation_experiments`, `test_investigation_experiment_api`, `test_investigation_audit_export` |
| Backend hors intégration (après correctifs régressions) | **4589 passed, 34 failed, 2 skipped** | `pytest --ignore=tests/integration -m "not integration"` sur `89eb3a5` ; à re-exécuter après fusion finale |
| Frontend | **293 passed**, `pnpm typecheck` OK | suite complète |
| B0A ciblé | **153 passed** | `test_advisor_completeness`, `test_bridge_investigator`, `test_investigation_advisor_tools`, `test_advisor_output_reparse` |
| B0B ciblé | **14 passed** | `test_investigation_audit_export` |
| B1A1 ciblé | **127 passed** | `test_b1_admission_durability`, `test_investigation_service`, `test_investigation_tools_contract`, `test_operator_tool_invocation` |
| B1A2 ciblé | **138 passed** puis **220 croisés** | suite G2 + suites croisées |
| A1 ciblé | **64 backend + 3 frontend + typecheck** | scénarios, expériences, API |
| L1 L06 | **2 + 41 passed** | `test_limits_mission01`, `test_invariant_proposals` |

### Triage des 34 échecs backend restants

- **32** échecs Typst réels : `FontBundleInvalidError: Manifest-listed file is
  missing: 'CascadiaCode/ttf/CascadiaCode.ttf'`. Environnement documenté par T ;
  ce n'est pas une régression de code. Le worker a reproduit ces 32 échecs sur
  `058bb4a`. Le pipeline réel reste `UNKNOWN`.
- **2** échecs préexistants prouvés sur `058bb4a` :
  `test_confined_analysis_dispatch.py::test_tool_registry_dispatch_runs_capa_and_strings_through_worker`
  (CAPA indisponible) et `::test_mwcp_allowlisted_fixture_parser_is_invoked_in_worker`
  (parseur MWCP de fixture absent).
- Les **5 régressions réelles** (hex court, alias C01 `T1`, retry d'acquisition,
  préflight advisor minimal, résultat `PARTIAL`) sont corrigées par `89eb3a5`
  et testées ; fusion avec l'intégration courante en cours.

## Livrables

| Livrable | État | Référence |
|---|---|---|
| `BASELINE.md` | PASS (runtime `UNKNOWN`) | `docs/mission01/BASELINE.md`, `e26c9a7` |
| `TASKS.md` | à jour, registre durable | `docs/mission01/TASKS.md` |
| `BRIDGE_CHAIN_EVAL.md` | livré ; transport Bridge réel `UNKNOWN` | `df1bd8f`, `docs/mission01/BRIDGE_CHAIN_EVAL.md` |
| Exports `RUN_TRACE.md`/`RUN_EVENTS.jsonl.gz`/`RUN_MANIFEST.json` | PASS_WITH_CHANGES ; PostgreSQL réel `UNKNOWN` | `41c8869`+`5add5df`, `docs/mission01/B0B_REVIEW.md` |
| `RUN_TRACE_SCHEMA.md` | intégré aux exports ; à vérifier dans la revue finale | `investigation_audit_export.py` |
| `LIMITS_DECISIONS.md` | 42 limites décidées ; L01 renderer `REMOVE`, P10 `DEFER` | `docs/mission01/LIMITS_DECISIONS.md` |
| `CTI_STRATEGY_EVAL.md` | scénarios A1 livrés et testés ; doc de synthèse à finaliser | `docs/mission01/CTI_STRATEGY_MAP.md`, `4a71f45` |
| `TYPST_REFERENCE_PARITY.md` | rapport PASS ; restauration V4 `BLOCKED` (validation éditoriale) ; pipeline réel `UNKNOWN` | `cae3115`, `docs/mission01/TYPST_REFERENCE_PARITY.md` |
| `FINAL_AUDIT.md` | provisoire, ce document | — |

## Revues indépendantes

| Revue | Verdict | Preuve |
|---|---|---|
| S0 baseline | PASS (runtime `UNKNOWN`) | `BASELINE.md` |
| B1 admission map | ADEQUATE_WITH_CHANGES | `94d6706` |
| L1 | PASS_WITH_CHANGES (L06), P10 DEFER après 3 FAIL | `bb7b8b4` |
| B0B | PASS_WITH_CHANGES | `230380d` |
| B0A | **FAIL/PARTIAL** : F1–F5 acceptés ; F6 arrêté après 3 tentatives (URL dans titre, percent sans `;base64`, base64 fragmenté) | `b149aa3`, code `cd69eca`/`498c2da` intégré `f67181e` |
| A1 | **PASS_WITH_CHANGES** : résiduel MEDIUM `capture_sha256`/`next_pivots` | `2e75d94`, code `1f0e813` intégré `545d32a` |
| B1A1 | **FAIL/PARTIAL** : protections principales confirmées, équivalence sémantique résiduelle, arrêt après 3 tentatives ; atomicité `BLOCKED` | `4317f46`, code `2f88a42` intégré `2cafb37` |
| B1A2 | **PASS_WITH_CHANGES** : plancher conservateur, `BridgeInvestigator.turn` hors borne, état ambigu en chaîne | `8597053`, code `01346e2`/`5484375` intégré `591eeca` |

Le quota Claude a été épuisé le 2026-10-23 ; les revues finales sont
exclusivement Luna xhigh depuis le reset du 2026-10-24. Aucune revue Claude
n'est comptée comme revue Luna.

## Gates et restes

- **B0** : chaîne dossier/manifeste/préflight/transport simulé livrée ; mode
  fichier >200 000 o simulé, aucun reçu Bridge inventé ; F1–F5 acceptés ;
  **F6 `FAIL/PARTIAL`** (URL dans un titre, percent sans `;base64`, base64
  fragmenté) après arrêt à trois tentatives ; F5 compte IOC exact `UNKNOWN`.
- **B1** : refus 266/3600 sans réserve active ni sortie fictive ; preuve
  `pre_submission` typée non forgeable ; atomicité révocation/dispatch
  **`BLOCKED`** (aucune primitive atomique) ; B1A2 `PASS_WITH_CHANGES` avec
  borne G2 incluant pacing ; B1A1 `FAIL/PARTIAL` sur l'équivalence sémantique
  (clé d'effet canonique requise) ; 3 600/7 200 borné pour `advisor.consult`.
- **L** : suppression du plafond renderer 96 k et du réglage L06 inerte ;
  protections G2/TLP conservées ; P10 `DEFER`.
- **A** : projection read-only, cinq scénarios offline, YARA synthetic-only
  sans `REAL_VALIDATED` ; `PASS_WITH_CHANGES` avec résiduel MEDIUM de
  provenance `capture_sha256`/`next_pivots`.
- **T** : rapport et captures livrés ; restauration `category`/numéro/`overview`
  `BLOCKED` derrière validation éditoriale ; pipeline réel `UNKNOWN`.
- **Non démarrés** : B1B (temporalité/reprise), L2 (pagination keyset), Q
  (éval anti-slop), `FINAL_AUDIT` définitif. Ils restent `PENDING` et ne
  doivent pas être présentés comme réalisés.

## Sécurité et provenance

- G2/G3/TLP, `external_llm_allowed`, `do_not_submit`, restrictions fournisseur,
  CAS, idempotence et append-only n'ont pas été affaiblis par les lots validés.
- Aucune migration Alembic, aucun changement de `0001_baseline`/`0002`.
- Aucun secret, URL signée, corps d'échantillon ou payload privé dans les
  exports standard ; le `.gz` n'est pas une mesure de confidentialité.
- Les preuves CTI canoniques restent PostgreSQL/MinIO ; Paseo et les transcripts
  d'agents ne sont pas des preuves CTI.

## Coûts et approbations

- Coûts fournisseur, tokens par session et coûts Paseo : **`UNKNOWN`**. Aucun
  reçu fournisseur ni facture indépendante n'a été consulté.
- Approbations humaines encore requises : toute mesure live Bridge/VT/Shodan,
  toute réconciliation `needs_review`, toute migration, toute publication/G3,
  toute modification de budget G2 et tout push/merge sur `main`.
