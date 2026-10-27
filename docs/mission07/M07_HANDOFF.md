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
| A3 E2E continu + D5 | `m07/e2e-continuous` … `ff991fb1` | enfants test `6b0f88fb`/`cbf086e3`/`95090678`/`b54e1495` ; parent fix `14888eac`,`78ff4c1a`,`fe414c74`,`ff991fb1` ; revues `cdf13334` REVISE → `df1777d5` REVISE → `d61d69f6` REVISE → `59e0a621` APPROVED | **intégré** `main@a506f481` (+`502dfae7` typage test) |

## A3 — clos (successeur #2, 2026-10-27)

Origine exacte du refus advisor (instrumentation PG) : `models.py` `_coordinated_create` ← coordinator `Refused(osint_policy_denied, PRE_CONSUME)` ← `SqlAlchemyVirusTotalPolicyGuard.authorize` → `_source_external_disclosure_allowed` vétait l'échantillon acquis (`sample_acquisition.py:689` : `do_not_submit=True`, `external_llm_allowed=True`). **D5 = défaut de production** présent aussi sur main/A2 : sous OSINT_POLICY, aucune consultation Bridge ne pouvait citer un échantillon téléchargé.
Correctif (guard uniquement, familles `bridge.`/`advisor.`) : ignore `do_not_submit` source ; `external_llm_allowed=False`, TLP RED, licence restent bloquants ; observations `sha256` et références `sample_association(_sample)` résolues comme le claim store. Non-Bridge inchangé.
Tests : E2E A3 vert (replay : 0 nouvel appel VT/Bridge, snapshots persistés/audit identiques) ; attentes E2E corrigées (tuples figés, `_vt_external_effect_source_refs`, clé composite de liens) ; PG `test_osint_guard_bridge_ignores_sample_do_not_submit_but_keeps_external_llm_veto` (6 cas, mutation vérifiée).

Gates `main@a506f481` (parent, recette D4) : lint OK ; test-backend 5143 passed ; test-integration 456 passed ; test-workflow-full 32/32 ; mypy 843 OK et J2 navigateur 1 passed sur `502dfae7`. CI non relancée (non poussé).

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

- D6 (hors périmètre, décision utilisateur) : le claim store (`repositories/external_effects.py` ~1622-1670, commit `15b36abe`) ne contrôle pas `external_llm_allowed` des samples et l'ignore pour les documents Bridge, y compris sous HUMAN_G2. Sous OSINT_POLICY le guard le contrôle désormais. Non bloquant pour le run.
- R1 (risque run) : un dossier Bridge citant une observation `sha256` de hash **non associé** à l'investigation (pivot VT non téléchargé) est refusé au consume (`evidence_observation_not_associated_with_investigation`, préexistant). Citer l'observation VT du rapport/pivot (type fichier) ou télécharger d'abord.
- R2 : 3 tests `test_confined_analysis_dispatch` (capa/MWCP) échouent localement hors `make` (identiques sur main) ; verts via `make test-backend`.

## Prochaines actions

1. Phase A close. Phase B : autorisation opérateur présentée le 2026-10-27 (push ; migration live `0002→0005` via `make up` ; jeton opérateur + OSINT_OPEN `tool_seconds` ≥ 86 400 ; smokes VT/Bridge ciblés ; nouveau production_run ; investigation). **Non accordée à ce jour** : ne rien exécuter sur le live sans réponse explicite.
