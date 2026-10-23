# Mission 01 — baseline S0

Capture locale : 2026-10-23, date du [registre](TASKS.md#L3). Les artefacts
Mission 00 cités ci-dessous sont les seules sources externes utilisées ;
leurs corps ne sont pas reproduits.

## Git et runtime

- `git rev-parse HEAD` : `058bb4af2278e3941956e523c5a1d560208de228`, égal à
  la base mission du [registre](TASKS.md#L3). Branche :
  `audit/mission01-s0-baseline`. Le relevé initial `git status --short --branch
  --untracked-files=all` montrait uniquement `docs/mission01/TASKS.md` non suivi.
- **HEAD testé : UNKNOWN** — aucun test ni replay n’a été exécuté sur le HEAD
  local pendant S0.
- Mission 00 déclarait le checkout runtime `31ceeac37c24c504873492faadd81fe34b064af1`
  et la révision `ea4560831e503c6ce34d18739476a71ac6e9a9de` pour backend,
  worker et recovery (Mission 00, 2026-10-23,
  `RUN12_LIVE_AUDIT.md`:3,20-23,30).
- **Image live inspectée : UNKNOWN** — `docker ps` et `docker image inspect
  ea456083` ont échoué avec `permission denied` sur le socket Docker.
  Aucune image ni aucun conteneur courant n’a donc été inspecté.
- **Code chargé : UNKNOWN** — le processus runtime n’est pas inspectable ici.
  L’audit Mission 00 précise que des hashes source concordants ne prouvent
  pas l’équivalence de l’image entière ni le code chargé
  (`RUN12_LIVE_AUDIT.md`:41).

## Compatibilité corpus et migration

| Élément | Déclaré par Mission 00 (2026-10-23) | Vérifiable à ce HEAD |
|---|---|---|
| C01–C15 | Les hashes d’entrée/sortie sont corrélés dans l’archive aux 15 tours persistés ; l’audit déclare 30/30 hashes de fichiers concordants. Réf. `RUN12_LIVE_AUDIT.md`:7,57-71,85. | `UNKNOWN` — corpus, base et blobs live non vérifiés localement. |
| C14/C15 | Datent du 18 octobre avant la création de l’investigation cible ; leur rattachement à celle-ci est `UNKNOWN`. Réf. `RUN12_LIVE_AUDIT.md`:74. | `UNKNOWN` — aucune vérification locale du rattachement. |
| C10/C12 | Référencés comme sorties de `advisor.reparse`; le couple archivé ne prouve pas une nouvelle soumission. Réf. `RUN12_LIVE_AUDIT.md`:75. | `UNKNOWN` — aucun reparse/replay exécuté à S0. |
| Expériences C2 | Archive : 21 ; base live rapportée : 24. Réf. `DOCUMENTATION_CONFLICTS.md`:22 et `RUN12_LIVE_AUDIT.md`:42. | Le code local contient le marqueur Alembic `0002_c2_experiments` ([migration](../../backend/migrations/versions/0002_c2_experiments.py#L18-L19)); le compte DB et la parité complète du schéma restent `UNKNOWN`. |

Aucune archive n’a été restaurée sur une base.

## Skills et profils

- Inventaire filesystem local : 13 répertoires sous `.agents/skills/`, 13
  symlinks `.claude/skills/` résolus vers ces répertoires, et 5 TOML sous
  `.codex/agents/` : [critical-reviewer](../../.codex/agents/critical-reviewer.toml#L1),
  [implementer](../../.codex/agents/implementer.toml#L1),
  [mechanical-worker](../../.codex/agents/mechanical-worker.toml#L1),
  [scout](../../.codex/agents/scout.toml#L1), [tester](../../.codex/agents/tester.toml#L1).
  Les 6 skills AutoWork et 7 skills d’ingénierie sont référencés dans
  [l’index docs](../README.md#L115) et [l’index local](../../.agents/skills/README.md#L11).
- [.codex/config.toml](../../.codex/config.toml#L1) déclare les helpers Codex
  activés, concurrence 6, `gpt-6-luna` et effort `high`. Le guide précise que
  cette config n’atteste pas son chargement par Paseo
  ([orchestration](../operations/paseo-agent-orchestration.md#L43)).
- `Paseo list_profiles` (métadonnées hôte, lecture seule, 2026-10-23) confirme
  `Luna Audit` xhigh, `Luna Implementer` xhigh et `Luna Focused` high.

## Contradictions documentaires ciblées

| Sujet | Constat vérifié | Statut |
|---|---|---|
| Commandes | Les cibles Make documentées pour backend (`test-backend`, `test-integration`, `lint-backend`, `typecheck-backend`) existent dans `Makefile`:317,516,629,643. Elles n’ont pas été exécutées ; aucune commande invalide n’est confirmée dans ce périmètre. Voir [backend/AGENTS.md](../../backend/AGENTS.md#L37). | CONFIRMED |
| Chemins morts | Les liens Markdown relatifs des six instructions demandées et de l’index skills ont été vérifiés : aucun lien manquant détecté. Ce contrôle ne couvre pas tous les documents du dépôt. Voir [docs/README.md](../README.md#L36). | CONFIRMED |
| Variables Advisor | `OPERATOR_ADVISOR_MAX_TURNS_PER_INVESTIGATION` et `OPERATOR_ADVISOR_MIN_CONSULTATION_INTERVAL_SECONDS` sont déclarées dans [Compose](../../compose.yaml#L130) et [`.env.example`](../../.env.example#L59), mais absentes de `backend/src` ; `Settings` ignore les entrées supplémentaires ([config.py](../../backend/src/cti_app/config.py#L51)). La garde locale applique le budget G2 sans plafond Advisor ([investigation_advisor_tools.py](../../backend/src/cti_app/application/investigation_advisor_tools.py#L1106)). Le pacing de soumission utilise `openai_bridge_min_submission_interval_seconds` ([config.py](../../backend/src/cti_app/config.py#L107), [bridge_investigator.py](../../backend/src/cti_app/application/bridge_investigator.py#L212)). | CONFIRMED |
| Réglages annoncés actifs hors ces variables | Aucun constat exhaustif sur les autres réglages ou sur leur consommation effective en runtime. | UNKNOWN |
| Autorisations | L’archive Mission 00 rapporte une contradiction historique commit/push (`DOCUMENTATION_CONFLICTS.md`:7-8). Le registre courant interdit push/merge et autorise les commits locaux ([TASKS.md](TASKS.md#L2-L4)); ce conflit historique ne constitue pas une autorisation live actuelle. | CONFIRMED |

## Statuts S0

| Section | Statut | Motif |
|---|---|---|
| Identité Git et base | PASS | HEAD, branche et base vérifiés localement. |
| Runtime (HEAD testé / image live inspectée / code chargé) | UNKNOWN | Aucun test exécuté ; accès Docker refusé ; code chargé non observable. |
| Corpus et état DB | UNKNOWN | Faits Mission 00 seulement ; aucune restauration ni interrogation locale. |
| Marqueur de migration | PASS | `0002_c2_experiments` lu dans le code au HEAD local. |
| Skills et profils | PASS | Inventaires filesystem et métadonnées Paseo vérifiés. |
| Contradictions ciblées | PASS | Constats limités aux références et recherches décrites ci-dessus ; consommation runtime hors contrôle local marquée `UNKNOWN`. |

Coût observé : `UNKNOWN`.
