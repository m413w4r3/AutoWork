# Mission 07 — handoff / registre (orchestrateur)

Parent : Claude Code `claude-opus-5-5` (non Luna). Enfants Paseo
`codex/gpt-6-luna`, `auto-review`, `xhigh`, une session neuve par cycle.
Les enfants n'atteignent pas PostgreSQL : le parent exécute tous les tests PG
(base jetable `make integration-db-up`, port 55432, venv
`uv sync --locked --python /usr/bin/python3.12`).

## Baseline vérifiée (2026-10-27)

- `main` = `origin/main` = `87093056`, CI #40491882109 success.
- Stack live `cti-bulletin` : images construites 2026-10-22, label
  `ea456083…` (absent du dépôt git) ; Alembic live **`0002_c2_experiments`**
  (code requiert `0005`). `make up` lance le service `migrate`
  (`alembic upgrade head`) : c'est donc une mutation live.
- Live (lecture seule) : inv `5f66ebfb` `running` (màj 2026-10-20),
  aucun job non terminal (jobs : 899 succeeded/10 failed/2 cancelled),
  19 invocations `needs_review`, 225/225 samples `do_not_submit=true`,
  12 production_runs `ready` sur le sujet `d22822d4` (dernier `ec7a6807`).
- `make smoke-live` inclut Shodan passif : **ne pas le lancer tel quel**.

## Lots phase A

| Lot | Branche @SHA | Agent(s) | Statut |
|---|---|---|---|
| A2 VT READ `do_not_submit` | `m07/vt-read-dns@ff7c4d1c` | fix `e2cc6a54`, r2 `969cdbca` ; parent `fc3e3d14`, `ff7c4d1c` ; revues `4062511c` REVISE → `ceb9021b` APPROVED | **intégré** `main@b4d5db8d` ; PG 89 passed, unit 365 |
| A3 E2E continu | `m07/e2e-continuous` `6fc02e0f`→`da2d9436` | `6b0f88fb`, r2 `cbf086e3` (rejeté : modif composition BRIDGE_LED), r3 `95090678` → `50041b17` (OPERATOR_LED, test seul), r4 `b54e1495` r4 `b54e1495` → `0ef6a909` (builder runtime de prod), parent `73d870dd` (settings du harnais) | voir §A3 ci-dessous |

## A3 — état précis à `m07/e2e-continuous@73d870dd` (non intégré)

Nœud `tests/integration/test_investigation_vertical_postgres.py::test_real_postgres_osint_open_continuous_vt_analysis_advisor`
(OPERATOR_LED, OSINT_OPEN épinglé, builder de prod
`build_investigation_runtime_composition_factory` + harnais confiné W15).
PG parent : 13 passed / 1 failed. Verts : pin OSINT_POLICY sans décision,
`vt.file_report`, `vt.intelligence_search`, `vt.download_sample` (404 →
download_url → CDN 200), quarantaine/SHA/association candidate,
`sample.analyze` avant G3. **Rouge : `advisor.consult`** → invocation
`needs_review` (`invocation_needs_review`), sortie advisor
`{advisor_status: unavailable, failure_stage: preflight, failure_reason:
external_effect_authority_required, confirmed_non_execution: True,
bridge_turns_consumed: 0}` ; aucune requête HTTP Bridge.

Instrumentation parent (temporaire, retirée) :
- `investigation_service.py:~1573` gate d'autorité : pour advisor.consult,
  envelope/reservation_persisted/persisted_reservation/_budget_snapshots tous
  True ⇒ `ExternalEffectAuthority` construite.
- `investigation_runtime_ports.py:238` `_build_bridge_external_effect_binding`
  appelé avec authority `ExternalEffectAuthority`, family
  `advisor.consultation.v1`, evidence_refs tuple d'`EvidenceReference` ⇒
  binding non nul ; `bridge_investigator.py:~389` reçoit binding non nul
  (la branche 397 `authority_context_missing` n'est PAS prise).
- `integrations/models.py` : les branches 126 (coordinator sans binding),
  134 (binding sans coordinator) et 160 (`request` POST /responses) ne sont
  **pas** atteintes (pytest `-s`). Branche 253 non instrumentée.
- Donc le code `external_effect_authority_required` (failure_stage
  preflight, comme `_bridge_external_effect_refusal`) provient d'un autre
  chemin : à tracer (`models.py:253` `_coordinated_create`, mapping
  d'erreur `BridgeTransportError`→sortie advisor, ou un 2ᵉ appel advisor
  / chemin `ModelConversationTurnTransport.submit` où le kwarg binding est
  perdu). Hypothèse : **défaut de production possible** (le r4 concluait
  « test seulement » sans PG ; contredit par le run avec builder de prod).
  Bloquant pour LIVE_BRIDGE / BRIDGE_GUIDANCE en mode opérateur.
- Sous-question ouverte (r4) : un refus pré-soumission
  `confirmed_non_execution` est projeté en `needs_review` (invariant :
  seule une preuve framework typée confirme la non-exécution) ⇒ risque
  d'items `needs_review` parasites pendant le run.

Méthode efficace : le parent instrumente avec `print(..., file=sys.stderr)`
+ `pytest -s`, puis `git checkout -- src/` (ne jamais committer).

## Phase B — préparation (lecture seule sur le live)

- Sauvegarde live (pg_dump -Fc, lecture seule) :
  `/home/nill/perso/work/AutoWork-backups/live-pre-m07-20261027T154631Z.dump`
  (5,0 Mo, SHA-256 `413d5cdc94b1528561a00aa3b0935401a4bdad5c9544fd8bc297ec2a5ad72495`,
  98 TABLE DATA).
- Répétition sur copie restaurée (`m07_rehearsal`, postgres-test 55432) :
  `0002→0005` OK, puis `0005→0002→0005` OK ; 225 samples, 1510 invocations
  conservés.

## Défauts consignés

- D1 (A2, corrigé `fc3e3d14`) : le prédicat partagé réappliquait
  `do_not_submit` source au consume Bridge, contraire à la décision
  utilisateur du 2026-10-27.

- D2 (A2, corrigé `ff7c4d1c`, revue `4062511c`) : admission Bridge
  réappliquait `do_not_submit` source (incohérent avec le consume).
- D3 (opérationnel, config) : `advisor.consult` réserve jusqu'à
  `MAX_ADVISOR_TOOL_SECONDS=3600` (`investigation_advisor_tools.py:101`) ;
  défaut OSINT_OPEN `tool_seconds=3600` ⇒ aucune consultation Bridge après
  une analyse. **Activer la politique live avec `tool_seconds` ≥ 86 400**
  (max `osint_open_max_tool_seconds`).
- D4 (env, non code) : 5 tests Typst réels échouent sans la recette du
  binaire épinglé (`TYPST_BINARY`, `FONT_BUNDLE_ROOT`, `TMPDIR`) ; verts avec.

## Prochaines actions

Gates `main@b4d5db8d` (parent) : lint OK, mypy 843 OK, test-backend
5102 passed + 5 Typst (verts avec recette D4, 16/16), test-integration
449 passed. Workflow FAST/FULL et J2 non relancés (A2 ne touche pas
l'UI ; à relancer avant push).

1. A3 : tracer l'origine exacte du refus advisor (ci-dessus), correctif
   minimal + test, PG vert, revue Luna xhigh, merge `--no-ff`.
2. Gates finaux sur `main` (+ workflow FAST/FULL, J2), puis push.
3. Demande d'autorisation opérateur bornée (push, sauvegarde, migration
   `0002→0005` + redéploiement, jeton opérateur pour OSINT_OPEN / API).
