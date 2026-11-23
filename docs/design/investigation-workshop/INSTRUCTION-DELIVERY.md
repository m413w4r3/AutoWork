# M24.1 F3-D — Livraison des instructions de session (conception)
**Statut :** conception, aucun code. Base `6c78ffa2`. Remplace la lacune « Known gap » de
[SESSION-RUNNER.md](SESSION-RUNNER.md):171-180 après acceptation ; ne modifie ni ADR-0011 ni
ADR-0007. Vocabulaire : [GLOSSARY.md](GLOSSARY.md). Contrat hôte : SESSION-RUNNER §6.
**VÉRIFIÉ** = lu dans le code à ce HEAD ; **INFÉRÉ** = non prouvé.

## 1. État vérifié
### 1.1 Persistance et états
- Table `agent_session_instructions` (`backend/migrations/versions/0023_agent_sessions.py` :
  `revision` :34, `create_table` :735-810, garde downgrade :864-885) ; modèle
  `infrastructure/database/models/agent_sessions.py:202-278` ; états fermés
  `queued|delivered|acknowledged|cancelled` (`:48`) ; aucune colonne d’essai, de jeton, de diff
  ou d’erreur ; unique `(session_id, idempotency_key)` (`:207-211`) ; FK composite
  `(session_id, investigation_id, task_id)` → `agent_sessions` (`:218-227`).
- Domaine `InstructionState` (`domain/agentic_operator.py:132-137`), transitions (`:224-231`),
  `AgentSessionInstructionV1` (`:1065-1144`). Dépôt
  `infrastructure/database/repositories/agent_sessions.py` : insert idempotent (`:383-401`),
  lecture ordonnée `(queued_at,id)` (`:418-424`), CAS `(id, expected_state)` (`:426-443`).
  `_previous_instruction_state` ne déduit qu’un prédécesseur unique
  (`application/agentic_sessions.py:2110-2120`) : inutilisable pour `delivering`.
- `agent_sessions.state` (`models/agent_sessions.py:39-46` ; `domain/agentic_operator.py:70-98`)
  n’est écrit que par le backend via CAS `state_version` (`agentic_sessions.py:1875-1889`).

### 1.2 Qui persiste, livre, acquitte
- Enqueue humain : `POST …/agent-sessions/{sid}/instructions` action `enqueue`
  (`api/agent_sessions.py:434-454`) → `SessionService.enqueue_instruction_with_notes`
  (`application/agentic_sessions.py:944-1060`) ; session admise `running`,
  `awaiting_instruction` ou `interrupt_requested` (`:984-991`). Livraison immédiate **seulement**
  si l’état backend est `awaiting_instruction` avec opérateur et `host_session_ref`
  (`:1019-1052`), sinon la ligne reste `queued`.
- Reçu hôte : `accepted:true` ⇒ `ACCEPTED`, que `delivered` vaille `"live"` ou `"queued"`
  (`infrastructure/agentic/host_operator.py:312-353` ; `application/agentic_host_sessions.py:484-487`) :
  une simple mise en file hôte est donc comptée comme livrée.
- Ack : route signée `ack` (`api/agent_sessions.py:455-471`) → `acknowledge_instructions`
  (`agentic_sessions.py:1062-1104`) ; `queued` est promue `delivered` puis `acknowledged` dans le
  même appel (`:1094-1101`). Aucun écrivain de `cancelled` (lecture seule, `:1089-1092`) ; aucune
  route de pull (`SessionService.instructions()` `:908-913` sans appelant ; `list_instructions`
  ne sert qu’à la position de file `:2128-2132` et à l’usage des notes
  `application/workspace_notes.py:1076-1087`).
- Catalogue MCP shim (`sidecars/workspace_agent/sessions/catalog.py:229-500`) : `advisor.consult`,
  `task.heartbeat`, `task.submit_result`, `handoff.submit`, `note.capture`,
  `contribution.import`, outils de registre — **aucun outil d’instruction** ; la route POST
  `instructions` est pourtant déjà autorisée (`api/main.py:845-868`), donc l’ack existe côté
  serveur mais est inatteignable depuis un CLI réel (catalogue fermé :
  `backend/tests/workspace_agent/test_session_shim_contract.py:498-515`).
- Notes : `WorkspaceNoteCatalog.prepare/refresh` (`application/investigation_transfers.py:1279-1379`)
  embarque les `(path, revision_id, sha256)` exacts et un diff visible, mais `refresh` n’est
  appelé qu’à la livraison immédiate (`agentic_sessions.py:1024-1039`) et à l’ack (`:1123-1141`),
  jamais à une livraison différée ; une note supprimée rend `current_revision_id=""` et le texte
  est livré quand même (`investigation_transfers.py:1345-1374`). La continuation WS2
  (`POST …/workspace/notes/{revision_id}/ask-agent`,
  `api/investigation_workspace_notes.py:515-541` → `ask_agent_continue`,
  `application/workspace_notes.py:861-922,1310-1320`) appelle `enqueue` via un port construit
  **sans** `note_resolver` (`api/investigation_workspace_notes.py:380-411`).

### 1.3 Transport hôte et points de réception
- Backend → hôte : `HostSessionsPort.send_instruction` (`application/agentic_host_sessions.py:750`),
  client httpx UDS (`infrastructure/agentic/host_sessions.py:180-192`), route
  `POST /v1/sessions/{id}/instructions` (SESSION-RUNNER §6). Supervisor `instruction()`
  (`sessions/supervisor.py:1833-1899`) : dédup en mémoire (`handle.instructions`, `:824`),
  écriture JSONL sur stdin **si** `state is RUNNING` et `_write_stdin` réussit (`:1878-1884`),
  sinon empilement dans `handle.queue` (`:1886`) et réponse `delivered:"queued"`. Ni
  `handle.queue` ni `handle.instructions` ne sont persistés (`_persist` n’écrit que le
  `SessionRecord`, `:2377-2383`) ; `_drain_queue` (`:1901-1916`) n’est appelé qu’en fin de
  `_launch` (`:1589`) ; `_write_stdin` (`:1918-1928`) ne consulte jamais
  `probe.flags.streaming_stdin` : il prouve un tuyau, pas une consommation.
- Codex : prompt statique via `stdin_data` (`adapters/codex.py:541-570`), stdin fermé après feed
  (`process.py:222-236,453-471`), `streaming_stdin=false` (`codex.py:245-252`), live non prouvé
  (`:349-352`). Claude : prompt positionnel sans `stdin_data` (`adapters/claude.py:790-830,853-896`),
  `streaming_stdin=false` et interdiction de rapporter du live (`:288-296,626-635`) ; **INFÉRÉ** :
  sans `--input-format`, un `_write_stdin` réussi serait un faux « live ». Aucun prompt de run ne
  contient d’instruction : Codex lit le brief statique (`codex.py:572-612`), Claude `prompt_path`
  (`claude.py:820-835`) ; `start_session` envoie une requête vide (`host_operator.py:287-293`) ;
  `resume` lance `run_prompt=None` (`supervisor.py:1487`) ; le cadre `run_prompt` post-spawn
  (`:1584-1588`) n’est jamais alimenté et course le feed stdin.
- `awaiting_instruction` existe dans les machines (`domain/agentic_operator.py:76,173-183` ;
  `sessions/state.py:53,69`) mais **aucun producteur** : fin de run propre → `stopped`
  (`supervisor.py:1767-1772`) ; le backend ne restaure `awaiting_instruction` que si l’état
  antérieur l’était (`agentic_sessions.py:1379-1391`). `request_input` est un statut métier de
  tâche (`application/investigation_tasks.py:1069-1110`) et ne touche pas la session.
- Retour d’état : le backend ne consomme **aucun** événement/log hôte (`get_events`/`get_logs`
  sans appelant hors définitions) ; `get_session_status` ne sert qu’à la libération
  (`agentic_sessions.py:568-580`). L’agent hôte n’ouvre aucune connexion sortante
  (WORKSPACE-AGENT §1) : il ne peut pas pousser. Balayages : baux, orphelins, libérations
  (`application/investigation_task_sweep.py:95-180` ; `workers/tasks.py:689-724` ;
  `workers/scheduler.py:37-46`), aucune référence aux instructions.
- Un futur `awaiting_instruction` réel heurte `collect` (refus, `supervisor.py:2054-2059`),
  `close` (refus, `:2285-2295`) et `interrupt` sans processus (reste `interrupt_requested`,
  `:1946-1988`), donc la libération retombe sur un timeout.

## 2. Lacunes
| # | Lacune | Preuve |
|---|---|---|
| G1 | Aucun producteur de `awaiting_instruction` (hôte et backend) : la livraison immédiate est morte. | §1.3 |
| G2 | File écrite seulement : pas de pull, `instructions()` sans appelant. | §1.2 |
| G3 | `ack` promeut `queued` → `delivered` : fausse preuve ; seul cycle testé (`tests/test_agent_sessions.py:1201-1232`, `tests/test_investigation_transfers.py:1474-1483`). | §1.2 |
| G4 | `"queued"` après mise en file **volatile** traitée comme livrée : perte au redémarrage, jamais re-livrée. | §1.3 |
| G5 | `_write_stdin` ignore `streaming_stdin` ; pour Claude un tuyau ouvert suffit ; pour Codex le drain course le prompt. | §1.3 |
| G6 | Aucun jeton d’essai : timeout après acceptation ou crash entre effet et commit = effet ambigu indécidable ; un rollback peut perdre la ligne. | §1.2-1.3 |
| G7 | Aucune annulation, aucun traitement à la perte/au transfert : les lignes `queued` restent à jamais. | §1.2 |
| G8 | Diff calculé à l’ack, pas à la livraison ; texte/diff livrés non persistés ; note supprimée livrée « révision absente ». | §1.2 |
| G9 | Aucune synchro d’état hôte, aucun balayage de livraison, aucune reprise dans une passation. | §1.3 |
| G10 | Outil d’ack absent du shim : même corrigé, un CLI réel ne peut pas acquitter. | §1.2 |
L’indexation de la note (`workspace_file_revisions`) ne prouve jamais la réception par l’agent :
seules une remise hôte prouvée puis un ack signé la prouvent.

## 3. Conception
### 3.1 Décisions
| # | Décision |
|---|---|
| D1 | **Persistance d’abord** : `enqueue` commit `queued` avant tout appel hôte ; l’effet hôte précède le commit `delivered`, jamais l’inverse. |
| D2 | **Point de réception = `awaiting_instruction` technique**, produit par le superviseur quand un run finit proprement et que la session reste ouverte ; distinct de `awaiting_input` métier. |
| D3 | **Drain piloté par le backend** : l’agent hôte ne peut pas pousser (WORKSPACE-AGENT §1). Boucle de fond dans le processus backend (`api/main.py:185-626`, après le runtime `:620-624`) avec `app.state.host_sessions` brut, `uow_factory` et la composition déjà présents. Rejetés : worker Dramatiq (pas de montage du socket hôte — `compose.yaml:307` backend vs `:351-378` worker — et duplication du jeton) et tout POST poussé par l’hôte. |
| D4 | **Primitive de livraison hôte unique** : à un point de réception, une instruction déclenche un **nouveau run** dont le prompt contient le texte livré ; plus de file volatile ni de faux `"queued"`. Le canal live (`streaming_stdin`) reste une optimisation future gatée par la sonde. |
| D5 | **Trois phases avec jeton d’essai** et revérifications dans la transaction qui persiste `delivered` (§3.3). |
| D6 | **Ack explicite signé** après livraison via deux outils shim ; `queued` n’est jamais acquittable ; broker inchangé (F2 intact). |
| D7 | **Notes rafraîchies à la livraison**, diff persisté, texte livré reconstructible ; note supprimée = refus. |
| D8 | **Passation** : instructions non consommées citées dans le handoff et annulées ; les `delivered` restent historiques ; jamais copiées vers une nouvelle session. |

### 3.2 Machine à états
États : `queued` (persistée, aucun effet hôte accepté) ; `delivering` (essai réservé par
`delivery_token`, effet possible en vol) ; `delivered` (remise au CLI prouvée : run lancé avec
le prompt, ou canal live prouvé) ; `acknowledged` (**terminal**, ack signé de l’opérateur) ;
`delivery_unknown` (reçu indécis, réconciliable, jamais re-livré à l’aveugle) ; `cancelled`
(**terminal**, annulation humaine motivée).

| Transition | Déclencheur | Conditions |
|---|---|---|
| `queued → delivering` | drain inline ou boucle | CAS `expected_state=queued`, jeton neuf, essai +1 |
| `delivering → delivered` | reçu hôte `live`/`next_run` | revérifications §3.3, même transaction |
| `delivering → queued` | refus hôte typé (aucun effet) | erreur posée, jeton effacé |
| `delivering → delivery_unknown` | timeout/transport/crash ou réservation > grâce | jeton conservé |
| `delivery_unknown → delivered` | événement hôte `instruction_delivered` (id+sha) ou ack | preuve, aucun renvoi |
| `delivery_unknown → queued` | hôte joignable, absence de remise prouvée | jeton effacé |
| `delivered → acknowledged` | route ack signée | CAS, rejeu idempotent |
| `queued/delivery_unknown → cancelled` | humain vérifié, action `cancel` | raison bornée |
| `acknowledged`, `cancelled` | — | terminaux |
Interdits : `queued → delivered`, `queued → acknowledged`, `delivered → cancelled`, toute
transition depuis un terminal.

### 3.3 Drain et revérifications
**Déclencheurs.** (a) `enqueue` : commit `queued`, puis tentative inline best-effort si l’état
backend est déjà `awaiting_instruction` (INS-01). (b) Boucle de fond toutes les
`M24_INSTRUCTION_DRAIN_INTERVAL_SECONDS` (défaut 10 s, 2–300), lot
`M24_INSTRUCTION_DRAIN_BATCH_SIZE` (défaut 50), verrou consultatif de transaction
`pg_try_advisory_xact_lock(hashtextextended('m24.instruction-drain',0))`. Elle réconcilie
d’abord les `delivering` périmés et `delivery_unknown`, puis draine les `queued` par
`(queued_at,id)`. Candidat = session active portant une instruction
`queued|delivering|delivery_unknown` (index partiel §4).

**Par session, une instruction :**
1. `host.get_session_status(session_id)` via le client brut (pas les `ProbedHostFacts` en
   mémoire, `host_operator.py:208-217`, donc survit à un redémarrage backend). Hôte
   `awaiting_instruction` + backend `running` ⇒ CAS session `running → awaiting_instruction` ;
   hôte `running` ⇒ pas d’essai v1 ; hôte `lost|closed|failed|stopped` ou 404 ⇒ erreur
   `host_session_lost`, rien d’autre n’est modifié (le bail/la réconciliation suivent leur cours).
2. Lire session, tâche, instruction ; exiger la capacité persistée
   `supports_next_run_instruction` (§3.4), `task.status ∈ (running, awaiting_input)`, bail
   courant (`lease_session_id`, `lease_fence`, `lease_expires_at > now` — règle
   `agentic_sessions.py:2085-2100`), session non terminale, identités exactes,
   `instruction.text_sha256` recalculé ; recalculer le texte livré depuis les refs de note
   **dans cette transaction** (dernières révisions canoniques du workspace lié), diff visible,
   refus si une note enregistrée n’a plus de révision ; SHA du texte livré.
3. **Phase A (courte, verrouillée)** : ordre advisory enquête → tâche `FOR UPDATE` → session →
   instruction. Poser le gate de divulgation avec les entrées mutables relues dans cette UoW
   (enveloppe G2, révocations, binding OSINT) ; snapshot/TLP immuable pré-lu hors transaction.
   Puis CAS `queued → delivering` (jeton, essai +1, horodatage, SHA). Commit.
4. **Phase B (sans verrou)** : `host_sessions.send_instruction(...)` avec le texte livré et une
   clé hôte dérivée de `(session, instruction.idempotency_key, delivery_sha256)`.
5. **Phase C (courte)** : recharger par id + `state=delivering, token` ; re-vérifier
   session/tâche/bail/fence et divulgation mutable ; reçu décisif ⇒ CAS
   `delivering → delivered` (`delivered_at`, `delivery_mode`, diffs, SHA) + CAS session
   `awaiting_instruction → running` ; refus typé ⇒ `queued` ; exception transport ⇒
   `delivery_unknown`.

Aucun appel hôte sous verrou (SESSION-RUNNER B3). Un crash entre A et C laisse `delivering`,
converti en `delivery_unknown` après `M24_INSTRUCTION_DELIVERY_GRACE_SECONDS` (défaut 120).

### 3.4 Hôte : réception, primitive, capacité
- `_finalize_exit` (`supervisor.py:1739-1782`) : fin de run code 0, session non interrompue,
  budget intact ⇒ `awaiting_instruction` + `state_changed` (au lieu de `stopped`) ;
  `interrupt_requested`, budget et échec restent `stopped`/`failed`. Ajuster `collect`
  (`:2054-2059`) et `close` (`:2285-2295`) pour accepter `awaiting_instruction`, et
  `interrupt` sans processus (`:1946-1988`) pour passer directement à `stopped`.
- `instruction()` (`:1833-1899`) devient la primitive : en `awaiting_instruction`, composer le
  prompt du run suivant (brief + section bornée « instructions en attente » : id, texte livré,
  SHA), `_launch(resume=proven, instruction_block=...)`, émettre
  `instruction_delivered {instruction_id, sha256, mode:"next_run", run}` et répondre
  `delivered:"queued"` **seulement après un spawn réussi** (mappé `delivery_mode='next_run'`),
  état `running`. `"queued"` signifie désormais « livré au run suivant » ; état non livrable ⇒
  refus typé. Supprimer `handle.queue`, `_drain_queue` et le faux `"queued"` ; un mode `live`
  futur réutilise le même reçu.
- `LaunchContext.instruction_block` (borné) : Codex l’ajoute dans `_compose_prompt`
  (`adapters/codex.py:572-612`), Claude au prompt positionnel (`adapters/claude.py:820-835`).
  Un run frais (`--ephemeral` Codex, sans resume) est permis si la reprise n’est pas prouvée :
  la continuité est la passation, jamais la mémoire du CLI.
- Sonde et réconciliation : flag hôte `next_run_instruction`
  (`CapabilityProbe`/`HostCapabilityFlagsV1`) mappé en `supports_next_run_instruction`
  (`domain/agentic_operator.py:347-369`) et persisté dans `capability_probe` ;
  `capabilities_from_probe` (`host_operator.py:486-527`) cesse de perdre les flags utiles ; pas
  de flag ⇒ file intacte, aucune livraison. L’événement hôte `instruction_delivered` (id + sha)
  est lu via `GET /v1/sessions/{id}/events` (scan borné ; `resync_required` ⇒ indécidable, aucun
  renvoi) ; aucun push hôte.

### 3.5 Idempotence et effets ambigus
- Enqueue : unique `(session_id, idempotency_key)` (dépôt `:383-401`) ; rejeu = même ligne.
- Réservation : CAS `queued|delivery_unknown → delivering` avec jeton ; un seul drainer gagne.
- Hôte : clé `(session, idempotency_key, delivery_sha256)` + registre
  `handle.instructions[instruction_id] = {sha256, status, response}`. Rejeu identique ⇒ même
  reçu sans second run ; même id avec autre SHA accepté **seulement** si l’essai précédent n’a
  jamais été livré (absence d’événement), sinon `idempotency_conflict`.
- Ack : rejeu d’un id `acknowledged` = même ligne ; `cancelled` refusé.

| Fenêtre | État | Résolution |
|---|---|---|
| timeout après envoi, hôte injoignable | `delivery_unknown` | aucun renvoi, réconciliation différée |
| crash backend entre B et C | `delivering` → `delivery_unknown` | événement cherché par id+sha |
| événement prouvé | `delivery_unknown → delivered` | aucun appel hôte |
| absence prouvée (hôte vivant, ni événement ni registre) | `delivery_unknown → queued` | nouvel essai possible, même texte |
| hôte redémarré / session perdue | `delivery_unknown` ou `queued`, `host_session_lost` | jamais de run sur session perdue |
| annulation pendant `delivering` | refusée | attendre la résolution puis annuler |
### 3.6 Ack, notes, passation
- **Ack** : deux outils MCP fixes au catalogue shim, mappés sur la route POST déjà autorisée
  (`api/main.py:859-865`) : `instruction.pending` → `{action:"pull"}` (instructions `delivered`
  de la session : id, texte livré reconstruit depuis `body` +
  `delivery_note_diffs`, SHA, refs) ; `instruction.ack` → `{action:"ack","instruction_ids":[…]}`
  (≤32). Aucune URL/chemin. Le service n’accepte que `delivered|delivering|delivery_unknown`
  (les deux derniers sont d’abord promus `delivered` comme preuve de remise) ; `queued` ⇒
  `409 instruction_not_delivered`. Journal de coordination, pas une preuve CTI ; le résultat
  technique n’est pas modifié (pas de broker) et peut citer l’id en prose.
- **Notes** : `prepare` reste la résolution exacte à la mise en file. À la livraison, `refresh`
  relit les dernières révisions canoniques **ingérées** ; diff persisté et texte livré =
  `body + rendu(diff)` ; `delivery_sha256` est le SHA exact. Note modifiée ⇒ livraison avec diff
  visible (INS-05) ; note sans révision ⇒ `note_ref_unresolved`, ligne `queued`, erreur visible.
  L’agent lit le fichier par le bind read-only ; le shim n’expose aucune lecture de révision
  historique (VÉRIFIÉ) : si le fichier change après livraison, le diff le dit, l’ancienne
  révision n’est plus lisible.
- **Passation Codex → Claude** : avant le handoff, lire les instructions source
  `queued|delivery_unknown` et les `delivered` non acquittées ; ajouter un item `work` borné par
  instruction (texte livré + état) avec `HandoffRef(kind='instruction', id=<uuid>)` — nouvelle
  valeur additive de `HandoffRefKind` (`domain/investigation_handoff.py:72-80,196-206`) ;
  après commit, annuler `queued|delivery_unknown` (`cancel_reason='handed_off:<handoff_id>'`) ;
  laisser les `delivered` en historique. Aucune copie de ligne (FK composite) ; le prompt de la
  nouvelle session contient déjà le handoff.

### 3.7 Scénario obligatoire
1. Codex travaille (session et hôte `running`). 2-3. `notes/humaines/piste.md` écrit ; le scan
   polling détecte la révision (WORKSPACE-AGENT §5.6). 4. AutoWork ingère
   `workspace_file_revisions` — **ne prouve pas (8)**. 5-6. L’humain appelle `ask-agent` (texte :
   path/révision/SHA) ; `enqueue` commit `queued`, aucun appel hôte.
2. Le run finit ; le superviseur produit `awaiting_instruction` (7). La boucle voit l’état,
   réserve, lance un run suivant contenant le texte, l’événement prouve la remise et le backend
   passe `delivered` (8). Le texte cite la révision exacte + le diff ; l’agent lit le fichier par
   le bind read-only (9). L’ack signé passe `acknowledged` ; le résultat cite l’id et l’ack est
   la preuve machine (10-11).

### 3.8 Oracles
| ID | Test |
|---|---|
| INS-01 | `test_instruction_delivery::test_idle_session_delivers_immediately_on_enqueue` |
| INS-02 | `::test_running_session_defers_then_delivers_once_at_receive_point` |
| INS-03 | `::test_two_instructions_deliver_in_order_with_their_own_ids` |
| INS-04 | `::test_replayed_enqueue_and_delivery_never_call_host_twice` |
| INS-05 | `::test_note_changed_after_queue_delivers_current_revision_with_visible_diff` |
| INS-06 | `::test_lost_session_keeps_instruction_queued_and_never_delivers` |
| INS-07 | `::test_revoked_disclosure_refuses_delivery_without_consuming` |
| INS-08 | `::test_transport_timeout_is_delivery_unknown_and_reconciled_without_resend` |
| INS-09 | `test_investigation_transfers::test_transfer_cites_pending_instructions_and_cancels_them` |
| INS-10 | `test_sessions_lifecycle::test_restart_marks_awaiting_instruction_lost_and_never_delivers` ; `test_instruction_delivery::test_supervisor_restart_never_redelivers_or_loses_queue` |
| Ack | `test_agent_sessions::test_ack_of_queued_instruction_is_refused` ; `test_session_shim_contract::test_instruction_tools_are_fixed_pull_and_ack` |
| Hôte | `test_sessions_lifecycle::test_clean_run_end_reaches_awaiting_instruction` ; `::test_instruction_at_receive_point_starts_next_run_with_prompt_sha` ; `::test_instruction_while_running_is_refused_not_queued_volatile` |
`tests/test_agent_sessions.py:1201-1232` et `tests/test_investigation_transfers.py:1474-1483`
encodent l’ack d’une instruction `queued` : ils doivent être réécrits (refus), pas contournés.

## 4. Migration `0027_instruction_delivery`
**Nécessaire** : les nouveaux états sont des états de contrôle et `backend/AGENTS.md` interdit
de les encoder en chaînes libres ou clés de métadonnées. Sans `delivery_unknown` typé, un essai
ambigu est indistinguable d’un essai jamais tenté et « jamais de re-livraison aveugle » serait
inapplicable. Additive après `0026_workspace_note_annotations` ; `0001_baseline` intacte.

- `revision = "0027_instruction_delivery"`, `down_revision = "0026_workspace_note_annotations"`.
  **Préflight ADR-0007** : vérifier avant DDL la forme exacte de 0023 (colonnes, types,
  nullabilité, CHECK `ck_agent_session_instructions_*`, index
  `ix_agent_session_instructions_session_queue`, uniques, FK composites) ; tout écart échoue.
- `ALTER TABLE agent_session_instructions` : CHECK d’état remplacé par
  `('queued','delivering','delivered','acknowledged','delivery_unknown','cancelled')` ; CHECK
  `queued`/`delivered` remplacés par `(state IN ('delivered','acknowledged')) = (delivered_at IS NOT NULL)` ;
  ajout de `delivery_attempts bigint NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0)`,
  `delivery_token uuid NULL`, `delivery_mode text NULL CHECK (delivery_mode IN ('live','next_run'))`,
  `delivery_sha256 char(64) NULL CHECK (delivery_sha256 ~ '^[0-9a-f]{64}$')`,
  `delivery_note_diffs jsonb NULL CHECK (jsonb_typeof(delivery_note_diffs)='array')`,
  `delivery_attempted_at timestamptz NULL`, `delivery_unknown_at timestamptz NULL`,
  `last_delivery_error text NULL`, `cancelled_at timestamptz NULL`, `cancel_reason text NULL`.
- CHECK : `(state='delivery_unknown') = (delivery_unknown_at IS NOT NULL)` ;
  `(state='cancelled') = (cancelled_at IS NOT NULL)` et `cancel_reason` non nul ssi `cancelled` ;
  jeton nul ssi `queued|cancelled` ; `delivery_mode|delivery_sha256|delivery_note_diffs` non nuls
  exactement en `delivered|acknowledged` ; `last_delivery_error` interdit en `delivered|acknowledged`.
- Index additif `ix_agent_session_instructions_pending (state, queued_at, id) WHERE state IN
  ('queued','delivering','delivery_unknown')`. **Downgrade** : refuser si la table contient au
  moins une ligne (garde 0023 `:864-885`), puis restaurer l’ancien CHECK et supprimer
  colonnes/index ; jamais sur base vivante.
- Impacts : `0027` ajouté à la chaîne ADR-0011 D2 (amendement orchestrateur) et à
  `tests/test_alembic_chain.py` ; oracles de CHECK/index dans
  `tests/integration/test_investigation_coordination_schema_postgres.py` ; SESSION-RUNNER §3bis
  « Known gap » remplacé par un renvoi à ce document.

## 5. Découpage et séquencement
| Lot | Contenu | Fichiers exclusifs | Dépend |
|---|---|---|---|
| ID-1 | Domaine, modèle, dépôt, migration 0027, fake mémoire, flags | `domain/agentic_operator.py` ; `infrastructure/database/models/agent_sessions.py` ; `infrastructure/database/repositories/agent_sessions.py` ; `application/investigation_persistence.py` ; `migrations/versions/0027_*` ; `tests/test_instruction_delivery_schema.py` | — |
| ID-2 | Service de livraison, boucle lifespan, config, gate scindé | `application/instruction_delivery.py` (nouveau) ; `application/agentic_sessions.py` ; `application/agent_session_disclosure.py` ; `api/main.py` (lifespan seulement, via l’orchestrateur) ; `config.py` ; `.env.example` ; `tests/test_instruction_delivery.py` | ID-1 |
| ID-3 | API : actions `pull`/`cancel`, ack strict | `api/agent_sessions.py` ; `tests/test_agent_sessions.py` (cas instruction seulement) | ID-2 |
| ID-4 | Superviseur : réception, primitive, réconciliation, fin de run | `sessions/supervisor.py` ; `sessions/state.py` ; `sessions/fake_cli.py` ; `tests/workspace_agent/test_sessions_lifecycle.py` ; `test_sessions_instruction_delivery.py` (nouveau) | **LIFE-1 mergé** |
| ID-5 | Adaptateurs : bloc d’instruction, probe flag | `sessions/adapters/codex.py` ; `sessions/adapters/claude.py` ; `tests/workspace_agent/test_codex_profile.py` ; `test_claude_profile.py` | **ADP-1 mergé**, ID-4 (interface `LaunchContext`) |
| ID-6 | Shim : `instruction.pending`/`.ack` | `sessions/catalog.py` ; `tests/workspace_agent/test_session_shim_contract.py` ; `test_session_shim_instructions.py` (nouveau) | ID-3 |
| ID-7 | Transfert/passation : citation + annulation | `application/investigation_transfers.py` ; `domain/investigation_handoff.py` ; `tests/test_investigation_transfers.py` ; `tests/test_investigation_handoffs.py` | ID-2, ID-3 |
| ID-8 | Acceptation INS-01..10 + amendements ADR | `backend/tests/acceptance/**` ; `docs/adr/ADR-0011-*` (orchestrateur) | tous |

**LIFE-1** : `sessions/supervisor.py` et `sessions/state.py` sont sa propriété exclusive jusqu’à
son merge (BROKER-RELAY-EGRESS §7 : « no two lots share a file ») ; LIFE-1 n’ajoute aucun code de
file, ID-4 démarre après, rebase et devient seul propriétaire de `instruction`, `_finalize_exit`,
`interrupt`, `collect`, `close` (aucune édition parallèle, même de fonctions disjointes).
**ADP-1** : `adapters/codex.py`/`claude.py` lui sont exclusifs ; ID-5 attend son merge ;
`LaunchContext.instruction_block` est figée ici : ID-4 la produit, ID-5 la consomme. **F2** :
aucun lot ne touche `application/agentic_tool_broker.py` ; l’ack est une route signée fixe, pas un
outil broker ; un lien structuré résultat→instruction appartiendrait à un lot post-F2.
**Ordre** : ID-1 → ID-2 → ID-3 ‖ ID-6 → ID-4 → ID-5 → ID-7 → ID-8 (ID-4 = chemin critique).

## 6. Risques et questions ouvertes
1. **Cutover strict du fil hôte** (modèles backend et validateurs sidecar rejettent les champs
   inconnus) : backend d’abord (accepte l’absence du flag), hôte ensuite (l’annonce). Hôte ancien
   ⇒ aucune livraison, file intacte ; backend ancien + hôte neuf ⇒ sonde fail-closed, pas de faux
   `delivered`. **Reprise CLI non prouvée (H0)** : v1 lance un run neuf avec brief + bloc
   d’instruction ; la continuité est la passation, pas la mémoire du CLI. Confirmer que c’est
   acceptable pour Codex (`--ephemeral`) avant de dépendre de `exec resume`.
2. **Latence** : 10 s par défaut ; une instruction déposée juste après la fin d’un run attend le
   tick suivant. **Ingestion des notes en retard** : la livraison ne voit que les révisions
   canoniquement ingérées ; un fichier modifié non scanné rend le diff incomplet (scan/ingestion
   reste la responsabilité WS1/WS2 avant `ask-agent`).
3. **Réouverture sans transfert** : une session perdue laisse ses instructions `queued` ; une
   nouvelle session ouverte sans transfert ne les voit pas. Faut-il exiger une décision humaine
   (annuler / citer) à l’ouverture ? **Réplicas backend** : le verrou consultatif par tick évite
   le double travail, mais chaque réplica doit monter le même socket/jeton (sans effet en
   mono-réplica actuel).
4. **Ack ≠ compréhension** : l’ack prouve qu’un processus autorisé a lu et déclaré consommé le
   texte, pas la prise en compte technique, qui reste humaine/éditoriale.
5. **Décisions humaines** : (a) run neuf plutôt qu’une reprise non prouvée pour Codex ;
   (b) délai de boucle 10 s ; (c) nouvelle valeur `instruction` dans `HandoffRefKind` ;
   (d) une instruction `delivered` non acquittée reste historique lors d’un transfert (seules
   les non livrées sont annulées).
