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

`OPENAI_BRIDGE_WAIT_TIMEOUT_SECONDS` borne l'attente HTTP d'une génération synchrone ; sa valeur
par défaut est 900 secondes. `MODEL_BACKGROUND_WAIT_TIMEOUT_SECONDS` borne les reprises d'une
réponse de fond ; sa valeur par défaut est aussi 900 secondes. L'intervalle de poll Discovery
existant reste configurable par `DISCOVERY_BRIDGE_POLL_INTERVAL_SECONDS`. Une expiration côté
client ne démontre pas l'annulation du travail par le Bridge.

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
