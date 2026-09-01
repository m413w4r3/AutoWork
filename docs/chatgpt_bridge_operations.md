# Bridge ChatGPT durci

## Topologie recommandée

`chatgpt-bridge` est un service Compose sur le même réseau que `backend` et
`worker`. Ceux-ci utilisent `http://chatgpt-bridge:8001/v1`. Le seul port hôte
est publié sur `127.0.0.1:8001`; l’extension Chrome se connecte à
`ws://127.0.0.1:8001/ws` avec son jeton d’appairage.

`http://127.0.0.1:8001/v1` est exclusivement l’URL des clients exécutés sur
l’hôte. Elle ne doit jamais être placée dans l’environnement d’un conteneur.
Un `docker compose up -d worker` démarre également le bridge et attend sa
liveness ; l’absence de l’extension reste un état dégradé et n’empêche ni le
backend ni Qwen de fonctionner.

Deux secrets distincts sont obligatoires dès que le bridge écoute sur une
adresse non locale :

- `OPENAI_BRIDGE_API_KEY` / `BRIDGE_API_KEY` authentifie les requêtes HTTP ;
- `BRIDGE_WS_TOKEN` authentifie l’extension WebSocket.

Le serveur refuse les endpoints de pilotage HTTP sans `BRIDGE_API_KEY` lorsqu’il
écoute sur `0.0.0.0`, et refuse toujours `/ws` si le jeton WebSocket manque ou
ne concorde pas. Ne publiez jamais `8001:8001` sur toutes les interfaces.

L’alternative historique reste possible : bridge sur l’hôte, worker vers
`http://host.docker.internal:8001/v1`. Elle exige les deux secrets, une écoute
`0.0.0.0` pour être joignable depuis Docker et un pare-feu empêchant tout accès
extérieur au port 8001.

## Configuration

| Variable | Usage |
|---|---|
| `BRIDGE_API_KEY` | Bearer HTTP, identique à `OPENAI_BRIDGE_API_KEY` côté application |
| `BRIDGE_WS_TOKEN` | jeton distinct, saisi dans le popup de l’extension |
| `BRIDGE_RUN_DB` | SQLite durable, `/data/bridge-runs.sqlite3` dans Compose |
| `BRIDGE_RUN_RETENTION_SECONDS` | rétention des runs terminaux, 7 jours par défaut |
| `BRIDGE_RUN_CLEANUP_LIMIT` | nombre maximal de lignes supprimées par nettoyage |
| `BRIDGE_IDLE_TIMEOUT` | silence maximal de l’extension |
| `BRIDGE_TOTAL_TIMEOUT` | recherche complète, 900 secondes par défaut |
| `BRIDGE_UI_TIMEOUT` | probe ou contrôle actif de l’UI |
| `BRIDGE_UI_SNAPSHOT_STALE` | âge après lequel le dernier snapshot est périmé |
| `BRIDGE_SHUTDOWN_GRACE_SECONDS` | délai de drainage des runs, 20 secondes par défaut |
| `OPENAI_BRIDGE_CONNECT_TIMEOUT_SECONDS` | connexion applicative, 3 secondes |
| `OPENAI_BRIDGE_CAPABILITIES_TIMEOUT_SECONDS` | capabilities, au plus 2 secondes |
| `OPENAI_BRIDGE_MAX_ATTEMPTS` | tentatives réseau bornées avec clé stable |

Dans Chrome : charger `chatgpt-bridge/extension`, ouvrir le popup, saisir
`ws://127.0.0.1:8001/ws` et `BRIDGE_WS_TOKEN`, puis reconnecter. Le jeton est
conservé dans `chrome.storage.local`; il n’est ni affiché dans le statut ni écrit
dans les logs.

## Sémantique d’idempotence

`POST /v1/bridge/runs` accepte `X-Idempotency-Key` et `request_id`. Lorsqu’ils
sont tous deux présents, ils doivent être identiques. L’application emploie
l’UUID du `ModelRun`, créé une seule fois avant l’appel réseau.

Le hash SHA-256 canonique couvre le payload JSON hors `request_id`. La table
SQLite associe atomiquement clé, hash, `bridge_run_id`, état, timestamps et
réponse ou erreur finale.

Avec `background=true`, le POST crée ou retrouve ce run puis retourne
immédiatement son identifiant et l'état `queued`/`running`. Une unique tâche
détachée pilote l'extension et écrit le snapshot final dans SQLite. Le client
interroge `GET /v1/bridge/runs/{id}` jusqu'à `completed` ou `failed` ; les
heartbeats de l'extension ne sont jamais concaténés au contenu final.

- même clé et même payload : même run, jointure si en cours, replay si terminé ;
- même clé et payload différent : `409 bridge_payload_conflict` ;
- timeout ou déconnexion du client : la tâche bridge continue et le retry joint
  le run initial ;
- redémarrage après un run terminé : replay SQLite, sans interaction UI ;
- redémarrage pendant un run : échec sûr `bridge_server_error` marqué
  `submission_attempted`, sans nouvelle soumission implicite ; si l'onglet
  Temporary Chat exact est encore disponible, l'opérateur peut lancer la
  recovery visible, sinon il doit l'abandonner explicitement.

L’extension garde en plus les `request_id` dans `chrome.storage.local`. Le
background réserve l’ID avant tout envoi au content script, et le content script
le réserve avant toute manipulation du DOM. Un paquet ou événement WebSocket
dupliqué ne déclenche donc jamais un second clic.

## Capabilities et probe

`GET /v1/bridge/capabilities` ne contacte jamais l’extension. Il retourne les
capacités statiques, la connexion et le dernier snapshot UI avec `observed_at`,
`age_seconds` et `stale`; un snapshot absent n’est pas une erreur.

`GET /v1/bridge/capabilities?probe=true` est l’opération active. Elle peut ouvrir
les menus ChatGPT, attend le verrou de génération et renvoie une erreur typée
`bridge_ui_timeout` ou `bridge_extension_disconnected`.

## Matrice erreurs et retry

| Code | Retry | Action |
|---|---:|---|
| `bridge_unreachable` | oui | vérifier processus, DNS/réseau Compose |
| `bridge_timeout` | oui | vérifier génération et timeouts |
| `bridge_rate_limited` | oui | attendre `Retry-After` |
| `bridge_extension_disconnected` | oui | ouvrir ChatGPT et reconnecter l’extension |
| `bridge_ui_timeout` | oui | vérifier l’onglet et les sélecteurs UI |
| `bridge_server_error` | oui | consulter les logs avec le correlation ID |
| `bridge_auth_failed` | non | corriger/faire tourner le secret HTTP |
| `bridge_payload_conflict` | non | corriger la génération de clé |
| `bridge_protocol_error` | non | aligner versions/contrats |

Le champ `submission_state` prime le tableau : `pre_submission` peut être
retenté après nettoyage ; `submission_attempted` ou `post_submission` exige la
réconciliation du run exact avant toute autre action. Une recovery visible
réussie se confirme par le SHA-256 attendu, puis seulement la target exacte
peut être libérée.

Un POST n’est jamais retenté automatiquement sans clé stable. Avec une clé, le
transport applique un backoff borné avec jitter aux seules erreurs transitoires,
honore `Retry-After` et réutilise strictement la même clé.

## Diagnostic et observabilité

Rechercher dans les logs `bridge_run_id`, `correlation_id` ou l’empreinte courte
`idempotency_fingerprint`. Les événements donnent phase, durée, déduplication et
reconnexion. `GET /v1/bridge/metrics` expose des compteurs sans labels sensibles
(runs, déduplication, conflits, timeouts UI, reconnexions et activité). Les probes de santé réussies ne sont pas journalisées par le
backend. Aucun prompt, réponse, token, cookie ni clé brute ne doit être logué.

`/health` est une liveness rapide et reste à HTTP 200 sans extension. `/ready`
décrit séparément `server_operational`, la configuration HTTP/WebSocket,
l’accès SQLite et l’état `extension_absent` ou `extension_available`. Une
configuration incomplète et une extension absente répondent HTTP 503 sans
faire échouer le healthcheck Compose.

```bash
make status
make bridge-status
make bridge-logs
```

`make bridge-status` affiche health, ready et capabilities. Il construit
l’en-tête Bearer dans le conteneur et n’affiche jamais le secret.

## Cycle de vie et arrêt

`make up` démarre et attend la stack ; `make down` l’arrête sans supprimer les
volumes. `make restart-bridge` recrée uniquement le bridge. À la réception de
SIGTERM, le bridge refuse les nouveaux runs, draine ceux déjà engagés pendant
`BRIDGE_SHUTDOWN_GRACE_SECONDS`, marque le reliquat comme soumission ambiguë,
conserve sa target exacte pour une recovery explicite, puis ferme le WebSocket
et effectue le checkpoint SQLite. Au redémarrage, les états `queued` ou
`running` sont transformés en échec `submission_attempted` et ne sont jamais
resoumis.

## Test manuel de non-duplication

Utiliser uniquement la fausse extension, jamais une session ChatGPT réelle :

```bash
export OPENAI_BRIDGE_API_KEY='http-secret-local'
export BRIDGE_WS_TOKEN='ws-secret-local'
docker compose up -d --build chatgpt-bridge
BRIDGE_WS=ws://127.0.0.1:8001/ws \
  BRIDGE_WS_TOKEN="$BRIDGE_WS_TOKEN" \
  chatgpt-bridge/.venv/bin/python chatgpt-bridge/examples/fake_extension.py
```

Dans un autre terminal, envoyer deux fois exactement la même commande :

```bash
curl --max-time 0.01 -sS -X POST http://127.0.0.1:8001/v1/bridge/runs \
  -H "Authorization: Bearer $OPENAI_BRIDGE_API_KEY" \
  -H 'Content-Type: application/json' -H 'X-Idempotency-Key: manual-once-1' \
  -d '{"request_id":"manual-once-1","input":"test déterministe"}' || true
curl -sS -X POST http://127.0.0.1:8001/v1/bridge/runs \
  -H "Authorization: Bearer $OPENAI_BRIDGE_API_KEY" \
  -H 'Content-Type: application/json' -H 'X-Idempotency-Key: manual-once-1' \
  -d '{"request_id":"manual-once-1","input":"test déterministe"}'
```

La fausse extension doit imprimer une seule ligne `prompt reçu`; les deux appels
partagent le même `resp_*`.

Le smoke test Compose automatisé exécute la même garantie avec deux POST
concurrents et un replay :

```bash
OPENAI_BRIDGE_API_KEY='compose-http-test' BRIDGE_WS_TOKEN='compose-ws-test' \
  docker compose --profile bridge-test up --build --abort-on-container-exit \
  --exit-code-from bridge-smoke bridge-smoke
```

## Rotation des secrets et limites

Pour une rotation, générer deux nouvelles valeurs indépendantes, arrêter le
bridge, mettre à jour l’environnement Compose et le popup Chrome, puis redémarrer
et vérifier health/capabilities. Les anciennes connexions WebSocket sont ainsi
fermées. Ne mettez jamais les valeurs dans Git ou une commande conservée dans
l’historique partagé.

La génération reste synchrone côté HTTP dans cet incrément. Elle est détachée de
la connexion cliente et son résultat est durable, mais une exécution en cours ne
peut pas reprendre après l’arrêt du processus sans risquer un second clic. Le
bridge choisit alors l’échec sûr. Le cache OpenAI-compatible `/v1/responses`
historique reste mémoire seulement; l’application utilise `/v1/bridge/runs`.
