# Mission 04 — S3-D / axe A : writers et ordre de verrous

**STATUS: ANALYSIS_COMPLETE**
**Base vérifiée:** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Portée:** analyse de sources et contrats seulement. Aucun code, test, migration, serveur, PostgreSQL, provider ou run CTI n'a été lancé ni modifié.

Les marqueurs **VERIFIED**, **INFERENCE**, **PROPOSED** et **UNKNOWN** distinguent les faits de code, leurs conséquences et les choix encore à implémenter. Les numéros sont ceux de la base indiquée.

## 1. Schéma et transactions observés

- **VERIFIED** — Les modèles d'investigation déclarent `tool_invocations`, `investigation_envelopes`, les tables `investigation_budget_*` et `investigation_working_states` dans `investigation.py:60-81,547-707`. Les modèles d'exécution déclarent `model_runs`, `model_conversations` et `model_conversation_turns` dans `model_execution.py:39-124,143-247`. Les autres tables observées sont `retrohunt_reservations` (`models/retrohunt.py:25-62`) et `sample_acquisition_attempts` (`models/production.py:469-508`). La baseline 0001 importe les modules de modèles et crée le contenu de `Base.metadata` sauf les deux tables C2 (`migrations/versions/0001_baseline.py:12-45`); 0002 ne porte que les expériences C2 (`migrations/versions/0002_c2_experiments.py:18-26`).
- **VERIFIED** — Il n'existe pas de modèle, de repository ou de DDL S3 pour `external_effect_dispatches`, `external_effect_uses`, les événements de dispatch ou les reçus d'usage; ces objets sont prévus par le design et L11, pas implémentés (`DESIGN_OPTIONS.md:359-422`; `IMPLEMENTATION_PLAN.md:266-296`). Le nom `bridge_turns` n'est pas une table actuelle: l'état Bridge est porté par `model_conversation_turns` et `model_runs`.
- **VERIFIED** — Les réservations utilisées par le dispatcher résident aujourd'hui dans le JSON `investigation_envelopes.budget_snapshot`. Les tables relationnelles `investigation_budget_records`, `investigation_budget_reservations` et `investigation_budget_reservation_amounts` existent dans le modèle (`investigation.py:585-675`) et sont exposées par l'UOW (`uow.py:366-368`), mais les chemins S3 courants tracés n'appellent pas `uow.investigation_budgets`.
- **VERIFIED** — `create_async_engine` ne fixe pas de niveau d'isolation (`session.py:9-14`). L'UOW crée une `AsyncSession` par unité et y câble les repositories; `commit()` commit cette session, tandis qu'une exception ou une sortie sans commit déclenche le rollback (`uow.py:296-303,323-368,410-430`). Les chemins inspectés n'emploient pas `begin_nested()` ni de savepoint.
- **INFERENCE** — Le défaut PostgreSQL usuel est `READ COMMITTED`, mais le niveau réel ne peut pas être établi depuis les sources: aucun `isolation_level` n'est configuré ici et la configuration du serveur n'a pas été interrogée. Pour rendre les oracles déterministes, les transactions S3 devraient demander explicitement `READ COMMITTED` (ou vérifier la valeur effective).
- **VERIFIED** — Les lectures ordinaires de `tool_invocations` et `model_conversation_turns` ne prennent pas `FOR UPDATE`; les écritures `UPDATE` ou flush obtiennent toutefois leurs verrous de ligne jusqu'au commit. Les repositories d'enveloppe et d'état font explicitement `SELECT ... FOR UPDATE`, vérifient la version puis `UPDATE` (`repositories/investigation.py:272-315,1360-1411,1418-1467`). Les opérations repository ne commitent pas.

## 2. Inventaire des writers d'état S3

| Writer / chemin | Tables écrites ou verrouillées et ordre actuel | Transaction, frontière HTTP et preuve |
|---|---|---|
| **Consume S3** | **VERIFIED:** absent du code. Il n'y a ni table dispatch, ni permis durable, ni transaction qui passe `PREPARED → CONSUMED`. Le chemin historique s'en approchant crée/charge l'invocation, fait `PENDING → RUNNING`, puis réserve le budget dans l'enveloppe: `tool_invocations → investigation_envelopes`. | Le chemin historique commit l'invocation et la réservation avant le dispatcher (`investigation_service.py:1031-1078,1110-1153`). Il relit ensuite l'arrêt et l'enveloppe sans garder leurs verrous, puis appelle le dispatcher (`1155-1237`). Le callback de pré-dispatch revalide l'enveloppe, mais ne verrouille pas ni ne revalide l'invocation et ne réinclut pas l'arrêt (`1190-1226`). **INFERENCE:** il existe une fenêtre entre la lecture d'admission/stop et l'effet; S3 doit remplacer cette vérification par le consume atomique décrit plus bas.
| **Réconciliation invocation** | **VERIFIED:** lit `tool_invocations`, lit l'enveloppe, effectue l'`UPDATE ... WHERE id AND version` de l'invocation (verrou implicite), puis sauvegarde l'enveloppe (`SELECT FOR UPDATE` + CAS), ajoute la décision d'audit: `tool_invocations → investigation_envelopes → analyst_decisions`. | Un même UOW est commité à la fin (`investigation_service.py:1800-10,1870-1910`; repository `investigation.py:272-315,1426-1467`). Aucune HTTP. L'ordre invocation puis enveloppe est le constat N1 du review (`R7_S3_REVIEW_R2.md:20-22`).
| **Settlement d'un outil** | **VERIFIED:** après réponse, sauvegarde les observations, met à jour l'invocation, puis appelle `_complete_tool_reservation`, qui relit/règle le budget et sauvegarde l'enveloppe: `tool_invocations → investigation_envelopes`. | Le commit couvre les deux écritures (`investigation_service.py:1269-1319,2761-2832`). Le helper boucle jusqu'à trois CAS d'enveloppe. L'ambiguïté conserve la réservation; une preuve framework de non-envoi peut la libérer (`2771-2817`). Aucun verrou ne traverse l'appel du dispatcher, le commit d'admission a lieu avant (`1152-1237`). L'inversion est aussi N1 (`R7_S3_REVIEW_R2.md:20-22`).
| **Refus / denial** | **VERIFIED:** le refus G2 après lecture non verrouillée de l'enveloppe insère/transitionne l'invocation en `POLICY_DENIED`, puis commit; l'enregistrement de réduction de politique met le working state à jour après ce commit (`investigation_service.py:1512-1562,2625-2656`). Les appels bloqués en lot écrivent des invocations puis commit (`2665-2701`). Un résultat `POLICY_DENIED` renvoyé par le dispatcher passe aussi par la mise à jour d'invocation puis settlement d'enveloppe (`1240-1268,1299-1319`). | Le refus d'un appel qui n'a pas encore de dispatch n'a pas d'état dispatch durable. Le chemin G2 pré-transport ne verrouille pas l'enveloppe en même temps que l'invocation. Le settlement générique peut, lui, prendre l'invocation avant l'enveloppe.
| **Working state: arrêt et fin de tour** | **VERIFIED:** `conclude()` sauvegarde `investigation_working_states` (FOR UPDATE/CAS) puis l'agrégat `analyst_investigations` (UPDATE versionné); l'endpoint interrupt commit ensuite (`investigation_service.py:499-521`; `api/investigation.py:2525-2595`). La fin normale de tour écrit working state puis `analyst_investigations`, commit (`investigation_service.py:1711-1781`). | Ces chemins ne modifient pas invocation/enveloppe dans cette transaction. Le stop seul prend donc le rang working-state. Le début d'un tour Bridge est différent: le contexte pending écrit l'état, puis la réservation `BRIDGE_TURNS` écrit l'enveloppe avant le commit (`investigation_service.py:626-659`), donc `working_state → envelope` dans le même UOW. **VERIFIED:** le chemin operator-led écrit également un marqueur/état, puis appelle l'exécution qui écrit invocation et enveloppe avant son commit (`operator_tool_invocation.py:1031-1117`, composition partage les mêmes repositories et service à `investigation_composition.py:1610-1650`). Son ordre actuel est `working_state → tool_invocation → envelope`.
| **Réservation opérateur dans l'enveloppe** | **VERIFIED:** `budget.reserve()` modifie d'abord le snapshot en mémoire; le repository d'enveloppe fait ensuite `SELECT ... FOR UPDATE`, compare la version et met à jour JSONB (`investigation_service.py:1040-1078,1110-1153,2610-2618`; `repositories/investigation.py:1418-1467`). L'invocation est déjà écrite, ce qui donne `invocation → envelope`. Dans operator-led, le working state précède également les deux (`operator_tool_invocation.py:1031-1117`). | Réservation `RUNNING` + enveloppe commitées avant le dispatcher/HTTP. `investigation_budget_*` n'est pas utilisé par ce chemin. Les services de gouvernance approuvent/étendent l'enveloppe par CAS; l'extension écrit enveloppe puis investigation (`investigation_governance.py:327-349`). La révocation synchronise d'abord l'investigation puis sauvegarde l'enveloppe (`441-487`), ordre inverse pour cette ligne auxiliaire.
| **Slot Retrohunt** | **VERIFIED:** `retrohunt_reservations.reserve()` fait une lecture de doublon puis tente les slots par `INSERT ... ON CONFLICT DO NOTHING`; l'unicité `(subject_id, slot_number)` et `(subject_id, operation_key)` arbitre le slot (`repositories/retrohunt.py:23-64`; `models/retrohunt.py:25-42`). Un UOW commit le slot `RESERVED`; un second fait le CAS `RESERVED → SUBMITTING` par `UPDATE ... WHERE version`, commit avant le provider. Après le provider, un autre UOW fait le CAS terminal (`application/retrohunt.py:152-194,199-239,402-451`). | Aucun `FOR UPDATE` explicite, mais le `UPDATE` verrouille la ligne. Pas de verrou pendant `submit_retrohunt`, `retrohunt_status` ou `retrohunt_results` (`application/retrohunt.py:199-239,241-329`). Le consume S3 devrait prendre dispatch avant ce slot et committer avant l'envoi.
| **Acquisition / `sample_acquisition_attempts`** | **VERIFIED:** ce tableau n'est pas une réservation: seules des lignes `success`/`error` sont permises (`models/production.py:469-508`). Le service dépense d'abord `analyst_investigations.loop_budget` et commit, puis fait `file_download`; il ajoute la ligne d'essai après téléchargement ou échec (`sample_acquisition.py:349-377,407-430,533-615,622-670`). Les insertions sont append-only par repository, sans `FOR UPDATE` (`repositories/production.py:183-228`). | Le chemin d'acquisition d'investigation consulte les succès/erreurs, réserve le budget externe dans le snapshot d'enveloppe si nécessaire, puis appelle l'acquisition; en mode dispatcher la réservation extérieure est déjà portée par le caller (`investigation_acquisition.py:155-197,341-388,657-679,766-778`). Aucun lock DB n'est gardé pendant le download. **UNKNOWN:** le lien de propriété et le settlement unique entre réservation externe, budget VT-read/hit et réemploi local n'est pas sélectionné; c'est déjà signalé dans `DESIGN_OPTIONS.md:406-412`.
| **Bridge turn / ModelRun: création et soumission** | **VERIFIED:** `ModelConversationService.add_turn` verrouille `model_conversations`, insère `model_runs`, insère `model_conversation_turns`, met à jour la conversation, commit (`model_conversations.py:1198-1302`). Le run et le tour sont durables avant `ModelGateway.execute`. Le Gateway relit/verrouille le run, exige `RUNNING + NOT_SUBMITTED`, incrémente l'état/compteur de tentative, puis commit avant `adapter.invoke`; après l'appel il ouvre un nouveau UOW, verrouille/sauvegarde le run (`model_gateway.py:1023-1144,1163-1217`). | **VERIFIED:** l'appel Bridge/provider est hors transaction DB; le résultat est persisté après l'appel. N3 relève que l'état de soumission ModelRun doit être intégré explicitement au consume S3 (`R7_S3_REVIEW_R2.md:28-30`). Une tentative physique interne doit consommer un dispatch distinct, ou le retry doit être désactivé; voir `DESIGN_OPTIONS.md:351-358` et `IMPLEMENTATION_PLAN.md:298-313`.
| **Bridge turn / ModelRun: finalisation et reprise** | **VERIFIED:** succès verrouille la conversation puis écrit le tour (`model_conversations.py:1330-1366`); l'erreur verrouille la conversation, écrit le tour, puis peut verrouiller le run (`1416-1449`). L'interruption verrouille conversation puis run et écrit ensuite tour/run (`1463-1499`). Certains chemins de reprise verrouillent d'abord le ModelRun, puis la conversation, ensuite le working state et le tour (`631-879`, commentaire d'ordre actuel `798-800`); le seal d'un orphelin fait run → conversation (`1007-1051`), et la reprise orpheline prend conversation → run → working state → écrit tour/run (`910-990`). | Ces chemins utilisent un UOW et commitent chaque décision durable. L'ordre entre conversation, tour, run et working state varie selon le chemin; il n'est donc pas conforme à un consume Bridge global. Les verrous sont locaux aux transactions de persistance; le transport a eu lieu avant leur finalisation.

## 3. Inversions et ordre global réalisable après refactor

### Inversions vérifiées

1. **Invocation ↔ enveloppe.** La réservation historique, la finalisation et `reconcile_invocation` écrivent l'invocation avant de verrouiller l'enveloppe (`investigation_service.py:1075-1138,1299-1319,1870-1910`). Avec le consume proposé enveloppe → invocation, deux sessions peuvent former le cycle décrit par N1 (`R7_S3_REVIEW_R2.md:20-22`).
2. **Working state ↔ enveloppe ↔ invocation.** Le chemin operator-led sauvegarde d'abord l'état, puis passe dans `execute_tool_call` qui écrit invocation puis enveloppe; le consume proposé prendrait enveloppe puis état. Le début de tour Bridge écrit aussi l'état pending avant l'enveloppe (`operator_tool_invocation.py:1031-1117`; `investigation_service.py:626-659`). Une session qui tient l'état et attend l'enveloppe peut donc croiser une session consume qui tient l'enveloppe et attend l'état. **INFERENCE:** deadlock possible si ces transactions sont simultanées.
3. **Working state ↔ conversation/run Bridge.** Les reprises Bridge verrouillent conversation ou ModelRun, puis sauvegardent le working state (`model_conversations.py:770-804,910-990,1007-1051`). Un consume qui verrouille l'état avant la conversation/run peut former le cycle inverse. Les chemins de reprise qui font run → conversation contredisent aussi les finalisations conversation → tour ou conversation → run.
4. **Ligne auxiliaire `analyst_investigations` ↔ enveloppe.** La révocation sauvegarde l'investigation avant l'enveloppe alors que l'extension fait enveloppe puis investigation (`investigation_governance.py:327-349,441-487`). **INFERENCE:** ces opérations peuvent aussi se bloquer mutuellement. Le consume S3 ne doit pas prendre le verrou de l'investigation par nécessité d'autorisation; si un même UOW S3 doit la modifier, il faut lui assigner une place stable.
5. **Course de stop/revocation.** Les vérifications legacy de stop et d'enveloppe sont des lectures non verrouillées avant dispatch (`investigation_service.py:1152-1228,2703-2711`). **INFERENCE:** stop ou révocation peut committer entre le contrôle et le transport. Un check avant HTTP ne constitue pas un consume atomique.

### Ordre proposé

**PROPOSED:** ordre de lignes pour tout UOW qui en verrouille plusieurs:

`investigation_envelopes → investigation_working_states → analyst_investigations (si nécessaire dans le même UOW) → investigation_budget_reservations / amounts (si ce ledger est retenu) → tool_invocations → external_effect_dispatches → réservation/attempt de famille → model_conversations → model_conversation_turns → model_runs → insertions append-only d'audit/usage`

Les rangs absents sont sautés. Pour un owner Bridge, il n'y a pas de rang `tool_invocations`; pour un outil sans sous-système famille, le rang famille est absent. Si `investigation_budget_*` est utilisé, verrouiller d'abord son parent/réservation puis les montants dans l'ordre stable de `dimension`, toujours après enveloppe et avant dispatch. Le snapshot JSON actuel reste verrouillé par la ligne d'enveloppe.

**Ce qui doit changer pour rendre cet ordre effectif:**

- faire de la transaction de consume le seul point de permis; réordonner admission, settlement, denial et réconciliation pour qu'elles commencent par l'enveloppe puis working state si le stop est pertinent, avant toute invocation;
- dans `OperatorToolInvocationService`, déplacer l'écriture du marqueur d'état après le rang enveloppe, ou la committer dans un UOW séparé avant la transaction de réservation/consume; ne pas conserver un verrou d'état en entrant dans `execute_tool_call`;
- séparer ou réordonner l'enregistrement du contexte Bridge pending et la réservation enveloppe: ne pas conserver le verrou working-state lorsqu'on acquiert l'enveloppe;
- réécrire les fonctions d'abandon/orphan/reconcile Bridge pour prendre le working state avant `model_conversations`, le tour puis le run; un repository `get_for_update` du tour est nécessaire. Aligner erreur, annulation et adoption sur conversation → tour → run;
- conserver `analyst_investigations` hors du consume si sa ligne n'est pas nécessaire. Harmoniser malgré tout révocation et extension (par ex. enveloppe → investigation) si elles prennent les deux lignes;
- donner à `sample_acquisition_attempts` un vrai claim pré-transport ou une ligne de réservation familiale dédiée: son append success/error actuelle ne peut pas être verrouillée pour autoriser un envoi;
- commit immédiatement après le consume et avant tout HTTP. Après le retour, ouvrir un autre UOW dans le même ordre pour settlement/CAS. Ne pas garder une transaction ou un verrou de ligne pendant un callback, un retry, Bridge, un transport ou un upload.

**Pourquoi chaque paire de rangs reste sans inversion, après ces changements:**

| Paire | Invariant proposé |
|---|---|
| Enveloppe → working state | Le consume prend les deux dans cet ordre. Stop seul ne prend que le working state; il ne demande ensuite ni enveloppe ni invocation. Les chemins de préparation actuels qui font state → enveloppe sont scindés par commit ou réordonnés.
| Working state → investigation (agrégat) | La fin de tour et `conclude()` écrivent déjà state puis agrégat. La révocation/extension doit faire enveloppe puis agrégat et ne jamais prendre le working state après l'agrégat.
| Enveloppe/state → invocation | Settlement, denial, consume et réconciliation prennent l'enveloppe d'abord puis state si requis, ensuite l'invocation. Les refus locaux qui n'ont pas de réservation ne prennent pas l'enveloppe après avoir mis à jour l'invocation.
| Invocation → dispatch | Le dispatch est verrouillé/validé après son owner invocation. Un owner Bridge saute ce rang et est validé par son tour/run exact. Aucun ancien dispatch n'existe à conserver.
| Dispatch → famille | Claim/slot est réservé après dispatch et par clé déterministe; les mises à jour d'une famille ne reviennent jamais modifier dispatch dans un UOW inversé. Les append-only de résultat acquisition ne servent pas de claim.
| Famille → conversation → tour → run | Les writers Bridge d'abandon/reprise/finalisation doivent se conformer à la même séquence. Les writes ModelRun seuls ne demandent pas de verrou de rang supérieur après le run.
| Toute ligne → HTTP | Commit avant le transport et nouvel UOW après. Les retries physiques reprennent toute la séquence avec un dispatch distinct; aucune transaction du premier essai n'est gardée ouverte.

**INFERENCE, sémantique linéaire à fixer:** le comportement sans verrou pendant HTTP est réalisable si le commit `CONSUMED` est le point de décision. Révocation commitée avant le consume ⇒ consume refuse. Consume commité avant la révocation ⇒ ce dispatch a déjà reçu son droit one-use, même si le transport commence après le commit de révocation. Si le besoin est «une révocation qui committe avant le début du POST doit toujours empêcher le POST», cette garantie stricte ne découle pas d'un verrou DB relâché avant HTTP; elle exige un contrat/point de sérialisation supplémentaire et reste **UNKNOWN**.

## 4. Pseudo-transactions SQL proposées

Les exemples ci-dessous sont **PROPOSED**, non exécutés. `external_effect_dispatches` n'existe pas encore; ses noms de colonnes sont des champs requis pour exprimer le CAS, à finaliser dans le schéma additif. Chaque transaction utilise `READ COMMITTED`, des clés d'owner/route/digest validées sur lignes durables, un timeout borné, et un commit avant effet externe.

### 4.1 Consume

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;

SELECT * FROM investigation_envelopes
 WHERE investigation_id = :iid FOR UPDATE;
-- Refuser si version/approval/provider/TLP/DNT/external_llm_allowed ne matchent pas.
-- Vérifier dans budget_snapshot que reservation_operation_key est RESERVED et exacte.

SELECT * FROM investigation_working_states
 WHERE investigation_id = :iid FOR UPDATE;
-- Refuser si version attendue différente, stop_reason IS NOT NULL,
-- ou checkpoint/turn/context attendu différent.

-- Si utile au même UOW: SELECT analyst_investigations après working_state.
-- Pour owner opérateur/advisor:
SELECT * FROM tool_invocations
 WHERE id = :invocation_id AND investigation_id = :iid FOR UPDATE;
-- Exiger status='running', version=:inv_version, tool/schema/key correspondant au route owner.

SELECT * FROM external_effect_dispatches
 WHERE id = :dispatch_id AND investigation_id = :iid FOR UPDATE;
-- Exiger status='prepared', version=:dispatch_version, exact owner_kind/id,
-- effect_key, route_id/version, request_sha256, occurrence/step et reservation.

-- Si Retrohunt: verrouiller/CAS la réservation exacte après le dispatch.
-- Si acquisition: utiliser la future ligne RESERVED, pas une ligne success/error.
-- Si Bridge: après ces rangs, verrouiller conversation, turn, puis model_run;
-- exiger turn RUNNING + digest/contexte exact, run RUNNING + NOT_SUBMITTED + attempt attendu.

UPDATE external_effect_dispatches
   SET status='consumed', version=version+1, consumed_at=now()
 WHERE id=:dispatch_id AND status='prepared' AND version=:dispatch_version
   AND request_sha256=:request_sha256;
-- Exiger rowcount=1; éventuel update ModelRun begin_submission_attempt dans le même UOW.
COMMIT;
-- Émettre le permis depuis la ligne commitée; seulement maintenant appeler le transport.
```

### 4.2 Réconciliation d'invocation / dispatch

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_envelopes WHERE investigation_id=:iid FOR UPDATE;
SELECT * FROM investigation_working_states WHERE investigation_id=:iid FOR UPDATE;
SELECT * FROM tool_invocations
 WHERE id=:invocation_id AND investigation_id=:iid FOR UPDATE;
SELECT * FROM external_effect_dispatches
 WHERE id=:dispatch_id AND investigation_id=:iid FOR UPDATE;
-- Revalider versions, owner, reservation_operation_key, digest et status NEEDS_REVIEW.
-- Pour ABANDON/Bridge: poursuivre après dispatch dans l'ordre famille, conversation, tour, run.

UPDATE tool_invocations SET status=:resolved_status,
       response_refs=:validated_refs, consumed_budget=:settled,
       version=version+1, updated_at=now()
 WHERE id=:invocation_id AND version=:inv_version AND status='needs_review';
UPDATE external_effect_dispatches SET status=:reconciled_state,
       version=version+1, reconciled_at=now()
 WHERE id=:dispatch_id AND version=:dispatch_version AND status='needs_review'
   AND request_sha256=:request_sha256;
-- EXECUTED_WITH_RESULT et ABANDON règlent le budget de façon conservative comme aujourd'hui;
-- NOT_EXECUTED libère seulement avec la décision/preuve acceptée.
-- Ajouter la décision opérateur/audit dans ce même UOW.
COMMIT;
```

### 4.3 Settlement après transport

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_envelopes WHERE investigation_id=:iid FOR UPDATE;
-- Si la transition dépend du stop, prendre ensuite working_states FOR UPDATE.
SELECT * FROM tool_invocations
 WHERE id=:invocation_id AND investigation_id=:iid FOR UPDATE;
SELECT * FROM external_effect_dispatches
 WHERE id=:dispatch_id AND investigation_id=:iid FOR UPDATE;
-- Puis famille et, pour Bridge, conversation → turn → run.
-- Vérifier owner, digest et que la réponse appartient à ce dispatch exact.

UPDATE external_effect_dispatches SET status=:succeeded_or_review,
       version=version+1, response_ref=:immutable_result_ref
 WHERE id=:dispatch_id AND status='consumed' AND version=:dispatch_version
   AND request_sha256=:request_sha256;
-- CAS invocation; confirmer la réservation du budget par operation_key dans l'enveloppe.
-- Insérer au plus un reçu (dispatch_id, dimension) dans le futur ledger/événement unique.
-- NEEDS_REVIEW conserve la réservation; aucun échec ambigu ne la libère.
COMMIT;
```

### 4.4 Denial avant consume

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_envelopes WHERE investigation_id=:iid FOR UPDATE;
SELECT * FROM investigation_working_states WHERE investigation_id=:iid FOR UPDATE;
SELECT * FROM tool_invocations
 WHERE id=:invocation_id AND investigation_id=:iid FOR UPDATE;
SELECT * FROM external_effect_dispatches
 WHERE id=:dispatch_id AND investigation_id=:iid FOR UPDATE;
-- Si l'enveloppe/stop/owner/route interdit l'appel, exiger PREPARED (jamais CONSUMED).
UPDATE external_effect_dispatches SET status='denied', version=version+1
 WHERE id=:dispatch_id AND status='prepared' AND version=:dispatch_version;
UPDATE tool_invocations SET status='policy_denied', version=version+1, updated_at=now()
 WHERE id=:invocation_id AND version=:inv_version AND status IN ('pending','running');
-- Libérer la réservation uniquement puisque ce dispatch n'a pas été consommé.
COMMIT;
-- Zéro permis; zéro appel transport.
```

### 4.5 Stop et finalisation

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_working_states WHERE investigation_id=:iid FOR UPDATE;
-- Vérifier expected_version et l'idempotency marker.
UPDATE investigation_working_states
   SET version=version+1, stop_reason=:reason, updated_at=now(), state=:payload
 WHERE investigation_id=:iid AND version=:expected_version
   AND stop_reason IS NULL;
-- Si le même UOW finalise analyst_investigations, le verrouiller ensuite (rang suivant).
COMMIT;
-- Ne pas prendre l'enveloppe, invocation, dispatch, famille ou Bridge après le state.
```

### 4.6 Bridge turn/run

Préparation durable avant le consume, sans appel réseau:

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM model_conversations WHERE id=:conversation_id FOR UPDATE;
-- Vérifier READY/non BUSY et la séquence attendue.
INSERT INTO model_runs (..., status, submission_state, submission_attempt)
VALUES (..., 'running', 'not_submitted', 0);
INSERT INTO model_conversation_turns
       (..., conversation_id, model_run_id, status, input_sha256, idempotency_key)
VALUES (..., :conversation_id, :run_id, 'running', :input_sha256, :key);
UPDATE model_conversations SET status='busy', turn_count=:next, version=version+1
 WHERE id=:conversation_id AND version=:conversation_version;
COMMIT;
```

Consume de la requête Bridge physique:

```sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- Enveloppe → working_state → dispatch; pas d'invocation pour l'owner Bridge.
SELECT * FROM investigation_envelopes WHERE investigation_id=:iid FOR UPDATE;
SELECT * FROM investigation_working_states WHERE investigation_id=:iid FOR UPDATE;
SELECT * FROM external_effect_dispatches WHERE id=:dispatch_id FOR UPDATE;
SELECT * FROM model_conversations WHERE id=:conversation_id FOR UPDATE;
SELECT * FROM model_conversation_turns
 WHERE id=:turn_id AND conversation_id=:conversation_id FOR UPDATE;
SELECT * FROM model_runs WHERE id=:run_id FOR UPDATE;
-- Lier les trois IDs; vérifier turn RUNNING/input digest, run RUNNING/NOT_SUBMITTED,
-- submission_attempt attendu et dispatch route/digest/tour exact.
UPDATE external_effect_dispatches SET status='consumed', version=version+1
 WHERE id=:dispatch_id AND status='prepared' AND version=:dispatch_version;
UPDATE model_runs SET submission_state='submission_in_progress',
       submission_attempt=submission_attempt+1, bridge_request_id=:request_id,
       updated_at=now()
 WHERE id=:run_id AND status='running' AND submission_state='not_submitted'
   AND submission_attempt=:attempt;
COMMIT;
-- Envoyer le POST Bridge après le commit; chaque retry physique = nouveau dispatch/consume.
```

Après le retour Bridge, un autre UOW prend enveloppe, state si pertinent, dispatch, conversation, turn, run dans le même ordre, puis CAS le dispatch, le turn et le run vers succès ou `NEEDS_REVIEW`. Le modèle actuel n'a pas de `version` sur `model_runs` ni `model_conversation_turns`; le CAS doit donc utiliser leurs états/compteur d'essai actuels ou ajouter une version lors de la migration. L'acquittement perdu ne recrée jamais de permis.

## 5. Oracles PostgreSQL à deux sessions (à implémenter, non exécutés)

Chaque oracle utilise deux connexions PostgreSQL indépendantes, des barrières déterministes, les repositories de production, une DB jetable et un faux transport permit-required. Deux sessions sont nécessaires pour démontrer l'arbitrage et l'attente des verrous PostgreSQL réels; un faux in-memory ne vérifie ni les row locks, ni les CAS, ni les contraintes uniques. Configurer un `lock_timeout` court (ex. `2s`) et un `deadlock_timeout` court (ex. `200ms`) par `SET LOCAL`.

1. **Ordre sans deadlock.** A prend les locks consume dans l'ordre et s'arrête après enveloppe (barrière). B commence settlement/réconciliation du même owner; elle doit être bloquée sur l'enveloppe, sans tenir invocation/dispatch/famille. Relâcher A pour commit, puis B doit relire l'état courant et finir en résultat idempotent/stale. Répéter pour working-state ↔ conversation/run. **Assertions:** les deux sessions terminent avant timeout; aucune erreur SQLSTATE `40P01` (deadlock) ni `55P03` (lock timeout); au plus une transition par version. Le scénario inverse historique invocation→enveloppe doit être couvert par une assertion de non-régression d'ordre.
2. **`RUNNING → NEEDS_REVIEW` avant consume.** A committe invocation `RUNNING`, réservation et dispatch `PREPARED`, puis attend avant le consume. B CAS l'invocation à `NEEDS_REVIEW` et committe. A reprend, relit/verrouille l'owner et doit refuser. **Assertions:** dispatch jamais `CONSUMED`; aucun permis créé; aucune invocation rebasculée; compteur d'appels du faux = 0; l'état est terminalement reviewable/denied selon la règle définie.
3. **Révocation avant consume.** B verrouille l'enveloppe, retire provider/droits et committe version `v+1`. A reprend ensuite le consume. **Assertions:** A observe `v+1`/approbation refusée, aucun dispatch consommé, permis nul, appel faux = 0.
4. **Consume avant révocation.** A consomme et committe pendant que B attend l'enveloppe; B révoque après le commit A. **Assertions minimales selon le point linéaire proposé:** un seul dispatch `CONSUMED`, une seule autorisation one-use, aucune seconde tentative. Il faut décider si ce dispatch peut encore être envoyé après le commit de révocation; sans verrou HTTP, la sémantique réalisable est «consume commit gagne». Si le produit exige zéro POST dès que la révocation committe avant l'instant réseau, cet oracle reste bloquant et l'architecture doit définir une autre sérialisation.
5. **Settlement concurrent.** A et B tentent simultanément de régler le même dispatch et la même `reservation_operation_key`, avec barrière après le premier lock enveloppe. Le second attend, puis relit le dispatch terminal. **Assertions:** une seule transition de dispatch, une seule incrémentation de version d'enveloppe, une seule charge du montant réellement consommé, aucune double libération/charge et exactement un reçu `(dispatch_id, dimension)` si ce ledger est ajouté. Les deux retours sont idempotents ou un seul est conflit explicite.
6. **Aucun verrou pendant HTTP.** A committe consume puis bloque le faux transport à l'entrée réseau. B révoque ou pose le stop et doit committer pendant que A est bloquée dans le faux. **Assertions:** B ne timeoute pas sur les lignes consume; le transport A ne détient aucune transaction/verrou DB; le permis n'est utilisable qu'une fois. Cet oracle vérifie aussi la sémantique consume-first choisie au point 4.

## 6. UNKNOWN bloquants et preuves d'implémentation restantes

- **UNKNOWN — Niveau d'isolation effectif** du serveur et configuration PostgreSQL de chaque déploiement; les sources ne le fixent pas.
- **UNKNOWN — Contrat de course consume/revocation** entre commit du permis et début réel du POST; fixer explicitement le point linéaire compatible avec «aucun verrou pendant HTTP».
- **UNKNOWN — Handoff ModelGateway:** le design ne dit pas encore comment le consume passe simultanément le `ModelRun` de `NOT_SUBMITTED` à `SUBMISSION_IN_PROGRESS`, comment il relie le dispatch au turn/run, et comment chaque retry interne obtient son dispatch. N3/N5 le relèvent (`R7_S3_REVIEW_R2.md:28-38`).
- **UNKNOWN — Réservation/settlement acquisition:** `sample_acquisition_attempts` n'a ni état pending ni verrou de réservation; budget VT-read/hit de `analyst_investigations`, réservation d'enveloppe, résultat ambigu et réemploi local ne forment pas un règlement unique (`sample_acquisition.py:349-377,533-670`; `DESIGN_OPTIONS.md:406-412`).
- **UNKNOWN — DDL additif exact:** table dispatch, types d'owner/FK, version/CAS, événements, usage receipt unique, index/reprise et éventuel claim de download ne sont pas définis dans les migrations actuelles. Ne pas backfill les `normalized_parameters` existants; suivre L11 après head 0002 (`IMPLEMENTATION_PLAN.md:268-296`).
- **UNKNOWN — Cohérence de toutes les reprises Bridge:** les chemins run→conversation→state et conversation→run→state actuels doivent être réordonnés et prouvés par l'oracle; `ModelConversationTurn` n'a pas de `get_for_update` repository et ni turn ni run n'ont de version de ligne (`repositories/model_conversations.py:84-124`; `repositories/model_runs.py:24-105`; `models/model_execution.py:82-124,196-247`).
- **UNKNOWN — Liaison durable owner/route/famille:** la clé d'owner doit identifier exclusivement une invocation ou un turn/run; vérifier investigation, tool/schema, reservation, route/version et digest avant permis. N2 rappelle que l'identifiant fourni n'est qu'un locator (`R7_S3_REVIEW_R2.md:24-27`).
- **VERIFIED — Verdict readiness:** le review R7 considère F2–F4 partiels et S3 non prêt (`R7_S3_REVIEW_R2.md:48-56`). L'ordre proposé ci-dessus est une conception réalisable après les refactors listés; il n'est pas établi par le code actuel.

**Prochaine preuve permise:** implémentation locale additive après résolution des inconnues de consume/Bridge/acquisition, puis tests PostgreSQL jetables à deux sessions des points 1–6. Aucun cutover, route production, provider réel ou migration live n'est autorisé par cette analyse.
