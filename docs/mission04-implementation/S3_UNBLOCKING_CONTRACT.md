# Mission 04 — S3-D unblocking contract

**Base vérifiée :** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Portée :** conception seulement ; aucun code, test, migration ou service exécuté.
**Verdict d’entrée :** `S3_BLOCKED_DESIGN`.
**Verdict final :** `S3_BLOCKED_DESIGN`.

Ce contrat réconcilie R7-S3-R2 et les trois analyses S3-D. Les états **VERIFIED**, **INFERENCE**, **PROPOSED** et **UNKNOWN** distinguent les faits observés avec preuve, les conséquences déduites, le contrat choisi pour une mission ultérieure et ce que les sources ne permettent pas de trancher. Une proposition ne décrit pas une garantie déjà implémentée.

## 1. Périmètre et verdict d’entrée

**VERIFIED —** Le HEAD lu est `e120a36f1afa08261fdf0276e0200872973ad8f2`. La cible de ce document n’existait pas au début de la mission. Aucun PostgreSQL, Docker, provider, Bridge, VT, Shodan, Retrohunt, MinIO ou run CTI n’a été lancé ; aucun test n’a été exécuté.

**PROPOSED — verdict d’entrée `S3_BLOCKED_DESIGN`.** La revue R7 garde le gate S3 non atteint : F2–F4 restent partiels et N1–N4 ne sont pas implémentés (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:15-16,48-64`). Ce document fixe des contrats proposés et les refactors qui les rendraient testables. Il ne transforme pas les propositions en preuves de source.

Ce document **n’autorise pas** :

- une route de production, un cutover, une migration exécutée ou une activation d’egress ;
- le réemploi d’un résultat entre invocations ;
- un appel provider, Bridge, VT, Shodan ou Retrohunt ;
- la mise en service de ModelGateway pour un owner non lié à une investigation ;
- le premier slice test-only `vt.file_report`, qui est différé à une mission ultérieure après fermeture des bindings owner/route et du contrat transactionnel.

Le design R2 reste le point de départ pour route figée, dernier mètre, transitions et lots L10–L14 (`docs/mission03-architecture/DESIGN_OPTIONS.md:334-422`; `docs/mission03-architecture/IMPLEMENTATION_PLAN.md:228-328`). Il n’autorise ni code S3 ni route réelle.

## 2. Matrice F1–F4 / N1–N6

Les statuts ci-dessous évaluent la fermeture **au niveau du contrat**. `CLOSED` n’affirme jamais que le code correspondant existe. Les faits de code sont séparés des hypothèses et des oracles futurs.

| Point | Statut | Preuve vérifiée sur la source | Contrat, hypothèse restante et oracle futur |
|---|---|---|---|
| **F1 — gate au dernier mètre, route figée, retry, entrée directe** | **CLOSED (design seulement)** | **VERIFIED —** le dispatcher actuel appelle un adapter après validation de registre/politique, sans permis d’envoi S3 (`backend/src/cti_app/application/investigation_tools.py:619-678,865-885`). La composition injecte encore le transport advisor (`backend/src/cti_app/application/investigation_composition.py:1539-1559`). R2 exige une route composée immuable et un permis vérifié sur chaque send (`docs/mission03-architecture/DESIGN_OPTIONS.md:342-358`). | **PROPOSED —** aucun transport caller-visible ; permis one-use lié aux bytes exacts, route/version et dispatch. **UNKNOWN —** tous les sends bruts, redirects, retries SDK et sends UI ne sont pas démontrés couverts. **Futur oracle :** entrée directe sans permis, permis copié/stale, route inactive et digest différent donnent zéro appel fake. |
| **F2 — locks, stop, réservation, owner et Bridge** | **PARTIAL** | **VERIFIED —** le service actuel met à jour l’invocation puis sauvegarde l’enveloppe en finalisation (`backend/src/cti_app/application/investigation_service.py:1299-1319`) et en réconciliation (`backend/src/cti_app/application/investigation_service.py:1870-1910`). L’admission operator-led écrit aujourd’hui état → invocation → enveloppe (`backend/src/cti_app/application/operator_tool_invocation.py:1031-1117`). Le début d’un tour Bridge écrit contexte pending avant la réservation d’enveloppe (`backend/src/cti_app/application/investigation_service.py:626-659`). | **PROPOSED —** un ordre total unique est fixé en `5` et tous ces writers sont à réordonner. **UNKNOWN —** les transactions futures et la configuration d’isolation effective n’ont pas été exercées sur PostgreSQL. **Futur oracle :** deux sessions, barrières, absence de `40P01`/`55P03`, stop/RUNNING stale et settlement concurrent. |
| **F3 — claim, occurrence, normalizer, réemploi** | **PARTIAL** | **VERIFIED —** l’identité courante `tool_operation_key` inclut investigation, tool, schéma et paramètres, pas les bytes du fournisseur (`backend/src/cti_app/domain/investigation.py:400-427`). Le replay courant peut retourner aussi des résultats `PARTIAL` (`backend/src/cti_app/application/investigation_service.py:1951-2035`, relevé dans `docs/mission04-implementation/_work/S3D_C_VERSION_SETTLEMENT.md:22-28`). | **PROPOSED —** claim logique distinct du dispatch physique ; continuité stable au changement de version ; mismatch de normalizer/serializer = review sans send. Occurrences page/retry/correction appartiennent au workflow. Réemploi futur uniquement terminal, complet, même politique et avec ligne de lineage. **UNKNOWN —** projection d’intention stable par route ; aucun réemploi cross-invocation n’est autorisé ici. **Futur oracle :** bump sans second send, occurrence caller-forgée refusée, résultat ambigu non réutilisé. |
| **F4 — transitions, ack perdu, réponses partielles et settlement** | **PARTIAL** | **VERIFIED —** une exception externe sans preuve de non-exécution devient `NEEDS_REVIEW` (`backend/src/cti_app/application/investigation_service.py:1238-1267`) ; le settlement actuel garde une réservation incertaine et libère sur preuve framework (`backend/src/cti_app/application/investigation_service.py:2761-2832`). `sample_acquisition_attempts` est un journal de succès/erreur, pas une réservation de send (`backend/src/cti_app/infrastructure/database/models/production.py:469-526`). | **PROPOSED —** CAS append-only, ack perdu sans permis reconstruit, réponse tardive attachée au dispatch exact, aucun reçu fournisseur inventé. **UNKNOWN —** coûts par route et sémantique des quotas acquisition. **Futur oracle :** perte d’ack, tardif/partiel, settlement double et limites de `CONFIRMED_NOT_SENT`. |
| **N1 — inversion invocation/enveloppe et autres writers** | **CLOSED (contrat choisi)** | **VERIFIED —** admission, finalisation et réconciliation actuelles écrivent/lockent l’invocation avant l’enveloppe (`backend/src/cti_app/application/investigation_service.py:1031-1078,1110-1153,1299-1319,1870-1910`). L’état operator-led et le pending Bridge précèdent également l’enveloppe (`backend/src/cti_app/application/operator_tool_invocation.py:1031-1117`; `backend/src/cti_app/application/investigation_service.py:626-659`). | **PROPOSED —** appliquer l’ordre `5` à reservation, consume, finalisation, settlement, denial, reconcile, governance et recovery Bridge ; scinder les transactions qui gardent aujourd’hui un lock d’état en entrant dans l’admission. Le graphe choisi est acyclique par rang ; le code courant ne l’est pas encore. **Futur oracle :** ordre opposé échoue avant refactor ; après refactor, deux sessions terminent sans `40P01` ni `55P03`. |
| **N2 — union owner et binding owner/route** | **CLOSED (contrat choisi)** | **VERIFIED —** les owners actuels sont invocation, tour et run séparés, sans FK d’investigation sur tour/run (`backend/src/cti_app/infrastructure/database/models/investigation.py:59-101`; `backend/src/cti_app/infrastructure/database/models/model_execution.py:39-123,196-247`). `ToolDefinition` ne porte ni famille, route ni versions de codec, et le registre est modifiable (`backend/src/cti_app/application/investigation_tools.py:150-159,351-409`). | **PROPOSED —** union fermée `InvocationOwner | BridgeTurnOwner`, refs traitées comme locators, reload/validation durable de chaque champ en `6`. Pour advisor, le tour/run est owner technique et l’invocation d’origine est un binding de causalité. **Futur oracle :** chaque champ falsifié (owner, investigation, operation, family, reservation, route, normalizer, serializer, digest) refuse avant consume et fake à zéro. |
| **N3 — handoff ModelGateway** | **CLOSED (contrat choisi)** | **VERIFIED —** `add_turn` persiste `ModelRun RUNNING/NOT_SUBMITTED` et le tour avant `ModelGateway.execute` (`backend/src/cti_app/application/model_conversations.py:1282-1305`). Le gateway revendique aujourd’hui la soumission dans une transaction séparée avant adapter (`backend/src/cti_app/application/model_gateway.py:1051-1084,1139-1144`). `begin_submission_attempt` met l’état à `EXTERNAL_STATE_UNKNOWN` (`backend/src/cti_app/domain/model_runs.py:451-463`). | **PROPOSED —** le consume S3 est le seul propriétaire de la transition `NOT_SUBMITTED → SUBMISSION_IN_PROGRESS` et du compteur d’essai ; chaque appel physique gateway passe par un dispatch différent. **UNKNOWN —** clients injectés/SDK/UI peuvent avoir des retries ou redirects non démontrés. **Futur oracle :** retry terminal obtient un nouveau consume, ambiguïté ne relance rien, preflight réseau est soit dispatché soit supprimé. |
| **N4 — bump de normalizer et nouveau claim silencieux** | **CLOSED (politique fail-closed), binding route PARTIAL** | **VERIFIED —** la clé d’invocation courante versionne `schema_version`, mais ne lie pas les bytes émis (`backend/src/cti_app/domain/investigation.py:419-427`). R7 relève qu’inclure directement le normalizer dans l’identité et le sentinel d’occurrence peut créer un claim par défaut neuf (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:32-34`). | **PROPOSED —** `continuity_key` n’inclut pas la version de normalizer ; digest sémantique versionné et digest des bytes restent des preuves séparées. Tout mismatch retrouve le claim de continuité et ferme en review ; aucune alias ni occurrence automatique. **UNKNOWN —** la projection stable exacte est à définir pour chaque route ; route inactive tant qu’elle manque. **Futur oracle :** version v2 sur intention v1 ne crée ni dispatch consommé ni second egress. |
| **N5 — retry ModelGateway** | **CLOSED (contrat choisi), couverture physique PARTIAL** | **VERIFIED —** le gateway relance une défaillance Bridge terminale confirmée dans le même run (`backend/src/cti_app/application/model_gateway.py:1257-1264,1317-1343`). La branche stateless rend `conversation=None` (`backend/src/cti_app/application/bridge_investigator.py:764-780`), ce qui peut satisfaire son prédicat de retry (`backend/src/cti_app/application/model_gateway.py:1570-1584`). | **PROPOSED —** conserver le comportement fonctionnel uniquement quand l’échec antérieur est terminal et attesté ; le retry a nouvel attempt et nouveau consume. Timeout/perte d’ack/partial n’ouvre pas de retry. **UNKNOWN —** retries cachés du SDK, redirects et toutes les méthodes Bridge. **Futur oracle :** compter chaque send physique et rapprocher un permis/dispatch par send. |
| **N6 — source unique de classification et composition** | **CLOSED (contrat choisi)** | **VERIFIED —** les familles sont actuellement inférées de catégories/capacités différentes et les adapters sont inscrits conditionnellement (`backend/src/cti_app/application/investigation_tools.py:39-48,150-159`; `backend/src/cti_app/application/investigation_composition.py:1250-1299,1539-1562`). La composition passe ensuite le transport Bridge au service (`backend/src/cti_app/application/investigation_composition.py:1607-1625`). | **PROPOSED —** une seule `ExternalOperationSpec` versionnée classe operation/famille/provider/tool/schema/route. Le catalogue composé ne porte que codecs, endpoint et transport permit-aware ; catalogue immutable, route choisie par serveur, toutes les routes production deny-all. **UNKNOWN —** routes réellement composées en environnement ; fake-test seulement. **Futur oracle :** composition production sans route test, inactive/mismatch = zéro transport. |

## 3. État, owners et schéma visés

### 3.1 Tables existantes et limites vérifiées

| Table ou objet | État au point de base |
|---|---|
| `tool_invocations` | **VERIFIED —** existe, FK vers investigation, `operation_key`, `tool_name`, `schema_version`, paramètres, budgets, status et version/CHECK (`backend/src/cti_app/infrastructure/database/models/investigation.py:59-101`). Pas de route, famille, digest des bytes ou FK Bridge. |
| `investigation_envelopes` | **VERIFIED —** existe ; approval/disclosure/provider scope, version et `budget_snapshot` (`backend/src/cti_app/infrastructure/database/models/investigation.py:547-582`). C’est l’autorité proposée pour réservation/settlement opérateur de ce périmètre. |
| `investigation_working_states` | **VERIFIED —** existe avec version, turn index et stop reason contrôlés (`backend/src/cti_app/infrastructure/database/models/investigation.py:677-719`). |
| `analyst_investigations` | **VERIFIED —** porte aussi le budget de boucle acquisition ; `sample_acquisition.py` le débite avant l’appel download (`backend/src/cti_app/application/sample_acquisition.py:349-367`). Le verrou futur est placé après working state. |
| `investigation_budget_*` | **VERIFIED —** les modèles et repositories existent et le UOW les expose (`backend/src/cti_app/infrastructure/database/models/investigation.py:585-675`; `backend/src/cti_app/infrastructure/database/uow.py:366-368`). **VERIFIED —** les chemins S3 examinés n’en font pas l’autorité courante (`docs/mission04-implementation/_work/S3D_A_LOCKS_WRITERS.md:13-16`). Ne pas les ajouter comme second ledger budgétaire sans décision distincte. |
| `model_conversations`, `model_conversation_turns`, `model_runs` | **VERIFIED —** existent ; le tour FK vers conversation/run/parent, mais n’a pas investigation, réservation ou version propre ; le run a `submission_state`/`submission_attempt` sans version de ligne (`backend/src/cti_app/infrastructure/database/models/model_execution.py:39-123,143-247`). |
| `retrohunt_reservations` | **VERIFIED —** existe et réserve les slots d’opération avec version/uniqueness (`backend/src/cti_app/infrastructure/database/models/retrohunt.py:25-62`). Le slot n’est pas un reçu de coût fournisseur. |
| `sample_acquisition_attempts` | **VERIFIED —** lignes de résultat succès/erreur appendées après tentative ; la tentative locale peut aussi réutiliser un sample avant le download (`backend/src/cti_app/infrastructure/database/models/production.py:469-526`; `backend/src/cti_app/application/sample_acquisition.py:315-347,533-670`). Ce n’est pas un lock de réservation avant send. |
| Tables S3 | **VERIFIED —** aucun modèle/repository S3 n’existe au point de base ; elles sont seulement prévues par le design et L11 (`docs/mission04-implementation/_work/S3D_A_LOCKS_WRITERS.md:9-18`; `docs/mission03-architecture/IMPLEMENTATION_PLAN.md:266-296`). |

### 3.2 Tables à créer ou compléter plus tard — pseudo-DDL contractuel

**PROPOSED —** migration additive uniquement après le head vérifié dans une mission ultérieure ; aucune migration n’est préparée ou exécutée ici.

| Table / modification | Champs et invariants nécessaires |
|---|---|
| `external_effect_claims` — nouvelle | `claim_id`, `investigation_id`, `operation_id`, family/provider, `continuity_key`, `occurrence_id`, occurrence kind/workflow version/ordinal, parent claim, `normalizer_version`, `serializer_version`, `route_id`/catalog version, `semantic_digest`, `reuse_policy_version`, `claim_state`, `state_version`. FK investigation et parent claim ; CHECK sur `claim_state` (`OPEN`/`NEEDS_REVIEW`/`CLOSED`). `UNIQUE(investigation_id, continuity_key, occurrence_id)`. Le claim définit une intention logique, pas un droit de send. |
| `external_effect_dispatches` — nouvelle | `dispatch_id`, `claim_id`, investigation, owner kind et refs typées, `origin_invocation_id`, operation/family/provider, réservation key + amounts snapshot, route/catalog/normalizer/serializer versions, artifact ref, body SHA-256, request SHA-256, physical ordinal, ModelRun attempt attendu, owner state version, dispatch state/version, response ref/SHA, timestamps. FK claim, parent dispatch composite `(dispatch_id, investigation_id)`, owner composite FKs. `CHECK` owner-kind impose exactement une branche Invocation ou BridgeTurn; state/family enum CHECK; digest CHECK SHA-256 hex; `UNIQUE(claim_id)` (un claim/occurrence autorisé donne au plus un dispatch); partial `UNIQUE(model_run_id, submission_attempt)`; CAS `WHERE state=:expected AND state_version=:v`. |
| `model_conversation_turns` — colonnes additives | `investigation_id`, `reservation_operation_key`, nullable `origin_invocation_id`, `state_version`. FKs restrict vers investigation et invocation composée ; uniques exacts pour FK composite `(id, model_run_id, investigation_id, reservation_operation_key[, origin_invocation_id])`. |
| `model_runs` — colonne additive | `state_version` non nulle, incrémentée par CAS avec `status/submission_state/submission_attempt`. L’ID seul n’est pas un CAS. Le tour/run gardent leurs FKs actuelles. |
| `external_effect_events` — nouvelle | Événement append-only avec `(dispatch_id, event_no)` unique, from/to state, actor kind, reason code, date et détails sûrs. Les transitions ne peuvent être écrites que par la fonction/repository S3 ; aucune édition d’événement. |
| `external_effect_measurements` — nouvelle | Une mesure applicative par `(dispatch_id, dimension)` ; montant non négatif, basis/version de policy. La mesure est une décision de comptabilité AutoWork, jamais un reçu fournisseur. |
| `external_effect_settlements` — nouvelle | Une ligne CAS par `(investigation_id, reservation_operation_key)`, version et état/montants consolidés. Insertion/update dans la même transaction que l’enveloppe JSON ; elle est un journal d’idempotence, pas une seconde autorité de budget. |
| `external_effect_uses` + refs — nouvelles | Un claimant typé (invocation ou Bridge turn), source dispatch et owner source, policy version, réponse/digest source, delta budget avec CHECK `{}`. FK dispatch/owners ; unicité claimant/source ; refs de preuve typées et versionnées. Pas de FK polymorphe inventée vers observations/artifacts : les relations doivent être matérialisées par tables de refs typées. |
| `sample_acquisition_reservations` — nouvelle | Claim familial pré-send avec investigation, hash normalisé, occurrence, dispatch, status/version et unicité par opération/hachage/occurrence. `sample_acquisition_attempts` reste le résultat final et ne sert pas de réservation. |
| `retrohunt_reservations` — extension ciblée si retenue | Lier le slot/opération à son dispatch submit et CAS sa transition familiale dans l’ordre global. Les polls/status/results restent chacun un dispatch distinct ; aucune unité de coût n’est présumée. |

**PROPOSED —** owner Invocation lie par FK composite dispatch → invocation `(id, investigation_id, operation_key, tool_name, schema_version)`. Owner Bridge lie dispatch → turn `(turn_id, model_run_id, investigation_id, reservation_operation_key, origin_invocation_id)` ; la branche advisor exige une invocation d’origine, la branche Bridge investigation sans invocation l’exclut. `CHECK` ferme la somme d’owners et les colonnes non applicables restent `NULL`. La réservation enveloppe est stockée en JSON existant : elle ne peut pas être une FK SQL ; sa key, son état `RESERVED` et ses amounts sont revalidés sous le lock d’enveloppe. Cette limite est explicite, pas remplacée par les tables `investigation_budget_*`.

### 3.3 États dispatch et règles de transition

| Transition | Condition requise |
|---|---|
| `PREPARED → CONSUMED` | Une seule CAS réussie après validation courante envelope/stop/owner/route/reservation/claim/digest. Émission de permis seulement après commit confirmé. |
| `PREPARED → DENIED` | Refus fail-closed avant consume (policy, stop, route inactive/stale, mismatch, owner stale). Zéro permis ; libérer uniquement cette réservation prouvée non consommée. |
| `PREPARED → ABANDONED` | Annulation avant consume, événement durable, zéro send ; libérer uniquement la réservation exacte prouvée non consommée. |
| `CONSUMED → SUCCEEDED` | Réponse complète liée au dispatch, route, owner et request digest exacts. `SUCCEEDED` signifie résultat complet au bord transport, y compris une réponse métier négative ; il ne signifie pas succès fournisseur/facturation. |
| `CONSUMED → NEEDS_REVIEW` | Ack perdu, timeout/socket/reset, exception après entrée transport, owner/process disparu, bytes/réponse partiels ou résultat impossible à attribuer. Garder la réservation ; aucun retry ou réemploi automatique. |
| `CONSUMED → CONFIRMED_NOT_SENT` | Preuve positive du framework que le transport physique n’a jamais été appelé et qu’aucun retry/redirect caché n’a pu envoyer. Libération possible de la réservation de ce dispatch seulement. |
| `NEEDS_REVIEW → RECONCILED_EXECUTED` | Décision humaine auditée avec preuves attachées au digest exact. Settlement conservateur ; réutilisation uniquement si le résultat est ensuite prouvé complet et autorisé. |
| `NEEDS_REVIEW → RECONCILED_NOT_EXECUTED` | Décision humaine auditée, distincte d’une preuve framework. Ne rembourse pas automatiquement, n’autorise pas un retry et ne rend pas un résultat réutilisable. |
| `NEEDS_REVIEW → ABANDONED` | Fermeture humaine auditée ; aucune preuve d’absence, aucun remboursement automatique, aucune réutilisation. |

`SUCCEEDED`, `CONFIRMED_NOT_SENT`, `DENIED`, `ABANDONED` et `RECONCILED_*` sont terminaux et immuables. Une réponse tardive peut compléter une ligne `NEEDS_REVIEW` une fois par CAS du dispatch exact ; elle ne peut ni créer un permis ni déclencher un second send. Une donnée partielle reste un artefact de revue.

**PROPOSED — cycle du claim :** un claim `OPEN` représente une seule occurrence autorisée et peut avoir au plus un dispatch, imposé par `UNIQUE(claim_id)`. Un conflit de version/digest le fait passer en `NEEDS_REVIEW` sans dispatch ; une issue terminale du dispatch le ferme ; une ambiguïté le met en revue. Aucun claim fermé ou en revue n’est rouvert. Retry, page, poll ou correction autorisé crée un claim d’occurrence enfant distinct et un nouveau consume.

## 4. Normalizer, occurrences et réemploi

**PROPOSED —** séparer trois identités :

1. `continuity_key` : projection d’intention stable pour la route, sans version de normalizer ; elle arbitre le claim par défaut.
2. `semantic_digest` : projection et règles de normalisation versionnées, pour prouver quelle sémantique a été préparée.
3. `request_sha256`/`body_sha256` : méthode, cible/headers sûrs et bytes exacts sérialisés une fois ; le transport reçoit ces mêmes bytes.

Un bump normalizer/serializer ne produit ni alias ni nouvel effet par défaut. Si la même `continuity_key` existe sous une version différente, le coordinateur CAS le claim à `NEEDS_REVIEW` et journalise l’événement de version, sans `external_effect_use`, nouveau dispatch ou permis. Il faut une décision de compatibilité revue et un workflow autorisé pour créer une occurrence distincte ; un UUID fourni par le caller ne constitue pas cette autorisation.

**UNKNOWN —** les champs qui forment la projection d’intention stable dépendent de chaque route. À défaut de golden vectors et de définition versionnée de l’équivalence, la route reste inactive. Les anciennes `normalized_parameters`, keys Retrohunt ou clés Bridge ne sont pas backfillées en claims ou lineage. La clé courante inclut déjà `schema_version` et les paramètres, pas les bytes (`backend/src/cti_app/domain/investigation.py:400-427`).

**PROPOSED — occurrences autorisées par workflow seulement :** page suivante après curseur de page précédente complet ; poll/status après référence de run complète ; retry après `CONFIRMED_NOT_SENT` ou erreur fournisseur terminale explicitement qualifiée par policy ; correction avec nouveau contenu/hash et parent ; répétition seulement si le workflow nommé le permet. Chaque occurrence persiste parent, étape, ordinal stable, actor/policy et version d’état. `NEEDS_REVIEW`, timeout, partial, ack perdu ou `RECONCILED_NOT_EXECUTED` ne créent pas automatiquement une occurrence.

**PROPOSED — réemploi futur borné :** source dispatch terminal complet (`SUCCEEDED`, ou absence `EMPTY` explicitement qualifiée), même route/version/policy et même investigation/scope autorisé ; références source complètes ; ligne `external_effect_uses` avec source dispatch/owner, claimant owner, refs observation/artifact/sample, policy version et delta `{}`. Un résultat `PARTIAL`, `UNAVAILABLE`, refusé, ambigu ou seulement consommé est non réutilisable. **Ce document n’autorise aucun réemploi cross-invocation.** Le chemin local Sample déjà vérifié est décrit en `8` ; une réutilisation d’effet ou de résultat entre invocations nécessite un gate et une autorisation ultérieurs. Aucun deuxième egress n’est déclenché silencieusement.

## 5. Ordre total des locks et transactions

### 5.1 Ordre choisi

**PROPOSED — tout UOW qui touche plusieurs rangs acquiert les lignes dans cet ordre total ; les rangs inutilisés sont sautés :**

1. `investigation_envelopes` — approval, disclosure, provider scope, budget snapshot, version.
2. `investigation_working_states` — version/checkpoint/stop reason si le stop gouverne le send.
3. `analyst_investigations` — seulement quand la transaction règle loop-budget ou l’agrégat de fin d’investigation.
4. `tool_invocations` — owner invocation ou invocation advisor d’origine.
5. `external_effect_claims`.
6. `external_effect_dispatches`.
7. Réservation/claim familial : `retrohunt_reservations` ou future `sample_acquisition_reservations`.
8. `model_conversations`.
9. `model_conversation_turns`.
10. `model_runs`.
11. Insertions append-only : event, mesure, settlement, décision auditée.

Les lectures route/catalogue se font sur l’objet immutable de composition et ne prennent pas de lock DB. Tout owner/version est rechargé sous ses locks. Toute mise à jour incrémente la version attendue par CAS. Un insert de nouveaux enfants ModelRun/turn se fait sous lock conversation ; l’ordre ci-dessus gouverne les lignes préexistantes verrouillées.

**Résolution du conflit A/B/C — budgets :** S3 utilise l’`investigation_envelopes.budget_snapshot` comme autorité de réservation/settlement opérateur, car c’est ce que les chemins actuels lisent et sauvegardent (`backend/src/cti_app/application/investigation_service.py:1110-1153,2761-2832`). `investigation_budget_*` est un jeu ORM distinct, exposé au UOW mais non appelé par ces chemins (`backend/src/cti_app/infrastructure/database/uow.py:366-368`; `docs/mission04-implementation/_work/S3D_A_LOCKS_WRITERS.md:13-16`). La table settlement proposée assure l’unicité/idempotence en transaction ; elle ne devient pas une deuxième source de solde.

**Résolution du conflit A/B/C — rang budget relationnel :** aucun rang `investigation_budget_reservations/amounts` n’est ajouté au chemin S3 tant qu’aucune décision ne les adopte comme autorité unique. Si une mission ultérieure adopte ces tables, elle doit spécifier un nouvel ordre versionné et retirer la mise à jour concurrente du JSON ; elle ne peut pas ajouter une réservation parallèle.

**Résolution explicite des divergences de contrat :** A laisse le ledger relationnel conditionnel, B propose de verrouiller ses réservations/montants et place le dispatch avant le rang familial, tandis que C privilégie une clé de continuité stable pour empêcher un claim neuf lors d’un bump de normalizer. Ici, l’autorité actuelle du JSON enveloppe est retenue ; le rang relationnel de B est donc omis. L’ordre total de `5.1` place le claim logique avant le dispatch, puis le dispatch avant la réservation familiale, ce qui réunit les chemins invocation et Bridge. Le `effect_key` versionné proposé par B n’est pas retenu comme identité unique : `continuity_key` avec `occurrence_id` arbitre le claim logique, et versions/digests restent des attributs contrôlés qui font échouer en revue. Cela suit le risque N4 documenté par R7 et évite qu’une version crée silencieusement un second envoi. La position consume-first est choisie comme proposition technique, mais reste une décision humaine bloquante : aucun compromis n’est pris sur un POST qui commencerait après révocation.

### 5.2 Writers présents, inversions et refactors nécessaires

| Writer actuel vérifié | Ordre/limite aujourd’hui | Refactor requis avant le gate |
|---|---|---|
| Admission opérateur | Invocation créée/transitionnée puis réservation JSON d’enveloppe commitée (`backend/src/cti_app/application/investigation_service.py:1031-1078,1110-1153`). | Envelope → working state → investigation si utile → invocation → claim/dispatch. |
| Operator-led / stop | Le marqueur working-state précède `execute_tool_call`, qui écrit invocation puis envelope (`backend/src/cti_app/application/operator_tool_invocation.py:1031-1117`). Le stop seul sauvegarde l’état (`backend/src/cti_app/application/investigation_service.py:499-521`). | Commiter le marqueur/stop sans garder ce lock ; transaction de réservation séparée prend ensuite l’ordre global. Stop seul ne demande jamais un rang supérieur. |
| Finalisation outil | Update invocation puis `_complete_tool_reservation` sauvegarde l’enveloppe dans le même UOW (`backend/src/cti_app/application/investigation_service.py:1299-1319,2761-2832`). | Envelope/stop/investigation éventuels d’abord, puis invocation/claim/dispatch/famille ; résultat et settlement atomiques. |
| Réconciliation invocation | Lit invocation, règle l’enveloppe, update invocation puis save envelope (`backend/src/cti_app/application/investigation_service.py:1800-1810,1870-1910`). | Recharger et verrouiller envelope puis owner ; CAS dispatch, owner, settlement et audit ensemble. |
| Governance | L’extension suit envelope puis investigation ; la révocation actuelle écrit investigation puis envelope (`backend/src/cti_app/application/investigation_governance.py:327-349,441-487`, relevé S3D-A). | Harmoniser toutes opérations multi-lignes en envelope → investigation ; révocation ne verrouille jamais envelope après investigation. |
| Pending Bridge | Le contexte pending précède la réservation enveloppe (`backend/src/cti_app/application/investigation_service.py:626-659`). | Enveloppe puis working state, ou deux commits sans lock conservé ; tour/run ensuite sous rangs Bridge. |
| ModelConversation / Gateway | `add_turn` persiste run+turn avant Gateway (`backend/src/cti_app/application/model_conversations.py:1282-1305`). Recovery/abandon contient run→conversation (`backend/src/cti_app/application/model_conversations.py:770-804,1007-1051`), alors que finalisation prend conversation→turn et certaines erreurs ajoutent ensuite le run (`backend/src/cti_app/application/model_conversations.py:1330-1366,1416-1449`). Le Gateway écrit le run avant adapter dans son propre UOW (`backend/src/cti_app/application/model_gateway.py:1139-1172`). | Uniformiser conversation→turn→run après rangs S3 ; fusionner writes dispatch/run/turn/finalisation/settlement là où la cohérence le requiert. Gateway ne claim ni ne finalise indépendamment du dispatch. |
| Retrohunt | Slot `RESERVED→SUBMITTING` CAS et commit avant provider, résultat dans un autre UOW (`backend/src/cti_app/application/retrohunt.py:152-239,402-451`, relevé S3D-A). | Consommer dispatch puis verrouiller/CAS le slot dans la même transaction ; le send suit le commit. |
| Acquisition | Loop budget commit avant download ; attempt succès/erreur est écrit après (`backend/src/cti_app/application/sample_acquisition.py:349-377,407-429,533-670`). Le wrapper peut découvrir un sample local au second contrôle et renvoyer encore le coût plein (`backend/src/cti_app/application/investigation_acquisition.py:343-388,515-537`, relevé S3D-C). | Décider le réemploi avant consume ; vraie réservation familiale CAS avant bytes ; loop budget + dispatch sous l’ordre ci-dessus ; `attempts` reste le résultat. |

**INFERENCE — absence d’inversion future :** si chaque writer ci-dessus ne verrouille que des rangs croissants, aucun cycle d’attente ne peut contenir une inversion invocation↔envelope, state↔envelope, ou ModelRun↔conversation. Cela prouve l’aciclicité du contrat, pas celle du code actuel. Les anciens workers doivent être drainés/fencés avant toute route, et les recovery/reconcile/abandon suivent le même ordre.

**PROPOSED — isolation/UOW :** le moteur ne fixe pas `isolation_level` (`backend/src/cti_app/infrastructure/database/session.py:9-14`). Un UOW crée une AsyncSession, committe explicitement et rollback sur exception ou sortie sans commit (`backend/src/cti_app/infrastructure/database/uow.py:296-303,410-430`). Les repositories utilisent `FOR UPDATE` et CAS versionnés pour envelope/working state (`backend/src/cti_app/infrastructure/database/repositories/investigation.py:1368-1411,1426-1467`). Les transactions S3 demandent explicitement PostgreSQL `READ COMMITTED` avant leur première requête, gardent lock-timeout borné et utilisent CAS/uniques en plus des locks. Le niveau effectif serveur/déploiement reste **UNKNOWN** ; une mission ultérieure doit le vérifier et tester avec deux sessions réelles.

**PROPOSED — aucun lock pendant HTTP/transport :** consume committe `CONSUMED` puis émet le permis ; le code ferme l’UOW avant appeler le transport. Réponse et settlement ouvrent un UOW neuf. Le point linéaire choisi est le commit consume : si révocation/stop committe avant, consume relit l’état récent et refuse ; si consume committe avant, le dispatch a le droit one-use même si le réseau commence après le commit de révocation. Un permis consommé n’est jamais révoqué par reconstruction.

**Décision humaine requise — sémantique consume-first :** confirmer que le commit consume peut gagner sur une révocation qui committe avant le début effectif du POST. Si le produit exige «aucun début réseau après commit révocation», ce design sans lock HTTP ne le garantit pas ; garder S3 bloqué et définir un autre fence. L’oracle `10.2` matérialise cette limite.

### 5.3 Pseudo-transactions SQL

Les tables/champs S3 ci-dessous sont proposés, non déployés. `BEGIN ISOLATION LEVEL READ COMMITTED` est l’objectif contractuel, pas une assertion sur la configuration en place.

**A. Réservation opérateur + préparation `PREPARED` — aucun permis**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_envelopes
 WHERE investigation_id=:i FOR UPDATE;
SELECT * FROM investigation_working_states
 WHERE investigation_id=:i FOR UPDATE;
-- Acquisition seulement : SELECT analyst_investigations FOR UPDATE si loop budget débité.
-- Recharger provider/G2/TLP/DNT/LLM allowance, capability, stop et version.
-- Verrouiller l’invocation existante après envelope/state ; sinon l’insérer.
-- Réserver operation_key dans envelope.budget_snapshot ; vérifier amounts exacts.
UPDATE investigation_envelopes
   SET budget_snapshot=:snapshot_with_operation_reserved, version=version+1
 WHERE investigation_id=:i AND version=:envelope_v;
-- Le snapshot écrit contient la key exacte, status=RESERVED et les amounts ;
-- exiger rowcount=1 ou rollback avant de créer un dispatch.
-- Préparer/archive les bytes immuables avant persist dispatch ; comparer les digests.
INSERT INTO external_effect_claims (...)
ON CONFLICT (investigation_id, continuity_key, occurrence_id) DO NOTHING;
SELECT * FROM external_effect_claims
 WHERE investigation_id=:i AND continuity_key=:continuity AND occurrence_id=:occurrence
 FOR UPDATE;
-- Une clé déjà présente retourne l’état/dispatch existant : jamais de second dispatch.
-- Comparer versions/digests/route. Mismatch : CAS claim→NEEDS_REVIEW, aucun dispatch.
-- Occurrence nouvelle seulement si un workflow autorisé a persisté parent/ordinal/étape.
INSERT INTO external_effect_dispatches (..., state, state_version)
VALUES (..., 'PREPARED', 1);
INSERT INTO external_effect_events (..., from_state, to_state)
VALUES (..., NULL, 'PREPARED');
COMMIT;
-- Pas de permit. Une erreur DB ne peut appeler un transport.
~~~

L’archive d’octets doit exister et son SHA doit être vérifiable avant consume. L’upload d’artefact et SQL ne sont pas supposés atomiques ; un échec entre eux laisse un artefact orphelin à traiter selon une politique d’artefact, pas un droit d’envoi.

**B. Consume — opération linéaire de décision**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_envelopes WHERE investigation_id=:i FOR UPDATE;
SELECT * FROM investigation_working_states WHERE investigation_id=:i FOR UPDATE;
-- Si acquisition : SELECT analyst_investigations FOR UPDATE et valider les quotas.
-- Owner opérateur : invocation FOR UPDATE ; Bridge advisor : origin invocation FOR UPDATE.
SELECT * FROM external_effect_claims WHERE claim_id=:c FOR UPDATE;
SELECT * FROM external_effect_dispatches WHERE dispatch_id=:d FOR UPDATE;
-- Après dispatch, verrouiller la ligne de réservation familiale exacte.
-- Owner Bridge : conversation → turn → ModelRun, tous reliés par IDs/FKs exacts.
-- Revalider envelope approval/disclosure/provider/budget/stop, owner RUNNING/version,
-- reservation RESERVED/key/amounts, route active/catalog version, operation/family,
-- normalizer/serializer, claim occurrence, blob SHA, body/request SHA.
UPDATE external_effect_dispatches
   SET state='CONSUMED', state_version=state_version+1, consumed_at=now()
 WHERE dispatch_id=:d AND state='PREPARED' AND state_version=:expected
   AND owner_state_version=:owner_v AND request_sha256=:request_sha;
-- Exiger rowcount=1 ; Bridge : dans le même UOW, CAS ModelRun
-- RUNNING/NOT_SUBMITTED -> RUNNING/SUBMISSION_IN_PROGRESS,
-- incrémenter submission_attempt et écrire bridge_request_id de cet attempt.
-- Acquisition : réserver/débiter les quotas distincts sous leur clé propre.
-- CAS dans envelope.budget_snapshot : la même opération passe RESERVED→CONSUMED
-- sans changer le hold maximal ; CAS loop-budget si acquisition et montant dû.
-- Un conflit de version ou de réservation annule toute la transaction.
INSERT INTO external_effect_events (..., from_state, to_state)
VALUES (..., 'PREPARED', 'CONSUMED');
COMMIT;
-- Remettre le SendPermit opaque seulement après ack de commit.
-- Aucun lock ne traverse l’appel HTTP.
~~~

Si l’ack du commit est perdu, le caller relit via un UOW neuf. Il n’émet jamais un permis d’une ligne déjà `CONSUMED`. Si l’issue de commit/dispatch n’est pas démontrable, `NEEDS_REVIEW`, zéro second send.

**C. Finalisation d’une réponse complète + settlement atomiques**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- Reprendre exactement les rangs B : envelope → working state → investigation éventuelle
-- → invocation/claim → dispatch → famille → conversation → turn → run.
SELECT ... FOR UPDATE; -- lignes existantes dans cet ordre
-- Revalider dispatch CONSUMED, owner IDs/versions, route, request/body SHA et response.
UPDATE external_effect_dispatches
   SET state='SUCCEEDED', state_version=state_version+1,
       response_ref=:immutable_ref, response_sha256=:response_sha
 WHERE dispatch_id=:d AND state='CONSUMED' AND state_version=:dispatch_v
   AND request_sha256=:request_sha;
-- CAS invocation/result ou turn/run selon owner ; incrémenter leurs state_version.
-- Mesure déterministe de route, jamais un reçu provider ; même transaction:
INSERT INTO external_effect_measurements(dispatch_id, dimension, amount, basis)
VALUES (:d, :dimension, :amount, :route_policy_version);
-- CAS external_effect_settlements par reservation_operation_key.
INSERT INTO external_effect_settlements
       (investigation_id, reservation_operation_key, version, settlement_state, settled_amounts)
VALUES (:i, :key, 1, 'OPEN', :initial_totals)
ON CONFLICT (investigation_id, reservation_operation_key) DO NOTHING;
SELECT * FROM external_effect_settlements
 WHERE investigation_id=:i AND reservation_operation_key=:key FOR UPDATE;
UPDATE external_effect_settlements
   SET settled_amounts=:new_totals, version=version+1
 WHERE investigation_id=:i AND reservation_operation_key=:key AND version=:settlement_v;
UPDATE investigation_envelopes
   SET budget_snapshot=:new_snapshot, version=version+1
 WHERE investigation_id=:i AND version=:envelope_v;
-- Exiger rowcount=1 pour chaque CAS ; amount <= montant réservé.
-- Insert les événements CONSUMED/SUCCEEDED append-only et la décision/audit requise ; PREPARED a été enregistré lors de la préparation.
COMMIT;
~~~

Un résultat tardif ne peut utiliser C qu’une fois, pour le dispatch exact. Si response est partial/incomplète, la même transaction passe dispatch/owner en `NEEDS_REVIEW`, conserve la réservation et archive la réponse partielle sans measurement terminale ni réutilisation.

**D. Settlement idempotent/reprise**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- Mêmes rangs et mêmes owner locks que C.
INSERT INTO external_effect_settlements
       (investigation_id, reservation_operation_key, version, settlement_state, settled_amounts)
VALUES (:i, :key, 1, 'OPEN', :initial_totals)
ON CONFLICT (investigation_id, reservation_operation_key) DO NOTHING;
SELECT * FROM external_effect_settlements
 WHERE investigation_id=:i AND reservation_operation_key=:key FOR UPDATE;
-- Si dispatch measurement et amount déjà identiques : retourner idempotent.
-- Si amount différent : conflit de version, jamais écrasement.
-- Sinon insérer la mesure avec UNIQUE(dispatch_id, dimension), puis:
UPDATE external_effect_settlements
   SET settled_amounts=:new_totals, settlement_state=:new_state, version=version+1
 WHERE investigation_id=:i AND reservation_operation_key=:key AND version=:settlement_v;
UPDATE investigation_envelopes
   SET budget_snapshot=:snapshot_released_or_settled, version=version+1
 WHERE investigation_id=:i AND version=:envelope_v;
-- Exiger rowcount=1 aux deux CAS ; le montant final ne dépasse pas le hold.
-- PK(investigation_id, reservation_operation_key) empêche un second settlement.
-- Ambigu = garder la réservation, NEEDS_REVIEW ; absence de reçu ne vaut pas zéro.
COMMIT;
~~~

**E. Denial avant consume**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- envelope → working state → investigation éventuelle → owner → claim → dispatch
-- → famille → conversation/turn/run si Bridge.
-- Recharger et prouver que dispatch est encore PREPARED et jamais CONSUMED.
UPDATE external_effect_dispatches
 SET state='DENIED', state_version=state_version+1
 WHERE dispatch_id=:d AND state='PREPARED' AND state_version=:v;
-- Owner stale/mismatch n’autorise jamais une mise à jour d’un owner différent.
-- Libérer le montant exact réservé seulement après rowcount=1 pré-consume.
UPDATE investigation_envelopes
   SET budget_snapshot=:snapshot_released_exact_hold, version=version+1
 WHERE investigation_id=:i AND version=:envelope_v;
-- Exiger rowcount=1 ; ne toucher ni aux autres reservations ni aux usages consommés.
INSERT INTO external_effect_events (..., from_state, to_state)
VALUES (..., 'PREPARED', 'DENIED');
COMMIT;
-- Pas de permit, pas d’appel.
~~~

**F. Réconciliation humaine**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- envelope → working state → investigation éventuelle → invocation/claim/dispatch
-- → famille → conversation → turn → run.
-- Exiger NEEDS_REVIEW + dispatch/owner/digest/version exacts + actor/reason.
UPDATE external_effect_dispatches
 SET state=:RECONCILED_EXECUTED_OR_NOT_EXECUTED_OR_ABANDONED,
     state_version=state_version+1
 WHERE dispatch_id=:d AND state='NEEDS_REVIEW' AND state_version=:v
   AND request_sha256=:request_sha;
-- Résoudre owner par CAS et ajouter la décision auditée dans le même UOW.
-- EXECUTED_WITH_RESULT n’accepte que refs complètes/digest exact.
-- NOT_EXECUTED humain n’est pas CONFIRMED_NOT_SENT : pas de remboursement automatique.
INSERT INTO external_effect_events (...);
COMMIT;
~~~

**G. Stop working-state**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT * FROM investigation_working_states WHERE investigation_id=:i FOR UPDATE;
UPDATE investigation_working_states
 SET stop_reason=:reason, version=version+1, updated_at=now()
 WHERE investigation_id=:i AND version=:expected AND stop_reason IS NULL;
-- Si la même transaction modifie analyst_investigations, la locker ensuite.
COMMIT;
-- Le stop n’acquiert ensuite ni envelope, invocation, dispatch, famille ou Bridge.
~~~

**H. Réservation familiale**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- Préparation : envelope → state → investigation si loop-budget → owner → claim → dispatch.
-- Retrohunt : insérer le slot prévu ou laisser l’unicité arbitrer.
INSERT INTO retrohunt_reservations
       (id, subject_id, investigation_id, slot_number, operation_key,
        rule_version_id, rule_source_sha256, corpus_id, corpus_sha256,
        parameters, state, version, created_at, updated_at)
VALUES (:id, :subject, :i, :slot_number, :operation_key,
        :rule_version, :rule_sha, :corpus, :corpus_sha, :parameters,
        'reserved', 1, now(), now())
ON CONFLICT (subject_id, operation_key) DO NOTHING;
SELECT * FROM retrohunt_reservations
 WHERE subject_id=:subject AND operation_key=:operation_key FOR UPDATE;
-- Conflit de slot occupé par une autre opération = refus, jamais réemploi.
UPDATE retrohunt_reservations
   SET version=version+1, external_effect_dispatch_id=:d
 WHERE id=:slot AND state='reserved' AND external_effect_dispatch_id IS NULL
   AND version=:slot_v;
-- Lier seulement un slot encore réservé sans dispatch ; ne jamais réouvrir
-- submitting/submitted/completed/failed/reconciliation_required.
-- Acquisition : matérialiser le claim par INSERT unique avant le download :
INSERT INTO sample_acquisition_reservations
       (investigation_id, operation_key, normalized_hash, occurrence_id, dispatch_id, status, version)
VALUES (:i, :key, :hash, :occurrence, :d, 'RESERVED', 1)
ON CONFLICT (investigation_id, operation_key, normalized_hash, occurrence_id) DO NOTHING;
SELECT * FROM sample_acquisition_reservations
 WHERE investigation_id=:i AND operation_key=:key
   AND normalized_hash=:hash AND occurrence_id=:occurrence FOR UPDATE;
-- Recharger/verrouiller la ligne gagnante ; ne jamais utiliser
-- sample_acquisition_attempts comme ligne pending.
COMMIT;
~~~

La consommation familiale est la fin de la transaction B, après ses verrous, validations et CAS dispatch. Ces statements s’exécutent dans le même UOW avant son commit :

~~~sql
-- Dans le pseudo-SQL B, après le CAS PREPARED→CONSUMED du dispatch :
UPDATE retrohunt_reservations
   SET state='submitting', version=version+1
 WHERE id=:slot AND state='reserved' AND version=:slot_v;
UPDATE sample_acquisition_reservations
   SET status='IN_FLIGHT', version=version+1
 WHERE id=:sample_reservation AND status='RESERVED' AND version=:sample_v;
-- Exiger le CAS de la branche familiale concernée ; puis COMMIT B avant provider.
~~~

**I. Bridge turn/run — aucun appel dans la transaction de préparation**

~~~sql
BEGIN ISOLATION LEVEL READ COMMITTED;
-- envelope → working state → investigation → origin invocation (advisor) → conversation.
SELECT * FROM model_conversations WHERE id=:conversation FOR UPDATE;
-- Réserver une fois BRIDGE_TURNS=1 et les coûts logiques du tour dans budget_snapshot.
-- Ajouter ModelRun RUNNING/NOT_SUBMITTED et ModelConversationTurn RUNNING ;
-- turn porte investigation_id, reservation_operation_key, origin_invocation_id,
-- input_sha256 et state_version ; conversation devient BUSY.
COMMIT;

-- Le coordinateur prépare ensuite claim+dispatch PREPARED, puis consomme avec B.
-- B verrouille conversation → turn → run ; exige IDs liés, owner/route/hash exacts,
-- turn RUNNING, run RUNNING/NOT_SUBMITTED et attempt attendu.
-- Seul B change run à SUBMISSION_IN_PROGRESS et incrémente l’attempt.
-- Gateway ne fait plus un begin-submission séparé.
~~~

## 6. Owner/route fail-closed

**PROPOSED — union propriétaire fermée :**

- `InvocationOwner(invocation_id)` pour VT/Shodan/Retrohunt/acquisition directs ;
- `BridgeTurnOwner(turn_id, model_run_id)` pour toute soumission physique Bridge/ModelGateway liée à une investigation.

`owner` dans `PreparedExternalRequest` est un locator, jamais une capacité. Pour `advisor.consult`, owner technique = Bridge turn/run ; `origin_invocation_id` relie explicitement l’invocation et reste obligatoire. N’inférer aucun lien depuis correlation ID, idempotency key, hash ou chaîne de paramètres.

**Binding durable obligatoire avant permit :** investigation ; owner kind/ID/version/status ; tool/schema si invocation ; conversation/turn/run et input hash si Bridge ; invocation advisor d’origine ; operation/famille/provider dérivés du registre canonique ; réservation operation key + amounts/status ; claim/occurrence/parent/ordinal ; `route_id` et version de catalogue ; normalizer/serializer versions ; request artifact ref et SHA des bytes ; request SHA ; Bridge submission attempt ; digest du corps qui sera donné au socket.

**PROPOSED — source de classification unique :** un registre `ExternalOperationSpec` versionné lie operation, family, provider, tool/schema éventuels et route. La composition valide unicité puis fige le catalogue `route_id → normalizer + serializer + endpoint/transport permit-aware`. Le catalogue transport ne porte pas une seconde règle tool→family/provider. Aucune méthode publique ne reçoit un transport, un booléen de permission, des credentials ou des bytes arbitraires.

Mismatch de route/owner, owner stale, route inactive, catalogue version inconnu, bytes/digest différents, réservation modifiée ou appel direct raw sans permis : refus typé avant transport et aucun permit. Le dernier transport est privé à la composition et consomme le nonce one-use au point des bytes exacts. Redirect et retry client/SDK sont désactivés ou exposés comme nouveaux dispatchs.

**UNKNOWN — portée ModelGateway :** le gateway est aussi appelé par extraction/discovery/production sans `ModelConversationTurn` ou `ToolInvocation` (`backend/src/cti_app/application/production_extraction.py:1403-1405`; `backend/src/cti_app/application/discovery/service.py:126`; relevé `docs/mission04-implementation/_work/S3D_B_OWNER_ROUTE_GATEWAY.md:77-80`). Ces sends n’entrent pas dans l’union ; leurs routes restent deny-all jusqu’à un owner explicite approuvé. Preflight avec I/O, polling, fermeture de session Bridge, redirects et runtime Bridge UI doivent être inventoriés comme opérations physiques séparées avant toute famille activée.

## 7. Handoff ModelGateway et Bridge

| Moment | Owner de la mutation | État durable visé | Règle |
|---|---|---|---|
| Création turn | `ModelConversationService`, transaction de préparation | Turn `RUNNING`; run `RUNNING/NOT_SUBMITTED`; conversation `BUSY`; aucun dispatch consommé | La ligne est durable avant le Gateway. Rejouer la même idempotency key rend le même tour. |
| Préparation | Coordinateur | Dispatch `PREPARED` lié au turn/run, input hash, owner version, attempt prévu, route/versions/bytes | Aucun permis ni send. |
| Consume | Coordinateur, UOW ordonné `5` | Dispatch `CONSUMED`; run `RUNNING/SUBMISSION_IN_PROGRESS`; `submission_attempt += 1`; turn reste `RUNNING` | CAS atomique dispatch↔run. C’est l’unique transition qui ouvre l’envoi. |
| Envoi | Transport privé permit-aware | Aucun lock SQL | Un permis = un send physique ; bytes exacts. |
| Réponse complète | Coordinateur + owner finalizer | Dispatch `SUCCEEDED`; run `RESULT_OBTAINED` puis terminal; turn `SUCCEEDED`; settlement unique | La transition et les refs exactes sont atomiques en SQL. |
| Échec certain sans send | Framework d’envoi + coordinateur | Dispatch `CONFIRMED_NOT_SENT`; run peut redevenir `NOT_SUBMITTED` après CAS | Nouvel attempt = nouveau dispatch/consume ; preuve attachée. |
| Retry Bridge terminal certain | Gateway orchestre, coordinateur consomme | Run reprend `NOT_SUBMITTED` après fermeture prouvée de l’attempt précédent ; nouveau dispatch/attempt | Comportement retry conservé seulement pour les échecs explicitement terminaux. L’incrément a lieu au nouveau consume, pas avant. |
| Ambiguïté | Coordinateur | Dispatch/run/turn `NEEDS_REVIEW` ou équivalent externe inconnu | Pas de retry, correction ou replay de la même occurrence. |
| Correction/continuation | Workflow | Nouveau tour/run/input hash/claim/occurrence | Nouvelle intention et consume ; parent et raison persistés. |
| Pagination/status/poll | Workflow + route dédiée | Un dispatch par GET/page physique | Nouveau cursor/ordinal après réponse précédente complète ; pas de boucle multi-page sous un permis. |

**VERIFIED —** le gateway actuel committe avant `adapter.invoke`, et un retry terminal peut créer un autre `bridge_request_id` sur le même ModelRun (`backend/src/cti_app/application/model_gateway.py:1139-1172,1257-1264,1317-1343`). Le contrat remplace ce claim par le CAS dispatch/run du consume. `ModelGateway.execute` ne peut ni réutiliser un permis ni faire un second send interne sous le premier.

`SUBMISSION_IN_PROGRESS` est l’état «consume commit effectué, résultat attendu». Si le processus tombe, l’absence de POST n’est pas prouvée par SQL ; l’issue passe en review, sauf preuve locale positive que le transport n’a jamais été invoqué. Une réponse complète, même terminalement négative côté Bridge, clôt le dispatch `SUCCEEDED` et marque le run `VERIFIED_TERMINAL_FAILURE` ; seul un CAS exact le rend ensuite `NOT_SUBMITTED` avant un nouveau consume. `EXTERNAL_STATE_UNKNOWN` représente l’incertitude après entrée/résultat possible, avec run/turn en review. `VERIFIED_TERMINAL_FAILURE` ne prouve aucun reçu fournisseur.

**UNKNOWN — retries physiques :** les implémentations explicites lues montrent un appel HTTP dans certaines méthodes, pas une garantie générale sur clients injectés, redirects, SDK, preflight ou Bridge UI (`docs/mission04-implementation/_work/S3D_B_OWNER_ROUTE_GATEWAY.md:270-278,362-379`). Le catalogue refuse toute route dont le dernier point d’envoi n’est pas capturé.

## 8. Autorité budgétaire et settlement par chemin

| Chemin | Autorité proposée | Settlement proposé et limite |
|---|---|---|
| VT report/relation | `investigation_envelopes.budget_snapshot`, dimensions route VT versionnées `VT_UNITS` | Réserver le plafond connu avant consume ; mesure application par requête physique/page. Meta provider ne vaut pas reçu. |
| Shodan | Enveloppe, dimension `SHODAN_CREDITS` et estimation route gelée avant send | Mesure selon policy locale ; `meta.credits_consumed` est donnée de réponse/estimation, pas preuve de facturation (`backend/src/cti_app/application/investigation_shodan_tools.py:1202-1222`; `backend/src/cti_app/infrastructure/shodan.py:133-178`, relevé `docs/mission04-implementation/_work/S3D_C_VERSION_SETTLEMENT.md:91-99`). |
| Retrohunt submit/status/results | `retrohunt_reservations` garde le slot de soumission ; enveloppe seulement après définition de coût approuvée | Chaque physical send a son dispatch. Les tools ont actuellement `cost_model={}` (`backend/src/cti_app/application/investigation_retrohunt_tools.py:81-105`) ; routes deny-all tant qu’une unité/prix d’application route-versionné n’est pas décidé. Le slot n’est pas un reçu. |
| Acquisition VT | Enveloppe pour `VT_UNITS`/`DOWNLOADS`; `analyst_investigations.loop_budget` pour `VT_READ_UNITS`/`HITS_ACQUIRED`; `sample_acquisition_attempts` journalise le résultat | Ces couches ne s’additionnent pas comme un total fournisseur. Compter `NEW_SAMPLES` seulement à l’insertion d’un Sample nouveau vérifié. Réemploi local vérifié avant consume = delta 0 et aucun download. |
| Bridge/advisor | Enveloppe `BRIDGE_TURNS=1` par tour logique advisor ; contexte/temps suivent la réservation de l’opération | Chaque POST/retry physique a un dispatch/mesure ; aucun second tour logique pour un retry. Pas d’atomicité SQL+Bridge ni de reçu inventé. |

**VERIFIED — acquisition actuelle :** le chemin réserve `{VT_UNITS:1, DOWNLOADS:1}` et vérifie localement des samples avant download ; le service débite `VT_READ_UNITS` et `HITS_ACQUIRED` avant le port (`backend/src/cti_app/application/investigation_acquisition.py:201-288,343-388`; `backend/src/cti_app/application/sample_acquisition.py:349-377,407-429`). `NEW_SAMPLES` n’est pas débité dans ce chemin (`docs/mission04-implementation/_work/S3D_C_VERSION_SETTLEMENT.md:86-94`). Le succès Sample/ledger arrive après ingestion bytes (`backend/src/cti_app/application/sample_acquisition.py:533-620`).

**PROPOSED — acquisition ambiguë :** le consume garde la réservation et les quotas d’essai ; timeout, erreur transport, ack perdu ou bytes partiels ne donnent ni refund automatique ni nouvelle occurrence. `NEW_SAMPLES` est réservé à l’écriture d’un nouvel objet Sample vérifié. Une découverte locale au second contrôle ne peut pas être comptée comme nouveau download ; déplacer le check/réservation pour qu’un reuse vérifié précède le consume.

**Décisions humaines requises avant activation des familles concernées :**

- confirmer si `DOWNLOADS` est une tentative au consume ou un téléchargement complet ;
- confirmer si `HITS_ACQUIRED` signifie tentative de hit ou sample effectivement acquis ;
- fixer le coût route exact VT pagination, Shodan, Retrohunt status/results/retry et acquisition ; aucun reçu fournisseur n’est supposé ;
- fixer le moment de décompte `NEW_SAMPLES` et la réparation si blob et SQL ne commitent pas ensemble ;
- confirmer que le budget JSON de l’enveloppe reste seul maître, sans adoption des tables relationnelles existantes.

`CONFIRMED_NOT_SENT` ne peut jamais découler d’un timeout, socket reset, exception adapter après entrée, HTTP status, erreur provider, absence de timestamp ou absence de reçu. Une réponse fournisseur «pré-submit» reste un fait de réponse à archiver ; elle n’est pas convertie en preuve framework ni refund sans règle route versionnée et décision humaine. Aucune transaction SQL+HTTP n’est supposée atomique.

## 9. DAG d’implémentation L10–L14 et slice différé

Ce DAG précise les lots envisagés par `5` du plan (`docs/mission03-architecture/IMPLEMENTATION_PLAN.md:228-328`). Toutes les étapes ci-dessous sont **futures** et conditionnelles à la levée du verdict S3 ; les oracles `10` ne sont pas exécutés.

| Ordre | Lot | Préconditions | Livrable et gate |
|---|---|---|---|
| 0 | Décisions de contrat | Décisions humaines `11` ; routes exactes candidates ; owner scope ; projection normalizer/golden vectors | En cas d’inconnue structurante, route concernée deny-all ; aucune migration ni send. |
| 1 | **L10 — route, normalizer, serializer, claim et owner pur** | Lot 0 ; aucun transport actif | `ExternalOperationSpec` unique, catalogue fermé, bytes/digests exacts, identité de continuité et occurrence workflow ; tests purs/fakes sans I/O ; owner-binding documenté. |
| 2 | **L11 — schéma et persistence additive** | L10 accepté ; head Alembic vérifié dans la mission d’implémentation ; ADR-0007 respecté | Tables claims/dispatch/events/measurement/settlement/uses et binding durable turn/run/invocation, CHECK/FK/uniques, repositories/UOW ; zéro backfill des paramètres historiques ; aucune route activée. |
| 3 | **L12 — writers, lock order et consume** | L11 vert ; migration additive revue puis seulement autorisée dans la mission suivante ; READ COMMITTED explicite | Refactor admission, reservation, stop interaction, denial, finalization, reconcile, governance/revocation, acquisition/Retrohunt/Bridge recovery ; barrier tests PostgreSQL deux sessions ; 40P01/55P03 absents ; aucun lock pendant fake HTTP. |
| 4 | **L13 — handoff ModelGateway et dernier mètre** | L12 vert et handoff Run/Turn CAS accepté ; catalog production toujours deny-all | Chaque retry/page/poll/preflight physique passe par nouveau dispatch/consume ; fake permit-aware sur chaque entrée brute ; direct-entry zéro-call et SDK/redirect test ; aucun endpoint live. |
| 5 | **L14 — settlement, versions, occurrence et lineage** | L10–L13 vert ; décisions coût/quota prises ; policy reuse explicitement approuvée si demandée | settlement idempotent, conservation ambiguë, occurrence workflow, bump fail-closed, use row source/claimant/refs et delta zéro. Aucun réemploi cross-invocation activé par ce document. |
| Après L10–L14 | Mission distincte : premier slice `vt.file_report` **test-only** | Binding owner/route et contrat transactionnel fermés ; tests PostgreSQL requis verts ; fake final seulement | Une route fake dans composition de test, absente de composition production ; autres familles deny-all. Pas de client VirusTotal réel. Cela reste hors de la présente mission et non autorisé ici. |

**PROPOSED —** aucune étape ne peut sauter le gate L12 transactionnel pour atteindre un fake send, et aucun résultat de tests documentaires ne rend S3 production-ready. L’activation de toute route nécessite une autorisation distincte, hors du présent contrat.

## 10. Oracles RED futurs

Chaque oracle décrit setup, action, assertions et la régression qu’il doit rendre rouge avant le contrat. Ils sont des tests futurs ; aucun n’a tourné ici. Les tests concurrency exigent deux sessions PostgreSQL indépendantes et faux transports locaux.

1. **`RUNNING → NEEDS_REVIEW` avant consume.** **Setup :** A commite owner `RUNNING`, réservation et dispatch `PREPARED`, puis barrière avant consume. **Action :** B CAS l’owner à `NEEDS_REVIEW` et committe ; A reprend. **Assert :** dispatch reste non consommé ou `DENIED`, aucun permis, invocation/run jamais remis à RUNNING, compteur fake = 0. **Pourquoi rouge avant :** le callback legacy relit l’enveloppe mais ne verrouille/revalide pas atomiquement l’invocation/stop (`backend/src/cti_app/application/investigation_service.py:1155-1226`). |
2. **Révocation/consume-first et absence de lock HTTP.** **Setup :** deux sessions, même envelope/dispatch ; fake transport bloque après entrée. **Action A :** pour revoke-first, B committe la révocation avant qu’A consume ; variante consume-first, A committe consume puis entre dans fake, B révoque et committe pendant que fake est bloqué. **Assert :** revoke-first = aucun consume/permis/call ; consume-first = une seule consommation/permis, B committe sans attendre le fake, une seule entrée transport. Le droit one-use reste valable même si B committe avant le début réseau. **Pourquoi rouge avant :** les checks legacy sont lecture/callback hors transaction atomique (`backend/src/cti_app/application/investigation_service.py:1155-1226`) et le code n’a pas de point S3 linéaire. **Décision :** verdict bloqué tant que la sémantique consume-first n’est pas acceptée. |
3. **Pas d’inversion et pas de deadlock.** **Setup :** A tient envelope au début de consume et attend barrière ; B finalise/reconcile le même owner avec UOW production. Répéter state↔envelope et recovery Bridge. **Action :** relâcher A, puis B ; couvrir aussi l’ancien ordre invocation→envelope en test de non-régression. **Assert :** deux transactions finissent ; aucun SQLSTATE `40P01` (deadlock) ni `55P03` (lock timeout) ; au plus une transition CAS par version ; aucun lock tardif pris avant envelope. **Pourquoi rouge avant :** finalization/reconcile et operator-led ont les inversions citées en `5.2`. |
4. **Owner/route mismatch et route inactive.** **Setup :** invocation A + réservation + claim/dispatch VT fake ; composition test route active et composition production deny-all. **Action :** falsifier séparément investigation, owner ID/version, operation key, tool/schema, family/provider, reservation amounts, route/catalog version, normalizer/serializer, digest/body ; essayer route absente/stale/inactive. **Assert :** aucun CAS vers `CONSUMED`, aucun permit, fake = 0 ; production n’expose pas route fake. **Pourquoi rouge avant :** `ToolDefinition` et registry n’ont ni route ni version de codec (`backend/src/cti_app/application/investigation_tools.py:150-159,351-409`). |
5. **Owner stale, entrée raw directe et permis rejoué.** **Setup :** owner stale/revoked dans PostgreSQL ; fake au dernier transport. **Action :** appeler chaque entrée brute sans permis, puis permis copié/stale/rejoué ou avec digest différent. **Assert :** refus typé, aucune entrée fake ; un nonce consommé ne fonctionne pas une seconde fois. **Pourquoi rouge avant :** aucun gate S3 au dernier transport dans le dispatcher actuel (`backend/src/cti_app/application/investigation_tools.py:619-678`). |
6. **Retry gateway, redirect, SDK et ambiguïté.** **Setup :** fake Bridge terminal explicit, fake timeout/ack perdu, fake redirect/SDK retry. **Action :** laisser le gateway traiter ces cas. **Assert :** chaque send physique autorisé a exactement un dispatch consommé, request ID et mesure ; terminal confirmé seulement crée le prochain dispatch ; timeout/partial/ack perdu passe review et deuxième send = 0 ; redirect/SDK est désactivé ou consomme une autre ligne. Preflight faisant HTTP doit lui-même avoir un dispatch. **Pourquoi rouge avant :** le gateway peut retenter en interne (`backend/src/cti_app/application/model_gateway.py:1257-1264`) et les ports injectés/UI ne sont pas tous observés. |
7. **Normalizer bump et occurrence forgée.** **Setup :** préparer même intention sous normalizer v1 puis v2 ; créer un workflow autorisé après résultat terminal complet. **Action :** caller répète le même input, puis tente UUID d’occurrence arbitraire ; workflow crée ensuite occurrence enfant. **Assert :** bump = review, aucun nouvel effect key/dispatch/permis/send ; UUID caller refusé ; occurrence enfant seulement après précondition, avec parent/ordinal/autorisation persistés. **Pourquoi rouge avant :** R7 montre que version dans la clé et sentinel dérivé peuvent réouvrir un claim (`docs/mission03-architecture/_work/R7_S3_REVIEW_R2.md:32-34`). |
8. **Réemploi terminal complet vs ambigu.** **Setup :** source complète exacte, source `PARTIAL`, `NEEDS_REVIEW`, `UNAVAILABLE` et ack perdu. **Action :** demander réemploi vers claimant. **Assert :** seule la complète même-version/policy pourrait créer `external_effect_uses` avec source/claimant/refs et delta `{}` ; toutes les ambiguës donnent zéro use, zéro permit, zéro second egress. Aucune route n’active ce réemploi dans cette mission. **Pourquoi rouge avant :** replay courant transmet une réponse partielle (`backend/src/cti_app/application/investigation_service.py:1951-2035`). |
9. **Double settlement concurrent et réponse tardive.** **Setup :** A/B règlent même dispatch + reservation key ; dans une variante, B reçoit tardivement résultat complet alors qu’A a mis review. **Action :** callbacks concurrents, mêmes puis montants différents, digest exact puis erroné. **Assert :** un seul CAS/measurement par dispatch/dimension/settlement key ; doublon identique idempotent ; montant différent conflit ; résultat tardif exact s’attache au plus une fois ; digest faux ou partial n’écrase rien. **Pourquoi rouge avant :** code courant n’a pas de receipt unique par dispatch/settlement S3 (`docs/mission04-implementation/_work/S3D_A_LOCKS_WRITERS.md:9-18`; settlement legacy `backend/src/cti_app/application/investigation_service.py:2761-2832`). |
10. **Limite de `CONFIRMED_NOT_SENT`.** **Setup :** fake instrumentation permet de savoir si l’adapter final a été invoqué. **Action :** invalide arguments/route avant entrée ; puis exception après entrée, timeout, reset socket, HTTP 404, réponse provider, ack perdu, octets partiels. **Assert :** uniquement un chemin framework prouvant zéro entrée peut confirmer non-envoyé et libérer le hold ; tous les autres restent consommés/review/conservateurs. Une réponse provider n’est pas convertie en preuve framework. **Pourquoi rouge avant :** le dispatcher legacy n’a pas de preuve/dispatch S3 durable liée au dernier point d’envoi (`backend/src/cti_app/application/investigation_tools.py:779-804`; `backend/src/cti_app/domain/investigation.py:30-42`). |
11. **Acquisition : reuse local, double session, frais et partial.** **Setup :** Sample quarantine vérifié local, absence de Sample, deux sessions concurrentes même hash, puis stream partiel. **Action :** appeler coordinateur et faux download. **Assert :** reuse local pré-consume = zéro dispatch/send et delta enveloppe/loop `{}` ; frais = un seul claim/send et quotas route-versionnés ; partial = `NEEDS_REVIEW`, aucun `NEW_SAMPLES`, aucune seconde tentative automatique. **Pourquoi rouge avant :** deuxième contrôle peut trouver un sample local après réservation tout en retournant coût plein (`backend/src/cti_app/application/investigation_acquisition.py:343-388,515-537`; `backend/src/cti_app/application/sample_acquisition.py:315-347`). |
12. **Bridge turn/run et attempt.** **Setup :** turn/run `RUNNING/NOT_SUBMITTED`, turn lié durablement à investigation/reservation/invocation advisor. **Action :** consume deux sessions même attempt ; simuler résultat complet, échec terminal explicite, ambiguïté et correction. **Assert :** un seul gagnant fait `SUBMISSION_IN_PROGRESS` et incrémente attempt ; l’autre n’obtient aucun permis ; retry terminal a nouveau dispatch/attempt mais un seul `BRIDGE_TURNS` logique ; ambiguity = `NEEDS_REVIEW` ; correction = nouveau turn/run/digest. **Pourquoi rouge avant :** le gateway revendique/relance le run indépendamment du dispatch S3 (`backend/src/cti_app/application/model_gateway.py:1051-1144,1257-1343`). |

## 11. Décisions humaines requises et UNKNOWN bloquants

| Décision / inconnue | Position contractuelle proposée | Effet si non résolu |
|---|---|---|
| **Consume vs révocation** | Commit consume est le point linéaire ; consume-first gagne. | **Bloquant.** Si révocation doit interdire tout POST qui n’a pas encore commencé, redessiner un fence sans garder lock SQL pendant HTTP. |
| **Projection normalizer par route** | Continuity key sans version ; mismatch version = review, pas alias ni send. | **Bloquant par route.** Fixer champs/provider projection, encodage, golden vectors et serializer exact. |
| **Occurrence autorisée** | Workflow nommé, version/étape/ordinal/parent ; aucun UUID caller seul. | **Bloquant pour retry/page/poll/correction.** Nommer les opérations et leurs conditions de succès préalable. |
| **Coût VT/Shodan/Retrohunt** | Mesure AutoWork via policy route versionnée, jamais reçu inventé. | **Bloquant sur routes sans coût borné.** Retrohunt a actuellement `cost_model={}`. |
| **Quotas acquisition** | Proposition conservative à l’essai consume ; `NEW_SAMPLES` au nouveau Sample hash vérifié. | **Bloquant acquisition.** Confirmer `DOWNLOADS`, `HITS_ACQUIRED`, moment `NEW_SAMPLES` et réparation artifact/SQL. |
| **Source de budget** | Proposition : enveloppe JSON autorité opérateur ; boucle acquisition séparée ; table settlement uniquement idempotence. | **Bloquant si l’architecture veut `investigation_budget_*`.** Il faut alors une seule autorité, pas deux writes concurrents. |
| **ModelGateway hors turn** | Union owner actuelle ne couvre que invocation/Bridge turn. | **Bloquant pour extraction/discovery/production et autres gateways.** Toutes leurs routes restent deny-all jusqu’à owner approuvé. |
| **Reuse historique** | Pas de backfill depuis paramètres bruts, pas de réemploi cross-invocation autorisé ici. | Toute exception future demande inventaire humain et preuve route/digest/résultat complet. |
| **Isolation/configuration** | UOW S3 explicite `READ COMMITTED`; CAS/locks/uniques. | **UNKNOWN runtime.** Aucune connexion DB/config serveur n’a été vérifiée ; test futur sur deux sessions. |
| **Routes réellement composées et sends physiques** | Catalogue de production deny-all jusqu’à audit de chaque last-mile, preflight, poll, redirect, SDK et UI Bridge. | **UNKNOWN runtime.** Aucun transport n’a été invoqué dans cette mission. |

## 12. Verdict final

**Verdict unique : `S3_BLOCKED_DESIGN`.**

Le contrat ferme au niveau de conception l’inversion invocation/enveloppe en imposant un ordre unique, l’ambiguïté owner/route par une union fermée rechargée, le handoff ModelGateway par un consume qui possède l’attempt, le bump normalizer par une continuity key fail-closed, et la classification par un seul `ExternalOperationSpec`. Il borne également le settlement ambigu et distingue la comptabilité opérateur des quotas acquisition.

S3 reste bloqué par la sémantique humaine consume-first/révocation, les projections par route, les coûts/quota acquisition, la couverture de tous les transports physiques, l’isolation réellement configurée et les owners ModelGateway hors investigation. Aucun verdict `S3_PRODUCTION_READY` n’est posé. Le premier slice test-only `vt.file_report` attend une mission distincte et tous les gates L10–L14 pertinents. Aucun test documentaire n’autorise du code S3.

## 13. Revue indépendante Mission 04 (ajout parent, non une révision de conception)

**Verdict indépendant : `S3_BLOCKED_DESIGN`.** Rapport :
`_work/S3D_CONTRACT_REVIEW.md` (SHA-256
`8613a7b2338a4aa6b05b8cde8917c0155186d09dbf54da4b06583ed3f74f2f69`).

Statut indépendant : F1–F4 **PARTIAL**, N1 **PARTIAL**, N2 **CLOSED — design
seulement**, N3–N5 **PARTIAL**, N6 **CLOSED — design seulement**.

Findings HIGH résiduels à traiter dans une révision ultérieure :

1. La garantie « aucun second send sans consume » n’est pas encore prouvée
   pour `ModelGateway.execute` (retry interne, `model_gateway.py:1139-1179,1257-1264`).
2. L’inventaire des sends Bridge doit couvrir récupération POST, polling GET,
   fermeture DELETE, retries et corrections (`integrations/models.py:544-581,885-893`;
   `bridge_investigator.py:415-495,1271-1340`).
3. Le polling verrouille `model_runs` pendant l’appel réseau, contrairement à
   la règle « aucun lock pendant HTTP » (`model_gateway.py:844-876`).
4. La réponse tardive après `NEEDS_REVIEW` n’a pas de transition/authority de
   settlement définie.
5. La règle de bump `normalizer_version` demande une mise en revue alors que
   le cycle interdit de rouvrir un claim fermé.

Findings MEDIUM : synchronisation de l’oracle d’inversion de locks, distinction
denial avant consume / preuve post-consume de non-envoi, et assertions de coûts
d’acquisition dépendantes de décisions humaines.

Ce document reste une **base de révision de design** pour une mission S3
ultérieure. Il n’autorise ni implémentation S3, ni route de production, ni
migration, ni cutover, ni slice `vt.file_report` dans Mission 04.
