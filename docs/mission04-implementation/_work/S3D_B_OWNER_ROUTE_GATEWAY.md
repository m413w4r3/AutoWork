# Mission 04 / Phase 2 — S3-D / Axe B : owner, route et ModelGateway

**STATUS :** `READ_ONLY_ANALYSIS_COMPLETE / S3_READY_FOR_IMPLEMENTATION_NOT_MET`

**Base vérifiée :** `e120a36f1afa08261fdf0276e0200872973ad8f2`
**Portée :** analyse et contrat proposé uniquement. Aucun code, test ou schéma modifié ; aucun serveur, PostgreSQL, provider, Bridge, VT, Shodan, Retrohunt, MinIO ou run CTI lancé.

Les mentions **VERIFIED**, **INFERENCE** et **UNKNOWN** distinguent respectivement le code ou contrat lu, une conséquence de ces éléments, et un point non établi. Les chemins sont relatifs à la racine du dépôt et les lignes désignent la révision de base indiquée ci-dessus.

## 1. Conclusions de preuve

- **VERIFIED —** Les propriétaires durables aujourd’hui sont séparés : `ToolInvocation` pour l’opération d’outil, `ModelConversationTurn` pour un tour, `ModelRun` pour une exécution modèle. Il n’existe pas de FK entre invocation et tour/run, et ni le tour ni le run n’ont aujourd’hui de FK d’investigation ([`domain/investigation.py:430-445`](../../../backend/src/cti_app/domain/investigation.py), [`models/investigation.py:59-101`](../../../backend/src/cti_app/infrastructure/database/models/investigation.py), [`models/model_execution.py:39-123,196-247`](../../../backend/src/cti_app/infrastructure/database/models/model_execution.py)).
- **VERIFIED —** `ToolDefinition` classe un outil par nom, version de schéma, `ToolEffect`, coût, confidentialité et capacité ; il ne déclare ni opération/famille, ni route, ni versions de normalizer/serializer. La composition enregistre des outils auprès d’un registre mutable puis construit les dispatchers ([`application/investigation_tools.py:39-48,150-159,351-409`](../../../backend/src/cti_app/application/investigation_tools.py), [`application/investigation_composition.py:1250-1297,1336-1345,1542-1625`](../../../backend/src/cti_app/application/investigation_composition.py)).
- **VERIFIED —** `ModelConversationService` persiste le run et le tour, puis appelle `ModelGateway.execute`. Le gateway committe sa revendication d’envoi avant l’adaptateur, mais cette revendication n’est pas atomique avec un dispatch S3 et ne couvre pas à elle seule les envois physiques ([`application/model_conversations.py:1282-1305`](../../../backend/src/cti_app/application/model_conversations.py), [`application/model_gateway.py:1026-1144`](../../../backend/src/cti_app/application/model_gateway.py)).
- **VERIFIED —** Le transport Bridge d’investigation utilise `stateless_external=True`; le `ModelRequest` transmis au gateway a alors `conversation=None`. Il peut donc satisfaire le prédicat de retry interne du gateway (`request.conversation is None`) après une défaillance Bridge terminale confirmée. Une autre relance pré-soumission est faite dans `submit_bridge_consultation_once` lorsqu’un 429 prouve `PRE_SUBMISSION` et que le transport offre `submit_with_attempt` ([`application/bridge_investigator.py:415-495,764-780`](../../../backend/src/cti_app/application/bridge_investigator.py), [`application/model_conversations.py:1243-1279`](../../../backend/src/cti_app/application/model_conversations.py), [`application/model_gateway.py:1257-1264,1570-1584`](../../../backend/src/cti_app/application/model_gateway.py)). Chaque envoi physique de ces deux chemins doit avoir son propre consume.
- **INFERENCE —** La bonne autorité est un coordinateur de dispatch durable appelé par les entrées d’envoi, avec une union propriétaire fermée et un transport final privé à la composition. `PreparedExternalRequest` ne confère ni G2 ni droit d’émettre ; ses IDs, route et digests sont des affirmations à recharger et comparer.
- **UNKNOWN BLOQUANT —** Le contrat N2/N3 de R7-S3-R2 reste inachevé, et le document de décision conserve `S3_BLOCKED_DESIGN` / gate non atteint ([`R7_S3_REVIEW_R2.md:24-50`](../../mission03-architecture/_work/R7_S3_REVIEW_R2.md), [`DESIGN_OPTIONS.md:619-625,670-690`](../../mission03-architecture/DESIGN_OPTIONS.md)). Le présent rapport clarifie deux sous-contrats ; il ne lève pas N1/N4, les autres parties de F2–F4, ni l’approbation M7 requise avant implémentation.

## 2. Inventaire des owners et de leur cycle de vie

### 2.1 `ToolInvocation`

**VERIFIED.** Le domaine contient `investigation_id`, `operation_key`, `tool_name`, `schema_version`, `normalized_parameters`, `purpose`, `status`, budgets réservé/consommé, refs de réponse, `id`, horodatages et `version` ([`domain/investigation.py:430-465`](../../../backend/src/cti_app/domain/investigation.py)). La transition est `PENDING → RUNNING → terminal`; `PENDING` peut aussi être fermé sans envoi. Une invocation en `NEEDS_REVIEW` peut devenir `FAILED` seulement sous preuve pré-soumission ([`domain/investigation.py:468-525`](../../../backend/src/cti_app/domain/investigation.py)).

La table `tool_invocations` a FK restrictive vers `analyst_investigations`; `operation_key` est unique globalement. Les CHECK portent sur statut, `version >= 1`, JSON objet de paramètres et budgets, et tableau JSON des refs ; des index couvrent investigation/temps et statut ([`models/investigation.py:59-101`](../../../backend/src/cti_app/infrastructure/database/models/investigation.py)). Elle n’a pas de champ `route_id`, famille, normalizer, serializer ou digest du corps émis, et pas de FK vers `model_runs` ou `model_conversation_turns`.

`InvestigationService._run_tool` lit la définition de registre, crée l’invocation avec l’operation key et le nom/schema, puis réserve et passe l’appel au dispatcher ([`application/investigation_service.py:1018-1048`](../../../backend/src/cti_app/application/investigation_service.py)). Il effectue un contrôle d’admission avant dispatcher, puis finalise invocation et réservation ([`application/investigation_service.py:1190-1237,1299-1319`](../../../backend/src/cti_app/application/investigation_service.py)). Un échec externe dont l’absence d’envoi n’est pas prouvée devient `NEEDS_REVIEW` ([`application/investigation_service.py:1238-1267`](../../../backend/src/cti_app/application/investigation_service.py)).

### 2.2 Enveloppe, état de travail et réservation

**VERIFIED.** `InvestigationEnvelopeRow` détient snapshot budget, approbation, fournisseurs autorisés, TLP, `do_not_submit`, `external_llm_allowed` et version ([`models/investigation.py:547-582`](../../../backend/src/cti_app/infrastructure/database/models/investigation.py)). L’état de travail porte `investigation_id`, version, `turn_index`, contexte pending et `stop_reason` ([`domain/investigation_state.py:67-89`](../../../backend/src/cti_app/domain/investigation_state.py)); la ligne SQL vérifie l’alignement des colonnes et du payload JSON ([`models/investigation.py:677-718`](../../../backend/src/cti_app/infrastructure/database/models/investigation.py)).

Les réservations normalisées existent dans `investigation_budget_reservations`, PK `(investigation_id, operation_key)`, FK restrictive vers le registre de budget, statut et séquence contrôlés ; les montants sont dans `investigation_budget_reservation_amounts`, PK `(investigation_id, operation_key, dimension)` avec FK composite et CHECK de dimension/montants ([`models/investigation.py:585-675`](../../../backend/src/cti_app/infrastructure/database/models/investigation.py)). Le contrat de consume doit relier ces lignes au dispatch ; `reserved_budget` de l’invocation seul n’est pas une preuve que la réservation courante est encore `RESERVED`.

Pour le chemin Bridge d’investigation, le workflow calcule une reservation key déterministe depuis investigation, index de tour, hash de contexte et numéro d’essai, puis réserve `BRIDGE_TURNS`, contexte octets/tokens avant l’appel ([`application/investigation_service.py:626-659`](../../../backend/src/cti_app/application/investigation_service.py)). `InvestigationWorkingState` conserve un hash/context pending et un index, mais aucune FK vers le tour ou run créé ensuite.

### 2.3 `ModelConversationTurn` et `ModelRun`

**VERIFIED.** `ModelConversationTurn` est une entité générique, pas une table Bridge spécifique. Le domaine porte conversation, séquence, `model_run_id`, ref/hash d’entrée, clé d’idempotence, parent, ref/hash de sortie, statut, `external_turn_id`, erreurs et correlation ID ([`domain/model_conversations.py:152-181`](../../../backend/src/cti_app/domain/model_conversations.py)). Statuts : `RUNNING`, `SUCCEEDED`, `FAILED`, `NEEDS_REVIEW`, `BLOCKED`; un résultat incertain en `RUNNING` devient `NEEDS_REVIEW` ([`domain/model_conversations.py:239-261`](../../../backend/src/cti_app/domain/model_conversations.py)).

`model_conversation_turns` a FK restrictive vers conversation, run et parent ; unicités conversation/séquence, run, clé d’idempotence ; hash d’entrée/sortie SHA-256, statut fermé, et index unique partiel « un RUNNING par conversation » ([`models/model_execution.py:196-247`](../../../backend/src/cti_app/infrastructure/database/models/model_execution.py)). Elle n’a ni `investigation_id`, ni reservation key, ni invocation initiatrice, ni version de ligne. `model_conversations` relie éventuellement une édition et un sujet mais pas une investigation ([`models/model_execution.py:143-193`](../../../backend/src/cti_app/infrastructure/database/models/model_execution.py)).

`ModelRun` conserve provider/backend/transport/role, hashes d’entrée, paramètres, statut, `submission_state`, `submission_attempt`, `bridge_request_id`, `response_id`, sorties et erreurs. Le CHECK SQL ferme provider/backend/transport/role/status/submission state, non-négativité des tentatives et hashes ([`models/model_execution.py:39-123`](../../../backend/src/cti_app/infrastructure/database/models/model_execution.py)). Il n’a pas de FK vers investigation ni vers tour ; la relation un-à-un depuis le tour est portée par `model_conversation_turns.model_run_id`.

État modèle : `ModelSubmissionState` distingue `NOT_SUBMITTED`, `EXTERNAL_STATE_UNKNOWN`, `SUBMISSION_IN_PROGRESS`, `RESULT_OBTAINED`, `VERIFIED_TERMINAL_FAILURE` ([`domain/model_runs.py:46-54`](../../../backend/src/cti_app/domain/model_runs.py)). `begin_submission_attempt` exige run `RUNNING` et `NOT_SUBMITTED`, incrémente l’essai, puis met aujourd’hui `EXTERNAL_STATE_UNKNOWN` ([`domain/model_runs.py:451-463`](../../../backend/src/cti_app/domain/model_runs.py)). Les transitions de récupération permettent de réouvrir seulement après échec terminal explicitement vérifié ([`domain/model_runs.py:401-449`](../../../backend/src/cti_app/domain/model_runs.py)).

`ModelConversationService.add_turn` crée et committe le run `RUNNING/NOT_SUBMITTED` et le tour `RUNNING`, puis seulement après ce commit invoque `ModelGateway.execute` ([`application/model_conversations.py:1282-1305`](../../../backend/src/cti_app/application/model_conversations.py)). Une clé déjà liée à un tour `RUNNING` rend ce tour ; une clé terminée échoue ou rend le succès existant, sans nouvelle soumission ([`application/model_conversations.py:1106-1157`](../../../backend/src/cti_app/application/model_conversations.py)). Continuation/correction crée un nouveau tour avec parent/séquence et un nouveau run ; la clé doit être distincte ([`application/model_conversations.py:1221-1298`](../../../backend/src/cti_app/application/model_conversations.py)).

**VERIFIED — owners supplémentaires / limitation de portée.** `ModelGateway` est aussi utilisé hors conversation par extraction, discovery et workflows de production (exemples : [`production_extraction.py:1403-1405`](../../../backend/src/cti_app/application/production_extraction.py), [`discovery/service.py:126`](../../../backend/src/cti_app/application/discovery/service.py)). Ces appels ont un `ModelRun`, mais ne sont pas forcément rattachés à un `ModelConversationTurn` ou un `ToolInvocation`. Un contrat fermé invocation/Bridge-tour ne sait pas les autoriser. Ces routes doivent rester désactivées jusqu’à une relation owner explicite ou une décision qui élargit le contrat.

### 2.4 Chemin Advisor : double causalité non persistée

**VERIFIED.** `advisor.consult` est enregistré comme outil externe (`advisor-consult.v1`) avec coût Bridge/context ([`application/investigation_advisor_tools.py:3328-3405`](../../../backend/src/cti_app/application/investigation_advisor_tools.py)). L’adapter prépare/archive son prompt puis appelle `submit_bridge_consultation_once` ([`application/investigation_advisor_tools.py:741-778`](../../../backend/src/cti_app/application/investigation_advisor_tools.py)); la composition lui passe le `turn_transport` injecté ([`application/investigation_composition.py:1539-1559`](../../../backend/src/cti_app/application/investigation_composition.py)). Le transport usuel crée à son tour un `ModelConversationTurn` et un `ModelRun` via `ModelConversationService` ([`application/bridge_investigator.py:555-570,764-780`](../../../backend/src/cti_app/application/bridge_investigator.py)).

**VERIFIED.** La même soumission a donc une invocation outil appelante et un owner technique tour/run. Le lien invocation→tour n’est pas une FK actuelle ; il ne doit pas être inféré d’une clé ou d’un hash. Proposition : au point d’envoi ModelGateway, le root owner est `BridgeTurnOwner(turn_id, model_run_id)` et l’invocation est une FK `origin_invocation_id` de provenance/contrôle additionnel. Pour l’envoi direct VT/Shodan/etc., le root owner reste `InvocationOwner(invocation_id)`.

## 3. Classification actuelle et source unique proposée

**VERIFIED.** `ToolEffect` ne donne que les catégories larges `LOCAL_READ`, `EXTERNAL_READ`, `LOCAL_COMPUTE`, `ACQUIRE`, `EXTERNAL_ACTIVE`, `EXTERNAL_SUBMIT` ([`application/investigation_tools.py:39-48`](../../../backend/src/cti_app/application/investigation_tools.py)). `ToolDefinition` ne contient pas famille/provider/opération/route/codec ([`application/investigation_tools.py:150-159`](../../../backend/src/cti_app/application/investigation_tools.py)). Le registre est mutable (`register`/`unregister`) et indexé seulement par `(name, schema_version)` ([`application/investigation_tools.py:351-409`](../../../backend/src/cti_app/application/investigation_tools.py)). Le dispatcher applique des gates d’admission/politique avant `adapter.execute`; il ne donne pas un permis au dernier transport ([`application/investigation_tools.py:619-678,865-885`](../../../backend/src/cti_app/application/investigation_tools.py)).

Exemples de classifications dispersées : VirusTotal enregistre des noms d’outils `EXTERNAL_READ` ([`investigation_vt_tools.py:1684-1705`](../../../backend/src/cti_app/application/investigation_vt_tools.py)); Shodan encode la capacité dans le nom et classe `EXTERNAL_READ` ([`investigation_shodan_tools.py:1202-1222`](../../../backend/src/cti_app/application/investigation_shodan_tools.py)); Retrohunt distingue `.submit` comme `EXTERNAL_SUBMIT`, sinon `EXTERNAL_READ` ([`investigation_retrohunt_tools.py:81-104`](../../../backend/src/cti_app/application/investigation_retrohunt_tools.py)); acquisition encode `ACQUIRE` dans un de ses enregistrements ([`investigation_acquisition_tools.py:665-704`](../../../backend/src/cti_app/application/investigation_acquisition_tools.py)). L’advisor Bridge est un autre enregistrement d’outil. La composition conditionne les inscriptions sur les ports/capacités et accepte un `tool_registrar` additionnel ([`investigation_composition.py:1250-1297,1561-1562`](../../../backend/src/cti_app/application/investigation_composition.py)).

**VERIFIED.** Recherche du code S3 à la base : aucun symbole `route_id`, `PreparedExternalRequest`, `ExternalEffectDispatch`, `SendPermit` ou `send_one` n’existe sous `backend/src/cti_app`. Il n’existe donc pas encore de catalogue de routes ou de gate physique à inventorier.

**PROPOSITION — classification canonique.** Introduire un même type `ExternalOperationSpec`, présent sur chaque `ToolDefinition` externe et dans l’enregistrement des opérations ModelGateway/Bridge :

```python
@dataclass(frozen=True, slots=True)
class ExternalOperationSpec:
    operation_id: str                 # identifiant stable/versionné de l’opération
    family: ExternalFamily            # enum fermé : vt, shodan, retrohunt, acquisition,
                                      # advisor_bridge, investigation_bridge, model_gateway…
    provider: ProviderId
    route_id: RouteId                 # ID choisi par la composition, jamais le caller
    normalizer_version: str
    serializer_version: str
    tool_name: str | None = None      # présent uniquement pour owner invocation
    schema_version: str | None = None
```

Cette spec est l’unique déclaration de la relation outil/opération/famille/route. `ToolDefinition.external_operation` référence cette déclaration ; les opérations de ModelGateway enregistrent le même type dans le même registre d’opérations. La composition vérifie unicité et compatibilité puis construit une map immutable `route_id -> (normalizer, serializer, endpoint/transport permit-aware)`. La map de transport ne contient pas de seconde table `tool → family/provider`; elle n’autorise pas un caller à sélectionner une route. Après le dernier registrar, composition ferme le registre (`MappingProxyType`/équivalent) ; aucun `register` ou `unregister` runtime ne peut changer une route déjà composée. La composition de production démarre deny-all ; seuls les fakes de composition de test remplacent le transport, sans changer les checks de permis.

## 4. Contrat `PreparedExternalRequest`, owner, bindings et consume

### 4.1 Types publics proposés

`owner` est uniquement un locator. L’union publique est fermée et n’accepte aucun objet arbitraire :

```python
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NewType
from uuid import UUID

RouteId = NewType("RouteId", str)
ProviderId = NewType("ProviderId", str)  # composition accepte un set fermé

class ExternalFamily(StrEnum):
    VT = "vt"
    SHODAN = "shodan"
    RETROHUNT = "retrohunt"
    ACQUISITION = "acquisition"
    ADVISOR_BRIDGE = "advisor_bridge"
    INVESTIGATION_BRIDGE = "investigation_bridge"
    MODEL_GATEWAY = "model_gateway"

@dataclass(frozen=True, slots=True)
class InvocationOwner:
    invocation_id: UUID

@dataclass(frozen=True, slots=True)
class BridgeTurnOwner:
    turn_id: UUID
    model_run_id: UUID

ExternalEffectOwner = InvocationOwner | BridgeTurnOwner

class DispatchState(StrEnum):
    PREPARED = "PREPARED"
    CONSUMED = "CONSUMED"
    SUCCEEDED = "SUCCEEDED"
    CONFIRMED_NOT_SENT = "CONFIRMED_NOT_SENT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    DENIED = "DENIED"
    ABANDONED = "ABANDONED"
    RECONCILED_EXECUTED = "RECONCILED_EXECUTED"
    RECONCILED_NOT_EXECUTED = "RECONCILED_NOT_EXECUTED"

@dataclass(frozen=True, slots=True)
class PreparedExternalRequest:
    owner: ExternalEffectOwner       # locator seulement
    dispatch_id: UUID                # ligne durable PREPARED
    expected_dispatch_version: int
    investigation_id: UUID           # revendication à vérifier
    operation_id: str
    family: ExternalFamily           # revendication à comparer
    provider: ProviderId             # revendication à comparer
    tool_name: str | None            # revendication pour InvocationOwner
    schema_version: str | None       # revendication pour InvocationOwner
    reservation_operation_key: str
    route_id: str                    # revendication à comparer au registre composé
    route_catalog_version: str
    normalizer_version: str
    serializer_version: str
    expected_owner_version: int
    expected_submission_attempt: int | None
    origin_invocation_id: UUID | None # causalité advisor; pas un second root owner
    reserved_amounts: Mapping[BudgetDimension, int]
    occurrence_id: str
    parent_dispatch_id: UUID | None
    effect_key: str                  # claim stable incluant digest/version/occurrence
    semantic_digest: str
    request_artifact_ref: str         # octets préparés immuables
    request_body_sha256: str         # SHA-256 des bytes applicatifs sérialisés
    request_sha256: str              # digest méthode/cible/headers sûrs/body

@dataclass(frozen=True, slots=True)
class EffectOutcome:
    dispatch_id: UUID
    state: DispatchState
    response_artifact_ref: str | None
    response_sha256: str | None

@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    outcome: Literal["executed", "not_executed", "abandon"]
    actor_id: str
    reason: str
    expected_dispatch_version: int
    response_artifact_ref: str | None = None
    response_sha256: str | None = None
```

`PreparedExternalRequest` ne contient ni transport, ni credentials, ni booléen d’autorisation, ni décision G2/TLP. Tous ses identifiants et hashes sont non autoritatifs. Le final transport reçoit seulement un permis opaque privé : `SendPermit(dispatch_id, route_id, request_sha256, body_sha256, nonce)`. Pour vérifier les bytes réellement remis au socket, le serializer doit produire `body: bytes` une fois ; l’envoi transmet ces mêmes bytes (`content=body`), pas un objet `json=` que le client pourrait re-sérialiser. Le permis ne peut être consommé qu’une fois. Redirect, retry automatique, pagination HTTP et retry SDK sont désactivés au transport final ou remontés au coordinateur comme une nouvelle préparation/consommation.

Interface publique minimale :

```python
class ExternalEffectCoordinator(Protocol):
    async def send_one(self, prepared: PreparedExternalRequest) -> EffectOutcome: ...
    async def reconcile(self, dispatch_id: UUID, decision: ReconcileDecision) -> EffectOutcome: ...
```

Le coordinateur obtient son unique `PermitAwareTransport` depuis le catalogue de composition. Aucun `transport` ni objet d’autorisation n’apparaît dans la méthode publique. Les transports bruts restent internes à la composition et refusent toute invocation dépourvue de permis vérifié.

### 4.2 Invariants avant émission du permis

1. La composition résout `(route_id, route_catalog_version)` vers exactement une entrée figée. Entrée absente, inactive, version stale ou famille/provider non compatible : refus, dispatch non consommé, pas de permis.
2. Le coordinateur recharge l’enveloppe et l’état de travail actuels : même investigation, approbation encore valide, provider autorisé, TLP/`do_not_submit`/`external_llm_allowed`, budget et stop state compatibles. Un booléen passé par l’appelant ne remplace aucune de ces lignes.
3. `InvocationOwner`: la ligne existe dans l’investigation indiquée, est `RUNNING`, a la version attendue, et `tool_name`, `schema_version`, `operation_key`, paramètres normalisés et opération canonique correspondent à la préparation. Sinon owner stale/mismatch.
4. `BridgeTurnOwner`: le tour existe et est `RUNNING`, sa relation durable dit qu’il appartient à la même investigation, son `model_run_id` est exact, son hash d’entrée correspond, et le `ModelRun` est `RUNNING` avec l’état/tentative attendus. Pour l’advisor, la FK `origin_invocation_id` pointe vers l’invocation `RUNNING` et son outil/schema doivent être `advisor.consult` / `advisor-consult.v1`.
5. La réservation courante est relue par `(investigation_id, reservation_operation_key)`, verrouillée, toujours `RESERVED`, et les montants par dimension sont identiques au snapshot préparé. Pour un owner invocation, la key égale `ToolInvocation.operation_key`; pour Bridge, le tour stocke la key Bridge calculée par le workflow.
6. Le route/opération/famille/provider/outil-schema sont re-dérivés du registre canonique de composition après rechargement de l’owner. Les copies présentes dans l’objet préparé doivent toutes égaler cette dérivation.
7. Le blob de requête est relu, son SHA recalculé, puis les mêmes bytes sont donnés au transport final. Le digest archivé doit égaler le digest du permis au dernier point d’envoi.
8. L’UPDATE `PREPARED → CONSUMED` est un CAS. Un seul gagnant obtient un permis ; aucun retry ne peut reconstituer un permis d’une ligne déjà consommée.

### 4.3 Bindings SQL à ajouter (pseudo-SQL seulement)

Les références de migration ci-dessous décrivent des contraintes souhaitées, aucune DDL n’a été exécutée.

```sql
-- Compléter les clés composites nécessaires aux FK de dispatch.
ALTER TABLE tool_invocations
  ADD CONSTRAINT uq_invocation_external_owner
  UNIQUE (id, investigation_id, operation_key, tool_name, schema_version);
ALTER TABLE tool_invocations
  ADD CONSTRAINT uq_invocation_investigation_operation
  UNIQUE (id, investigation_id, operation_key),
  ADD CONSTRAINT uq_invocation_investigation
  UNIQUE (id, investigation_id);

-- Pour les tours créés pour un workflow d’investigation, persister l’association
-- qui est actuellement seulement encodée dans les clés/correlation IDs.
ALTER TABLE model_conversation_turns
  ADD COLUMN investigation_id uuid NULL REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
  ADD COLUMN reservation_operation_key varchar(255) NULL,
  ADD COLUMN origin_invocation_id uuid NULL,
  ADD COLUMN version bigint NOT NULL DEFAULT 1 CHECK (version >= 1);
ALTER TABLE model_conversation_turns
  ADD CONSTRAINT fk_turn_origin_invocation
    FOREIGN KEY (origin_invocation_id, investigation_id)
    REFERENCES tool_invocations(id, investigation_id),
  ADD CONSTRAINT fk_turn_budget_reservation
    FOREIGN KEY (investigation_id, reservation_operation_key)
    REFERENCES investigation_budget_reservations(investigation_id, operation_key);
ALTER TABLE model_conversation_turns
  ADD CONSTRAINT uq_turn_dispatch_owner
    UNIQUE (id, model_run_id, investigation_id, reservation_operation_key),
  ADD CONSTRAINT uq_turn_origin_owner
    UNIQUE (id, origin_invocation_id, investigation_id);
-- Le tour ModelGateway non Investigation reste nullable mais n'est pas
-- recevable par la route S3 d'investigation.

CREATE TABLE external_effect_dispatches (
  id uuid PRIMARY KEY,
  owner_kind text NOT NULL CHECK (owner_kind IN ('invocation','bridge_turn')),
  investigation_id uuid NOT NULL REFERENCES analyst_investigations(id) ON DELETE RESTRICT,
  tool_invocation_id uuid NULL,
  tool_name text NULL,
  schema_version text NULL,
  bridge_turn_id uuid NULL,
  model_run_id uuid NULL,
  origin_invocation_id uuid NULL,
  operation_id text NOT NULL,
  family text NOT NULL CHECK (family IN ('vt','shodan','retrohunt','acquisition',
    'advisor_bridge','investigation_bridge','model_gateway')),
  provider text NOT NULL,
  reservation_operation_key varchar(255) NOT NULL,
  route_id text NOT NULL,
  route_catalog_version text NOT NULL,
  normalizer_version text NOT NULL,
  serializer_version text NOT NULL,
  semantic_digest char(64) NOT NULL CHECK (semantic_digest ~ '^[0-9a-f]{64}$'),
  request_artifact_ref text NOT NULL,
  request_body_sha256 char(64) NOT NULL CHECK (request_body_sha256 ~ '^[0-9a-f]{64}$'),
  request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  effect_key char(64) NOT NULL CHECK (effect_key ~ '^[0-9a-f]{64}$'),
  occurrence_id text NOT NULL,
  parent_dispatch_id uuid NULL,
  physical_send_ordinal integer NOT NULL CHECK (physical_send_ordinal >= 1),
  expected_owner_version bigint NULL CHECK (expected_owner_version IS NULL OR expected_owner_version >= 1),
  expected_submission_attempt integer NULL
    CHECK (expected_submission_attempt IS NULL OR expected_submission_attempt >= 1),
  state text NOT NULL CHECK (state IN ('PREPARED','CONSUMED','SUCCEEDED',
    'CONFIRMED_NOT_SENT','NEEDS_REVIEW','DENIED','ABANDONED',
    'RECONCILED_EXECUTED','RECONCILED_NOT_EXECUTED')),
  submission_attempt integer NULL CHECK (submission_attempt IS NULL OR submission_attempt >= 1),
  version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
  created_at timestamptz NOT NULL,
  consumed_at timestamptz NULL,
  finished_at timestamptz NULL,
  UNIQUE (id, investigation_id),
  CHECK (provider <> ''),
  CHECK (route_id <> '' AND operation_id <> '' AND occurrence_id <> ''),
  CHECK ((owner_kind='invocation' AND tool_invocation_id IS NOT NULL
          AND tool_name IS NOT NULL AND schema_version IS NOT NULL
          AND bridge_turn_id IS NULL AND model_run_id IS NULL
          AND expected_owner_version IS NOT NULL AND expected_submission_attempt IS NULL)
      OR (owner_kind='bridge_turn' AND tool_invocation_id IS NULL
          AND tool_name IS NULL AND schema_version IS NULL
          AND bridge_turn_id IS NOT NULL AND model_run_id IS NOT NULL
          AND expected_owner_version IS NOT NULL AND expected_submission_attempt IS NOT NULL)),
  CHECK ((family='advisor_bridge' AND origin_invocation_id IS NOT NULL)
      OR (family<>'advisor_bridge' AND origin_invocation_id IS NULL)),
  FOREIGN KEY (tool_invocation_id, investigation_id, reservation_operation_key,
               tool_name, schema_version)
    REFERENCES tool_invocations(id, investigation_id, operation_key, tool_name, schema_version),
  FOREIGN KEY (bridge_turn_id, model_run_id, investigation_id,
               reservation_operation_key)
    REFERENCES model_conversation_turns(id, model_run_id, investigation_id,
                                        reservation_operation_key),
  FOREIGN KEY (bridge_turn_id, origin_invocation_id, investigation_id)
    REFERENCES model_conversation_turns(id, origin_invocation_id, investigation_id),
  FOREIGN KEY (investigation_id, reservation_operation_key)
    REFERENCES investigation_budget_reservations(investigation_id, operation_key),
  FOREIGN KEY (origin_invocation_id, investigation_id)
    REFERENCES tool_invocations(id, investigation_id),
  FOREIGN KEY (parent_dispatch_id, investigation_id)
    REFERENCES external_effect_dispatches(id, investigation_id)
);
CREATE TABLE external_effect_measurements (
  dispatch_id uuid NOT NULL REFERENCES external_effect_dispatches(id) ON DELETE RESTRICT,
  dimension text NOT NULL,
  measured_amount bigint NOT NULL CHECK (measured_amount >= 0),
  receipt_ref text NULL,
  PRIMARY KEY (dispatch_id, dimension)
);
CREATE TABLE external_effect_dispatch_reservation_amounts (
  dispatch_id uuid NOT NULL REFERENCES external_effect_dispatches(id) ON DELETE RESTRICT,
  dimension text NOT NULL,
  expected_reserved_amount bigint NOT NULL CHECK (expected_reserved_amount >= 0),
  PRIMARY KEY (dispatch_id, dimension)
);
CREATE TABLE external_effect_events (
  id uuid PRIMARY KEY,
  dispatch_id uuid NOT NULL REFERENCES external_effect_dispatches(id) ON DELETE RESTRICT,
  event text NOT NULL,
  event_at timestamptz NOT NULL,
  details jsonb NOT NULL CHECK (jsonb_typeof(details)='object')
);
CREATE UNIQUE INDEX uq_external_effect_dispatch_claim
  ON external_effect_dispatches(investigation_id, effect_key);
CREATE UNIQUE INDEX uq_external_effect_model_attempt
  ON external_effect_dispatches(model_run_id, submission_attempt)
  WHERE model_run_id IS NOT NULL AND submission_attempt IS NOT NULL;
```

**Ajustements requis au pseudo-DDL.** En SQL réel, chaque FK composite ci-dessus exige un `UNIQUE` parent sur exactement les colonnes référencées. Ajouter les `UNIQUE` exactes nécessaires sur `model_conversation_turns`, y compris `(id, origin_invocation_id, investigation_id)`, et une contrainte de cohérence pour que `origin_invocation_id` du dispatch égale celui du tour. Une opération de famille advisor impose `origin_invocation_id IS NOT NULL`; une branche Bridge d’investigation sans outil impose `NULL`. Les colonnes de la variante propriétaire absente restent `NULL` et ne doivent pas résoudre un FK composite partiellement nul. Le CHECK doit aussi imposer pour `owner_kind='invocation'` que la reservation key égale l’opération de l’invocation. Le claim unique `(investigation_id, effect_key)` empêche le même effet/occurrence d’être réémis ; les retries, pages et correctifs doivent recevoir une occurrence workflow nouvelle. L’ordinal physique sert à tracer/valider le send de cette occurrence, pas à permettre de la re-consommer. Pour la FK parent, ajouter `UNIQUE(id, investigation_id)` sur `external_effect_dispatches` et référencer cette paire, afin qu’un parent d’une autre investigation soit impossible. `external_effect_events` est append-only et le reçu de mesure est unique par `(dispatch_id, dimension)`. Le catalogue en mémoire n’est pas une FK SQL : la ligne conserve son `route_id` et ses versions, puis le consume les compare au catalogue figé du binaire.

### 4.4 Transactions de préparation et de consume

La préparation sérialise la requête une fois, archive ses bytes immuables, calcule digest sémantique et digest des bytes, puis insère une ligne `PREPARED`. Elle ne consomme rien et ne retourne aucun permis.

Pseudo-consume, avec l’ordre global de verrous de R2 complété pour la réservation normalisée :

```sql
BEGIN;
SELECT * FROM investigation_envelopes
 WHERE investigation_id=:i FOR UPDATE;
SELECT * FROM investigation_working_states
 WHERE investigation_id=:i FOR UPDATE;
-- verrouiller l'invocation si owner invocation ou invocation initiatrice;
SELECT * FROM tool_invocations WHERE id=:invocation_id FOR UPDATE;
SELECT * FROM investigation_budget_reservations
 WHERE investigation_id=:i AND operation_key=:reservation_key FOR UPDATE;
SELECT * FROM investigation_budget_reservation_amounts
 WHERE investigation_id=:i AND operation_key=:reservation_key
 ORDER BY dimension FOR UPDATE;
SELECT * FROM external_effect_dispatches WHERE id=:dispatch_id FOR UPDATE;
-- verrouiller l'attempt familiale s'il y en a un ; puis tour, puis run.
SELECT * FROM model_conversation_turns WHERE id=:turn_id FOR UPDATE;
SELECT * FROM model_runs WHERE id=:run_id FOR UPDATE;
-- revérifier en mémoire les invariants §§4.1–4.2 et le SHA du blob.
UPDATE external_effect_dispatches SET state='CONSUMED', consumed_at=now(),
       submission_attempt=:next_attempt, version=version+1
 WHERE id=:dispatch_id AND state='PREPARED' AND version=:expected_dispatch_version
   AND request_sha256=:expected_sha AND route_id=:resolved_route;
-- pour Bridge: dans la même transaction, RUNNING/NOT_SUBMITTED →
-- RUNNING/SUBMISSION_IN_PROGRESS et increment de submission_attempt;
-- le tour reste RUNNING. Pour invocation seule, il reste RUNNING/version exacte.
INSERT INTO external_effect_events(..., event='CONSUMED', ...);
COMMIT;
-- Créer/retourner le permis éphémère uniquement après le commit réussi.
```

L’ordre recommandé est enveloppe → working state → invocation/initiatrice → réservation et montants → dispatch → ligne familiale → `ModelConversationTurn` → `ModelRun`. Aucun verrou ne reste pendant réseau. Tous les chemins de révocation, completion, settlement, reconciliation et abandon doivent adopter cet ordre ; R7-S3-R2 relève que les chemins invocation/enveloppe actuels font l’inverse ([`R7_S3_REVIEW_R2.md:20-24`](../../mission03-architecture/_work/R7_S3_REVIEW_R2.md)). Après commit `CONSUMED`, perte d’acquittement = aucun permis reconstruit. Un sweeper ou réconciliateur CAS la ligne à `NEEDS_REVIEW` si l’issue ne peut pas être prouvée.

## 5. Handoff ModelGateway et Bridge

### 5.1 État constaté

**VERIFIED.** Dans `ModelGateway._execute`, le gateway accepte un run `RUNNING` uniquement si `submission_state=NOT_SUBMITTED`; sinon il le ferme pour revue. Il appelle ensuite `begin_submission_attempt`, sauvegarde et committe avant de quitter l’UOW ([`application/model_gateway.py:1051-1084,1139-1144`](../../../backend/src/cti_app/application/model_gateway.py)). La méthode de domaine met l’état à `EXTERNAL_STATE_UNKNOWN`, pas à `SUBMISSION_IN_PROGRESS` ([`domain/model_runs.py:451-463`](../../../backend/src/cti_app/domain/model_runs.py)). Puis le gateway fait son preflight et appelle l’adaptateur ([`application/model_gateway.py:1161-1170`](../../../backend/src/cti_app/application/model_gateway.py)). Le claim durable courant est donc avant adaptateur mais n’est pas lié atomiquement à un dispatch ou au corps exact.

Le code de transport actuellement lu fait un seul appel HTTP par `create` : Responses appelle `_request`, qui documente un HTTP attempt unique ([`integrations/models.py:93-144`](../../../backend/src/cti_app/integrations/models.py)); Chat Completions appelle `client.post` une fois ([`integrations/models.py:635-676`](../../../backend/src/cti_app/integrations/models.py)). Cela ne prouve pas que tous les clients injectés, transport Bridge UI, redirect handlers ou SDK externes ont zéro retry.

### 5.2 Transition proposée et propriétaire de chaque état

| Moment | Transaction / propriétaire de l’état | État durable | Règle de retry / réponse tardive |
|---|---|---|---|
| Création de la requête | `ModelConversationService` crée le run + tour, stocke l’entrée exacte et committe avant le gateway (`model_conversations.py:1282-1305`) | Tour `RUNNING`; run `RUNNING / NOT_SUBMITTED`; pas de dispatch consommé | Une même clé idempotente rend le tour existant. Toute correction/continuation a un nouveau tour, run, hash, clé et dispatch. |
| Préparation initiale | Coordinateur external-effects; aucune sortie réseau | Dispatch `PREPARED`, propriétaire/réservation/route/versions/digests liés | Pas de permis. Le status owner doit rester eligible au consume. |
| Consume d’un premier send | Transaction §4.4; le coordinateur possède l’atomique dispatch↔ModelRun | Dispatch `CONSUMED`; run reste `RUNNING`, `submission_state=SUBMISSION_IN_PROGRESS`, tentative incrémentée, ID de requête de cette tentative persisté; tour reste `RUNNING` | Le `ModelGateway` délègue le dernier envoi au coordinateur ; il ne fait pas un second begin-submission indépendant. |
| Envoi physique | Transport permit-aware de composition, sans verrou SQL | Pas de changement SQL avant réponse ; un permis lie dispatch, route et digest exact | Un seul `POST` par consume. Redirect, retry de client/SDK ou seconde requête physique exige un nouveau dispatch/consume. Aucun transport brut public. |
| Succès synchrone | CAS résultat dans l’UOW du run et du dispatch; finalisation du tour par `ModelConversationService` | Dispatch `SUCCEEDED`; run `RESULT_OBTAINED` puis terminal succès; tour stocke sortie/hash et devient `SUCCEEDED` | La réponse tardive attache au dispatch exact et au digest exact par CAS ; elle ne déclenche pas d’envoi. |
| Background / polling | Création `POST` terminée; chaque `adapter.resume`/GET est une opération route distincte, préparée et consommée séparément | Run `WAITING_BACKGROUND`; dispatch de création terminal; chaque poll a son propre dispatch/measurement | Poll/refresh ne réutilise pas le permis du POST. Si le run est en attente, pas de nouvelle création POST. Le code actuel appelle `adapter.resume` en polling (`model_gateway.py:830-875`). |
| Échec prouvé pré-soumission | Le transport/framework atteste qu’aucun envoi n’a eu lieu ; CAS dispatch et owner | Dispatch `CONFIRMED_NOT_SENT`; run peut redevenir `NOT_SUBMITTED`/`FAILED`; le tour ne devient retryable que sous preuve enregistrée | Retry = nouveau dispatch, occurrence stable/ordinal suivant et consume neuf. Ne pas réutiliser le permis/dispatch consommé. |
| Échec terminal explicitement confirmé par Bridge | Transaction marque le dispatch antérieur terminalement non exécuté, met le run à `VERIFIED_TERMINAL_FAILURE`, puis autorise la prochaine tentative | Nouvelle préparation / nouveau dispatch ; `ModelRun.submission_attempt` augmente seulement dans le consume de cette tentative | L’ID de requête change par tentative. La relance interne actuelle du gateway doit traverser le coordinateur. |
| Erreur incertaine, timeout/cancel après consume, acquittement perdu | Réconciliation CAS ; coordinateur et propriétaire Bridge | Dispatch `NEEDS_REVIEW`; run et tour `NEEDS_REVIEW` / état externe inconnu | Aucune nouvelle émission avec la même occurrence. Une réponse tardive exacte se rattache par CAS et peut aboutir à `RECONCILED_EXECUTED`; sinon revue humaine. |
| Réponse partielle ou réponse sans identité exacte | Coordinateur conserve refs/hash pour revue | `NEEDS_REVIEW`, jamais succès par défaut | Pas de réutilisation ni de retry automatique ; aucune promotion de contenu partiel comme résultat terminal. |

`SUBMISSION_IN_PROGRESS` est l’état proposé immédiatement après le consume commit : il signifie que le droit d’émettre cette requête exacte a été consommé et que son issue est attendue. Si le processus disparaît avant l’envoi, l’absence n’est pas prouvée par le seul état SQL ; la ligne passe à revue, sauf preuve locale/framework attestant qu’aucune écriture réseau n’a commencé. Le code actuel utilise `EXTERNAL_STATE_UNKNOWN` dès le claim, ce qui fusionne l’attente d’envoi et l’incertitude post-send ; le contrat S3 doit rendre la transition plus explicite.

### 5.3 Retry, correction, pagination et envois cachés

- **Retry `ModelGateway`. VERIFIED :** le loop offre jusqu’à `_BRIDGE_TERMINAL_FAILURE_RETRIES` essais et recommence après `verified_no_answer` d’un run Bridge terminal ([`model_gateway.py:56-59,1257-1264`](../../../backend/src/cti_app/application/model_gateway.py)). Une branche d’investigation utilise `stateless_external=True`, qui rend `conversation=None`; elle peut donc satisfaire le prédicat de retry ([`bridge_investigator.py:764-780`](../../../backend/src/cti_app/application/bridge_investigator.py), [`model_gateway.py:1570-1584`](../../../backend/src/cti_app/application/model_gateway.py)). Ce retry garde le `ModelRun`, change `bridge_request_id` et incrémente l’essai ([`model_gateway.py:1317-1343`](../../../backend/src/cti_app/application/model_gateway.py)). Le nouveau protocole doit créer un dispatch par tentative, sous le même tour/run, avec `(model_run_id, submission_attempt)` unique.
- **Retry Bridge pré-soumission. VERIFIED :** `submit_bridge_consultation_once` fait une première soumission, et n’effectue une seconde qu’après rate-limit pré-soumission prouvé, cooldown accepté et transport capable de `submit_with_attempt` ([`bridge_investigator.py:415-495`](../../../backend/src/cti_app/application/bridge_investigator.py)). Le transport de turn inclut `attempt` dans la clé, puis `add_turn` persiste le tour/run associé ([`bridge_investigator.py:248-269,642-659,764-780`](../../../backend/src/cti_app/application/bridge_investigator.py)). Chaque soumission a donc un nouveau dispatch ; la preuve `PRE_SUBMISSION` clôt uniquement le dispatch précédent.
- **Retry et correction de la boucle Bridge d’investigation. VERIFIED :** `BridgeInvestigator.turn` maintient une boucle de retry pré-soumission (jusqu’à une tentative rate-limit et les retries pré-soumission configurés), appelle `submit`/`submit_with_attempt`, puis calcule `retry_offset` dans la clé ([`bridge_investigator.py:1201-1230,1271-1340`](../../../backend/src/cti_app/application/bridge_investigator.py)). Une sortie reçue mais invalide peut produire jusqu’à `MAX_CORRECTIVE_ATTEMPTS` nouveaux contextes et prompts, avec hash/idempotency key recalculés ([`bridge_investigator.py:1086,1395-1433`](../../../backend/src/cti_app/application/bridge_investigator.py)). Les pré-soumission sans send restent `CONFIRMED_NOT_SENT`; chaque retry, correction et prompt révisé reçoit néanmoins un dispatch et un consume distincts. Les sorties invalides reçues restent liées au dispatch parent et ne sont pas un succès métier.
- **Correction/continuation. VERIFIED :** un tour de conversation est un nouveau run/tour avec `parent_turn_id` et `sequence`; clé identique avec autre corps est refusée ([`model_conversations.py:1115-1117,1221-1298`](../../../backend/src/cti_app/application/model_conversations.py)). Une correction est une nouvelle occurrence/enfant, jamais une mutation du dispatch consommé. Les enfants de batch advisor dérivent des idempotency keys nouvelles à partir de l’index et des unités ([`investigation_advisor_tools.py:1298-1317`](../../../backend/src/cti_app/application/investigation_advisor_tools.py)); chacun doit consommer distinctement.
- **Pagination. PROPOSITION :** chaque page/résultat de pagination porte un ordinal stable créé par le workflow, `parent_dispatch_id`, un digest exact et un dispatch distinct. La page 2 n’est préparée qu’après réception du curseur page 1. Un adaptateur ne peut pas boucler sur plusieurs pages sous un seul permis. Un poll de réponse background est également un nouvel envoi physique (`GET`), avec une opération/route de récupération séparée.
- **Preflight / redirect / SDK. VERIFIED + UNKNOWN :** le gateway exécute `preflight` avant `adapter.invoke` ([`model_gateway.py:1165-1170`](../../../backend/src/cti_app/application/model_gateway.py)); les adaptateurs délèguent parfois à `transport.preflight` ([`integrations/models.py:850-854,913-916`](../../../backend/src/cti_app/integrations/models.py)). Si ce preflight fait une requête, il doit être routé et consommé séparément, ou rendu local/config-only. Toute redirection, retry httpx/SDK ou envoi UI caché doit être désactivé ou recevoir un dispatch distinct. Le code lu ne prouve pas le comportement des ports/client injectés ni celui du runtime Bridge UI.

## 6. Oracles RED proposés (aucun exécuté)

Tous les appels sont à des fakes locaux. Les cas d’atomicité utilisent PostgreSQL jetable et deux sessions indépendantes comme demandé par la porte d’implémentation ; aucune base n’a été démarrée dans cette phase.

1. **Owner/route mismatch — fake + PostgreSQL.** Créer owner invocation A, réservation A et dispatch `PREPARED` pour `vt.file_report`; falsifier dans l’objet préparé le owner, investigation, operation key, outil/schema, famille, route Shodan ou serializer version. `send_one` recharge les lignes et refuse avant CAS `CONSUMED`; assertion : aucun permis et compteur fake à zéro. Paramétrer un mismatch à la fois pour rendre chaque binding observable.
2. **Route inactive — fake composition.** Composer le registre production deny-all et un registre de test où une seule route fake est activée. La route manquante/inactive/version périmée refuse avant consume ; aucun transport appelé. Vérifier aussi qu’un fake route ID ne peut pas être trouvé dans la composition production.
3. **Owner stale / réservation révoquée — PostgreSQL deux sessions + fake.** A insère `PREPARED` et committe owner `RUNNING` + réservation `RESERVED`, puis attend avant consume. B committe owner `NEEDS_REVIEW` (variante : réservation release ou envelope révoquée). A reprend : le reload verrouillé refuse, aucun dispatch consommé/permis, fake à zéro. Faire l’assertion sans deadlock et vérifier le CAS attendu.
4. **Entrée directe sans permis — fake transport final.** Appeler chaque entrée raw de transport/adaptateur directement, sans coordinateur ni permis ; attendre refus typé et zéro compteur réseau fake. Répéter avec permis copié, nonce déjà utilisé, route stale et digest body différent. L’API publique ne doit pas offrir un paramètre transport substituable.
5. **Retry interne/redirect/SDK — fake + PostgreSQL.** Faire simuler à l’adaptateur un échec Bridge terminal prouvé puis une deuxième tentative, et à l’autre variante un retry/redirect caché. Chaque sortie physique doit correspondre à une ligne consommée et un permis; soit le deuxième envoi reçoit un nouveau dispatch/ordinal, soit le transport l’arrête avec un appel fake au plus. Une réponse terminale confirmée peut ouvrir une nouvelle tentative ; erreur ambiguë, timeout ou absence d’acquittement ne le peut pas. Assert `(model_run_id, submission_attempt)` et les deux digests/IDs de requête.
6. **Ambiguïté / réponse tardive — fake + PostgreSQL deux sessions.** Le fake enregistre qu’il a reçu le corps puis coupe l’acquittement ; simuler crash/reprise, et une réponse tardive au premier dispatch. Le dispatch consommé passe à `NEEDS_REVIEW`, le run/tour aussi, et une seconde session ne peut obtenir un permis. La réponse tardive peut être attachée uniquement par CAS au dispatch/body hash exact ; elle ne produit aucun deuxième appel fake et ne réouvre pas une émission.

**Gate supplémentaire avant activation d’une famille :** tester à la fois le direct-entry zéro permis et l’ordre de verrous opposé signalé N1. Un fake seul ne clôt pas la transaction PostgreSQL. Aucun de ces oracles n’a été lancé pour ce rapport.

## 7. UNKNOWN et blocages résiduels

- **UNKNOWN — lien Bridge→investigation/invocation.** Le schéma actuel ne matérialise pas les relations proposées. Le tour obtient une clé et un correlation ID dérivés, mais aucune FK vers `analyst_investigations`, `tool_invocations` ou réservation. Il faut propager et persister ces IDs au moment où `ModelConversationTurn` est créé ; les reconstruire d’une chaîne ne satisfait pas le contrat.
- **UNKNOWN — portée du gate.** Plusieurs appels `ModelGateway` existent hors de la variante conversation. Ils ne peuvent pas être émis par `BridgeTurnOwner` sans FK de tour, et le contrat demandé ne fournit pas une troisième variante de propriétaire. Garder leurs routes deny-all tant que la portée d’implémentation n’est pas décidée.
- **UNKNOWN — sends exacts couverts.** Les transports HTTP montrés ont un appel explicite par méthode, mais comportement de clients injectés, redirect, retry implicite d’une dépendance, preflight et runtime Bridge UI n’a pas été démontré. Une revue des ports réellement composés et une interception au dernier point réseau sont nécessaires avant toute affirmation de couverture.
- **UNKNOWN — normalizer version / identité inter-version.** Le plan R2 inclut version de normalizer dans l’identité mais sa règle d’occurrence ne ferme pas le renvoi silencieux quand une version change ([`R7_S3_REVIEW_R2.md:32-34`](../../mission03-architecture/_work/R7_S3_REVIEW_R2.md)). Il faut une alias/règle d’équivalence revue ou un blocage cross-version ; un nouveau digest seul ne constitue pas une autorisation de réémission.
- **UNKNOWN — settlement acquisition.** Le contrat R2 laisse à documenter la réservation d’opération, le ledger VT-read/hit et le réemploi local ; ce rapport ne choisit pas leur comptabilité ([`DESIGN_OPTIONS.md:406-410`](../../mission03-architecture/DESIGN_OPTIONS.md)).
- **UNKNOWN — état réellement déployé.** Cette lecture n’a pas interrogé PostgreSQL ni comparé les migrations/données à l’ORM. Les contraintes décrites sont celles du code versionné à la base, pas une attestation d’environnement.
- **Hypothèse proposée, non prouvée :** seules des opérations liées à une investigation autorisée peuvent entrer dans ce premier gate. Toute route reste inactive par défaut et les autres familles refusent jusqu’à fermeture du gate S3 complet et autorisation séparée.

## Références de décision

- `DESIGN_OPTIONS.md §5.1.4, points 1–2 et 8` : route figée, permis au dernier mètre, activation deny-all.
- `IMPLEMENTATION_PLAN.md §5, L10–L12` : préparation, transaction de consume, entrée gateway obligatoire, tests PostgreSQL requis.
- `R7_S3_REVIEW_R2.md N2/N3, F1/F2` : binding owner/route, handoff ModelGateway, lock order et coverage des retries restent des gates.

**SHA-256 du rapport :** communiqué dans le retour de mission.
