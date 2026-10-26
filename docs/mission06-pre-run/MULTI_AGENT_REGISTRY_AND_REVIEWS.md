# Mission 06-P — registre multi-agent et revues

Statut du document : **EN COURS** (registre vivant ; ne vaut pas clôture).
Baseline de travail : `505c1cd2c525744bee5d768cccc1a830abce2d2b` (`main`, worktree
propre au démarrage).

## Conformité du modèle d'orchestration

| Rôle | Modèle réellement utilisé | Effort | Constat |
|---|---|---|---|
| Parent / orchestrateur | `deepseek-claude` / `deepseek-flash[1m]` | `max` | **NON CONFORME** à l'exigence « tous les agents Luna xhigh ». Le parent n'est pas un modèle Luna et ne peut pas se substituer lui-même. |
| Enfants de reconnaissance | `codex` / `gpt-6-luna` | `xhigh` | Conforme à la création ; vérification runtime à consigner par agent. |
| Implémenteurs prévus | `codex` / `gpt-6-luna` | `xhigh` | À créer. |
| Reviewers prévus | `codex` / `gpt-6-luna` | `xhigh` | À créer, distincts des implémenteurs. |

Conséquence : la ligne finale `LUNA_XHIGH_MULTI_AGENT_VERIFIED` ne pourra pas
être déclarée PASS pour la couche parente tant que la session principale n'est
pas elle-même une session Luna xhigh. Les sessions enfants, elles, sont
créées avec le couple provider/modèle/effort requis.

## Registre des lots

Légende : `PLANNED → RUNNING → REVIEW → APPROVED → INTEGRATED`, alternatives
`REVISE` / `BLOCKED`.

| Lot | Agent | Modèle/effort | Workspace | SHA base | Statut | Tests | Reviewer | Décision | Risques |
|---|---|---|---|---|---|---|---|---|---|
| M06-R-POLICY | `fc7927f4-28cb-4d99-ab50-1566a4bf153b` | codex/gpt-6-luna · xhigh | `wks_0a6cdd38441320d6` | `505c1cd2` | DONE | lecture seule | — | rapport reçu | carte des gates |
| M06-R-S3 | `f263c604-dbc2-4c5c-8e74-73bca04b5dfb` | codex/gpt-6-luna · xhigh | `wks_c951e8eddf4fe80e` | `505c1cd2` | DONE | lecture seule | — | rapport reçu ; D6 toujours valide | validation D6 |
| M06-R-RUNTIME | `eab41336-0371-4bf2-be21-45b190adb0cf` | codex/gpt-6-luna · xhigh | `wks_a861774a39607ad9` | `505c1cd2` | DONE | lecture seule | — | rapport reçu ; 0 READY live | prévol P3 |
| M06-R-RUN | `af077b38-cb5f-48b9-8a9b-800bda3f0c3e` | codex/gpt-6-luna · xhigh | `wks_f8c47eda0b091a30` | `505c1cd2` | DONE | lecture seule | — | rapport reçu ; lignée synthèse UNKNOWN | état P4 |
| M06-POLICY-1 | `268c4abe-7f93-4d12-bcff-0d8b2175064b` | codex/gpt-6-luna · xhigh | `wks_a15c58fa94fe06bf` | `7059ebbd` | **INTEGRATED** | 51 unit + 6 PG + 120 migrations PASS sur `main@4a97fa58` | `a656cd81-4250-4b5f-8033-01063f1d8aee` | **APPROVED** lot politique ; release externe conditionnée S3 | héritage épinglé, anti-faux HUMAN, bypass Bridge fermé ; B-4/cap/lignée S3 |
| M06-S3-SPEC | `adb555b5-158a-450e-acff-c25293f85ea4` | codex/gpt-6-luna · xhigh | `wks_69c42fa0c7b7eab6` | `7059ebbd` | DONE | lecture seule | — | rapport reçu ; migration 0004 après 0003 | spécification S3 |
| M06-REVIEW-POLICY-DESIGN | `66e365ad-83b0-43ad-8e7d-8b1d42a8e616` | codex/gpt-6-luna · xhigh | `wks_a221abc2a4219236` | `7059ebbd` | DONE | lecture seule | — | **REVISE** : 4 BLOCKER, 5 MAJOR ; corrections transmises | revue adversariale |
| M06-S3-CORE-1 | `b052d44a-d753-44c5-8c5b-37e50bcb69a7` | codex/gpt-6-luna · xhigh | `wks_7dc76e2c336df65d` | `d53538b0` | **REVISE — NON INTÉGRÉ** | 46 unit PASS ; PG combiné 3/5 avant `2c6c6074` ; lint+typecheck PASS | `a5773f45-7854-4165-9a1b-054ba12da200` | REVISE | 3 MAJOR : gate sans I/O, fragments token, preuve forgeable ; familles non migrées |
| M06-REVIEW-S3-CORE | `a5773f45-7854-4165-9a1b-054ba12da200` | codex/gpt-6-luna · xhigh | `wks_bb93ccd029795f6a` | `0637bdea`/`de8b7451` | DONE | lecture seule + 46 unit PASS ; PG 3/5 | — | **REVISE final** : 0 BLOCKER, 3 MAJOR | revue adversariale |
| M06-REVIEW-POLICY-IMPL | `a656cd81-4250-4b5f-8033-01063f1d8aee` | codex/gpt-6-luna · xhigh | `wks_7769192bb22b9bd6` | `33104968` | DONE | lecture seule + 51 unit PASS + 6 PG PASS | — | **APPROVED** lot politique ; S3-CORE doit fermer la course/cap/lignée avant release externe | revue adversariale |
| M06-S3-VT-1 | à créer | codex/gpt-6-luna · xhigh | à créer | après core | PLANNED | fakes + PG | à créer | — | lecture VT |
| M06-S3-DL-1 | à créer | codex/gpt-6-luna · xhigh | à créer | après core | PLANNED | fakes + PG | à créer | — | téléchargement |
| M06-S3-BRIDGE-1 | à créer | codex/gpt-6-luna · xhigh | à créer | après core | PLANNED | fakes + PG | à créer | — | tours Bridge |
| M06-E2E-ORACLES | à créer | codex/gpt-6-luna · xhigh | à créer | après lots | PLANNED | PG + concurrence | à créer | — | faux PASS |

## Règles d'indépendance

- Un implémenteur ne révise jamais son propre lot.
- Un reviewer travaille sur un SHA figé, dans un worktree distinct, sans
  mutation ni appel fournisseur.
- Le parent seul intègre les commits approuvés sur `main` local.
- Aucun push, force-push, merge distant, déploiement ou migration live.

## Écarts de conformité déjà observés

1. Orchestrateur parent non-Luna (voir tableau ci-dessus).
2. Les skills demandés `codebase-design`, `improve-codebase-architecture` et
   `implement-spec` ne figurent pas dans le catalogue de skills exposé à cette
   session. Les skills `code-review`, `diagnosing-bugs`,
   `autowork-ralph-gate`, `autowork-context-handoff`, `autowork-run-audit` et
   `autowork-cti-evidence-gate` sont disponibles.
3. Aucun appel fournisseur, téléchargement, accès live PostgreSQL/MinIO ou
   migration live n'est autorisé par cette mission.

## Preuves de reconnaissance conservées

| Rapport | Agent | SHA-256 |
|---|---|---|
| `recon/POLICY_SEAM.md` | M06-R-POLICY | `45e6c18d1563d22a09dd468fca3055111e719cf43ab8a5a6b89e55c93cb9cc44` |
| `recon/S3_SEAM.md` | M06-R-S3 | `9f17611abf3a05166f069b745cfd546041e6748ccce1f4f36b6135f3774dbaa4` |
| `recon/RUNTIME_READINESS.md` | M06-R-RUNTIME | `8d45480896e27945087c99786cd995dce6670831f5ee05edae6833fee4220f47` |
| `recon/RUN_STATE.md` | M06-R-RUN | `7a71689e65cf6198c16550d05a59ca03dfb97a877d00b268dbd4cb0ee28b01ed` |
| `recon/POLICY_DESIGN_REVIEW.md` | M06-REVIEW-POLICY-DESIGN | `83773c57faedf56009edb2905c973214d38779b31c725a2d80473f3afb761338` |
| `recon/S3_IMPLEMENTATION_SPEC.md` | M06-S3-SPEC | `ee82367b419a85ba1c6b90dd79a014907dc856647e4e016fffd22412fbe3a397` |
| `recon/S3_CORE_REVIEW.md` | M06-REVIEW-S3-CORE | `2c511d4f1dbc2d86d24424a3bbbc04a1cc8dc5b00b2d94485588a9badcffd9d2` |
| `recon/POLICY_IMPL_REVIEW.md` | M06-REVIEW-POLICY-IMPL | `008153a396d5a4db1b3642ba4905bc2095417af569e5e3de6618a1eed1a3bc39` |

## Gate combiné temporaire (hors `main`)

- Worktree `/tmp/m06-combined-gate`, branche `m06/combined-gate`, base
  `4923bbf8`, cherry-picks politique `7ef241f2` + S3 `642fae96`.
- Chaîne `0001 → 0002 → 0003 → 0004` appliquée réellement sur une base
  jetable après deux corrections d'intégration : exclusion des tables
  `external_effect_*` du métadata baseline gelé, et import du modèle
  `retrohunt` dans `migrations/env.py`. Résultat : `UPGRADE_OK`.
- Tests PostgreSQL politique : **6/6 PASS** à `7ef241f2`.
- Tests PostgreSQL S3 : après correction du fixture et de la barrière,
  **3 passed, 2 failed** — replay d'un claim complété renvoyant
  `consume_unavailable` au lieu de `effect_occurrence_required`, et fixture
  de crash seedant un budget sans clé `limits`. Correction finale demandée.
- Politique : **intégrée sur `main@efc38e8a`**, 51 unit + 6 PG verts,
  revue **APPROVED** ; release externe conditionnée S3.
- Aucun de ces résultats ne constitue une preuve de transport fournisseur.
  Le socle S3 n'est **pas** fusionné sur `main` tant que sa revue n'est pas
  `APPROVED` et que les 5 tests PG ne sont pas verts.
