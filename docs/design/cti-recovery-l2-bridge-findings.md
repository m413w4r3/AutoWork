# CTI recovery L2a — Bridge findings

Date : 2 octobre 2026. Périmètre : adaptateur et persistance AutoWork ; aucun appel réel au
Bridge ou à un modèle n'a été effectué.

## Propriétaire du code Bridge/DOM

AutoWork ne contient que le client HTTP Bridge (`backend/src/cti_app/integrations/models.py`) et
sa composition. `compose.yaml` pointe vers un service Bridge séparé ; `docs/model_gateway.md`
identifie la stack externe `MetaHarness-/Bridges`. L'extraction DOM, la gestion des onglets et le
service worker Chrome sont donc détenus par ce dépôt externe. Aucun correctif DOM n'est inclus ici.

## Contrat de réconciliation et de retry

Avant le POST, AutoWork persiste sa clé d'idempotence exacte `bridge_request_id` dans `ModelRun`.
Si une réponse fournit un identifiant `resp_*`, AutoWork le conserve en plus. La réconciliation
utilise d'abord cet identifiant de réponse ; quand il manque, elle interroge la clé d'idempotence
exacte via `GET /v1/responses/{bridge_request_id}`. Cette identité survit aux redémarrages dans
`ModelRun` et dans l'identité typée de réconciliation Production. La clé seule ne sert pas à
ouvrir l'interface ou à récupérer un élément DOM visible.

Le contrat observé dans AutoWork ne prouve pas qu'un statut `failed` exclut toujours une réponse
récupérable. Pour pouvoir retenter un run stateless, le Bridge doit fournir simultanément le
statut terminal `failed`, l'identité exacte du run et `verified_no_answer: true`, avec la garantie
que cette tentative ne pourra plus produire de réponse. AutoWork rejette actuellement un `failed`
sans ce signal vers la réconciliation ; les retries automatiques demeurent bornés à deux
réémissions après une preuve vérifiée. Le Bridge ne doit pas être modifié par ce lot.

Le contrat externe reste à confirmer pour les deux points suivants :

- `GET /v1/responses/{idempotency_key}` doit renvoyer uniquement le résultat associé à cette clé ;
- `verified_no_answer: true` doit être une garantie terminale, stable après redémarrage, et non une
  simple interprétation d'une fermeture d'onglet ou d'une erreur d'extraction DOM.

Sans ces garanties, AutoWork conserve `external_state_unknown`, garde la requête pour diagnostic
et n'émet pas de nouveau POST.

## Causes et budgets

L'adaptateur préserve des codes distincts pour racines de réponse ambiguës
(`bridge_ambiguous_response_roots`), onglet fermé (`bridge_tab_closed`) et extension déconnectée
(`bridge_extension_disconnected`). Une expiration d'attente côté AutoWork est
`bridge_wait_budget_exceeded`. Ces diagnostics sont conservés sous
`ModelRun.error_details.diagnostic_code` même si `ModelRun.error_code` indique l'état de
réconciliation.

`OPENAI_BRIDGE_WAIT_TIMEOUT_SECONDS` borne l'attente HTTP synchrone ; sa valeur par défaut est
300 secondes. `OPENAI_BRIDGE_WAIT_TIMEOUT_RESEARCH_SECONDS` conserve 900 secondes pour le rôle
recherche. Les transports Qwen/WebAI utilisent `MODEL_REQUEST_TIMEOUT_SECONDS` (300 secondes) et
`MODEL_REQUEST_TIMEOUT_RESEARCH_SECONDS` (900 secondes). Les anciens noms globaux restent
acceptés.

`MODEL_BACKGROUND_WAIT_TIMEOUT_SECONDS` garde son nom historique et devient le plafond de
sécurité total, 5400 secondes par défaut. `MODEL_BACKGROUND_IDLE_TIMEOUT_SECONDS` (1200 secondes
par défaut) déclenche une revue si la progression Bridge n'a pas changé. Tant que le Bridge
rapporte `queued`/`running` et que cette progression évolue, le poll continue au-delà de l'ancien
plafond de 900 secondes ; une ancienne valeur configurée sous 5400 secondes est portée à 5400.
Les overrides optionnels
`MODEL_BACKGROUND_WAIT_TIMEOUT_RESEARCH_SECONDS` et
`MODEL_BACKGROUND_IDLE_TIMEOUT_RESEARCH_SECONDS` ciblent le rôle recherche et héritent des
valeurs globales lorsqu'ils sont absents. L'intervalle Discovery reste configurable par
`DISCOVERY_BRIDGE_POLL_INTERVAL_SECONDS`. Une expiration côté client ne démontre pas l'annulation
du travail par le Bridge.

La réconciliation n'autorise une nouvelle émission que sur l'identité exacte avec statut
terminal `failed` et `verified_no_answer: true`. Un 404, un état inconnu ou `failed` sans preuve
reste `external_state_unknown`. Après cette preuve, les étapes stateless peuvent être réémises au
plus deux fois pour l'extraction, la projection de pertinence, la synthèse et l'enrichissement
éditorial ; le compteur PostgreSQL dédié reste distinct du compteur de reprise générique.
`bridge_ui_timeout` garde son code diagnostic et utilise un backoff job de 300 secondes au départ,
plafonné à 1800 secondes.

Les événements locaux du run mentionnent un timeout, `ambiguous_response_roots`, une extension
déconnectée et une attente d'environ 44 min 40 s. Le journal examiné ne contient pas les réponses
HTTP brutes nécessaires pour associer chaque cause à son statut/corps Bridge. Ces causes HTTP/DOM
ne sont donc pas observées indépendamment et restent ouvertes pour le dépôt propriétaire du
Bridge. L'échec de collecte PDF n'est pas traité par L2a (il appartient à L2b).

## Diagnostic sans credentials

`GET /api/health/models` expose le routage actif, les backends configurés et les versions de code
API/worker. Il ne renvoie ni clés, ni URLs d'authentification, ni credentials. Chaque appel de
modèle effectue en plus un préflight de route et une sonde Bridge capabilities sans prompt, avec
cache positif de 15 secondes.
