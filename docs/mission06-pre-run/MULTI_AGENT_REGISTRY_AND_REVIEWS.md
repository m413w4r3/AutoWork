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
| M06-R-POLICY | `fc7927f4-28cb-4d99-ab50-1566a4bf153b` | codex/gpt-6-luna · xhigh | `wks_0a6cdd38441320d6` | `505c1cd2` | RUNNING | lecture seule | — | — | carte des gates |
| M06-R-S3 | `f263c604-dbc2-4c5c-8e74-73bca04b5dfb` | codex/gpt-6-luna · xhigh | `wks_c951e8eddf4fe80e` | `505c1cd2` | RUNNING | lecture seule | — | — | validation D6 |
| M06-R-RUNTIME | `eab41336-0371-4bf2-be21-45b190adb0cf` | codex/gpt-6-luna · xhigh | `wks_a861774a39607ad9` | `505c1cd2` | RUNNING | lecture seule | — | — | prévol P3 |
| M06-R-RUN | `af077b38-cb5f-48b9-8a9b-800bda3f0c3e` | codex/gpt-6-luna · xhigh | `wks_f8c47eda0b091a30` | `505c1cd2` | RUNNING | lecture seule | — | — | état P4 |
| M06-POLICY-1 | à créer | codex/gpt-6-luna · xhigh | à créer | après recon | PLANNED | RED/GREEN politique | à créer | — | héritage G2/G3 |
| M06-S3-CORE-1 | à créer | codex/gpt-6-luna · xhigh | à créer | après recon | PLANNED | PG jetable + concurrence | à créer | — | claim/permis |
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
