# D6 — Brief commun S3 : décision de dispatch et identité d'effet externe

**SHA :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Nature :** brief de conception only. Aucun code, test, migration ou configuration
ne doit être modifié. Le seul écrit autorisé est l'artefact du designer.
**Sources à lire :** ce brief, `_work/M1A_EXTERNAL_EFFECTS.md`,
`_work/M3_S3_DIAGNOSIS.md`, `_work/M2A_TEST_SURFACE_EXTERNAL.md`,
`backend/AGENTS.md`, `docs/adr/ADR-0007-frozen-baseline-and-c2-migration.md`,
`.agents/skills/codebase-design/{SKILL.md,DEEPENING.md,DESIGN-IT-TWICE.md}`.
**Interdit :** lire les artefacts `D6_S3_*.md` des autres designers, appeler un
fournisseur/PostgreSQL/MinIO/Docker, exécuter des tests, créer des sous-agents.

## 1. Faits vérifiés au SHA

### Chemins

- Opérateur : `api/investigation.py:1461-1517` →
  `OperatorToolInvocationService.invoke` (`operator_tool_invocation.py:700-930,1031-1123`)
  → `InvestigationService.execute_tool_call` (`investigation_service.py:1007-1321`)
  → `RegistryToolDispatcher.dispatch` (`investigation_tools.py:619-798`) →
  adapter → port/transport provider.
- Bridge : `InvestigationService.run` (`investigation_service.py:523-672`) →
  `BridgeInvestigator.turn` (`bridge_investigator.py:1172-1445`) →
  `ModelConversationTurnTransport`/`ModelConversationService` →
  `ModelGateway` → provider.
- L'adaptateur advisor est un cas à part : `advisor.consult` est déclaré
  `EXTERNAL_READ` (`investigation_advisor_tools.py:3395-3409`) alors qu'il
  soumet une consultation modèle via `submit_bridge_consultation_once`.

### Défauts vérifiés

1. `ToolDefinition.effect` est fixé à l'enregistrement et consommé par le
   service/dispatcher ; le JSON client ne le choisit pas. Le registre reste
   mutable (`register`/`unregister`, `investigation_tools.py:363-409`).
2. `normalized_parameters` reçoit `arguments` bruts
   (`investigation_service.py:1031-1040`) ; chaque adapter normalise plus tard.
   Il n'existe aucune identité d'effet canonique persistée distincte de
   `operation_key`.
3. L'invocation `PENDING → RUNNING` et la réservation sont commitées avant le
   dispatch (`investigation_service.py:1031-1153`).
4. Le callback `check_current_admission` relit l'enveloppe et la réservation,
   puis retourne ; le dispatcher entre ensuite dans l'adaptateur
   (`investigation_service.py:1190-1237` ; `investigation_tools.py:779-784`).
   Le callback est optionnel (`investigation_tools.py:412-427`).
5. Aucune transaction PostgreSQL ne réclame atomiquement
   `{enveloppe+version, réservation RESERVED, invocation RUNNING, décision
   d'effet, tentative Bridge}`. La révocation verrouille l'enveloppe seulement
   pendant sa propre sauvegarde (`investigation_governance.py:428-487` ;
   `repositories/investigation.py:1426-1467`).
6. Un replay peut passer une invocation `RUNNING` en `NEEDS_REVIEW`
   (`operator_tool_invocation.py:863-878` ; `investigation_service.py:1079-1098`)
   ; l'exécution originale ne relit pas le statut de l'invocation dans son
   callback et peut encore dispatcher (`investigation_service.py:1190-1226`).
7. Les verrous sont locaux au processus (`operator_tool_invocation.py:700-761` ;
   `investigation_tools.py:619-642`).
8. Bridge : jusqu'à 3 tentatives pré-soumission et 2 corrections, avec des
   clés d'essai distinctes, sous une seule réservation `BRIDGE_TURNS`
   (`bridge_investigator.py:59,1204-1354,1395-1445`). Le `ModelConversationTurn`
   et le `ModelRun` sont persistés avant l'exécution
   (`model_conversations.py:1282-1305`), mais ne sont pas coordonnés avec un
   consume ni avec la révocation.
9. Retrohunt et acquisition ont leurs propres machines d'état durables
   (`retrohunt.py:108-239` ; `sample_acquisition.py:282-447`), séparées du
   journal `ToolInvocation`.
10. Un résultat externe ambigu devient `NEEDS_REVIEW`, conserve sa réservation
    et n'est pas resoumis automatiquement (`investigation_service.py:1238-1321`).
    Une preuve de non-exécution fournie par un adaptateur est retirée
    (`investigation_tools.py:799-801`).
11. Le réemploi inter-invocations d'un résultat terminal n'a pas de politique
    commune de filiation/budget ; seuls des chemins spécifiques (acquisition,
    replay exact) la préservent.

### États et migrations

- `ToolInvocation` : `PENDING`, `RUNNING`, `NEEDS_REVIEW`, terminaux ; CAS de
  version ; `operation_key` unique (`domain/investigation.py:18-27,430-525` ;
  `repositories/investigation.py:159-314`).
- Enveloppe : version CAS + `SELECT … FOR UPDATE` ; réservation
  `RESERVED/COMMITTED/RELEASED` (`domain/investigation.py:218-370` ;
  `repositories/investigation.py:1414-1577`).
- Toute nouvelle colonne/table/état doit être une révision additive après le
  head courant ; `0001_baseline` est gelée ; ne jamais réécrire `0002` ; les
  CHECK de statut existants doivent être étendus par migration si un nouvel
  état est ajouté (ADR-0007:19-45 ; `backend/AGENTS.md`).

## 2. Garanties non négociables

- PostgreSQL, les artifacts versionnés et MinIO/SHA-256 restent canoniques ;
  préserver provenance, versions, CAS, idempotence, références de preuve.
- G2/G3, TLP, `external_llm_allowed`, `do_not_submit`, frontières provider,
  révocations et approbations humaines restent contraignants.
- Aucun appel fournisseur réel, aucune hypothèse d'idempotence ou de
  canonicalisation côté fournisseur.
- Une révocation commitée **avant** le consume durable doit empêcher l'egress.
  Une révocation après le consume ne peut pas annuler un HTTP déjà autorisé ;
  le comportement des egress ultérieurs doit être explicite.
- Ne pas tenir un verrou SQL pendant le transport. Le point de décision est un
  commit court, avant l'I/O réseau.
- Crash/timeout/annulation après consume sans issue prouvée → `NEEDS_REVIEW`,
  jamais de resoumission automatique.
- Un résultat réutilisé doit préserver la filiation invocation/observation et
  ne charger le budget qu'une fois.
- Ne pas introduire de plafond arbitraire, de troncature silencieuse ou de
  perte d'observabilité des preuves autorisées.
- L'interface ne doit jamais rendre une consultation plus permissive que son
  autorisation effective, ni rendre les preuves autorisées moins observables.

## 3. Classification des dépendances (DEEPENING)

- PostgreSQL : système possédé, avec adapters réels et base jetable ; c'est le
  seul oracle acceptable pour l'ordre `SELECT … FOR UPDATE`, l'unicité et les
  courses multi-sessions.
- Transports VT/Shodan/Retrohunt/acquisition : systèmes externes réels ;
  doubles de transport/mocks pour les tests, aucune hypothèse d'idempotence.
- Bridge/modèle : système externe réel ; le `ModelConversationTurnTransport`
  possède déjà un double scripté et une persistance durable.
- Calcul d'identité/normalisation : in-process, testable directement.
- Aucun port hypothétique sans second adapter réel.

## 4. Questions auxquelles la conception doit répondre

1. Où placer le seam ? (service, dispatcher, gateway partagé, adaptateur ?)
2. Contrat public exact : types, opérations, invariants, ordre, erreurs.
3. Quel état est vérifié et écrit dans la transaction de consume ? Comment
   sont ordonnés les verrous (enveloppe, invocation, réservation, décision,
   tentative Bridge) ?
4. Comment l'identité d'effet normalisée est-elle calculée, versionnée,
   persistée et liée aux octets réellement émis ?
5. Comment chaque adapter/transport direct est-il fail-closed sans permis ?
6. Le permis couvre-t-il une requête physique ou une session bornée
   (retry, page, correctif) ? Que signifie une révocation après consume pour
   les egress suivants ?
7. Comment représenter l'ambiguïté, le crash avant/après consume, le timeout,
   la réponse tardive et la réconciliation ?
8. Comment un résultat réutilisé préserve-t-il invocation/observation/budget ?
9. Quelles transitions nécessitent une migration additive ? Quel rollback ?
10. Quel code ou quelle obligation d'appelant peut réellement disparaître ?
11. Quels oracles positifs, négatifs, crash, replay et concurrence PostgreSQL
    prouvent le contrat ?
12. Quelles hypothèses restent `UNKNOWN` (idempotence provider, portée Bridge,
    comportement runtime) ?

## 5. Format de l'artefact (13 points obligatoires)

1. Seam actuel et seam proposé.
2. Contrat public (types et pseudo-code).
3. Invariants, timing, versions, erreurs, confidentialité.
4. Exemple d'appel depuis un consommateur réel.
5. Responsabilités cachées dans l'implementation.
6. Responsabilités supprimées des appelants.
7. Dépendances et adapters.
8. Source de vérité persistante et point de décision.
9. Oracles positifs, négatifs, crash, replay, concurrence.
10. Compatibilité, migration et rollback.
11. Deletion test.
12. Comparaison avant/après avec éléments vérifiables.
13. Risques, `UNKNOWN` et limites.

La conception doit explicitement traiter les 7 décisions `BLOCKED_DESIGN` de
`M3_S3_DIAGNOSIS.md` §« Remaining BLOCKED_DESIGN decisions ».
