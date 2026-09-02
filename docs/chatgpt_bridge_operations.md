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
| `BRIDGE_IDLE_TIMEOUT` | silence maximal de l’extension, 300 secondes par défaut |
| `BRIDGE_TOTAL_TIMEOUT` | génération complète, 3600 secondes par défaut |
| `BRIDGE_UI_TIMEOUT` | probe ou contrôle actif de l’UI, 30 secondes par défaut |
| `BRIDGE_UI_SNAPSHOT_STALE` | âge après lequel le dernier snapshot est périmé |
| `BRIDGE_SHUTDOWN_GRACE_SECONDS` | délai de drainage des runs, 20 secondes par défaut |
| `OPENAI_BRIDGE_CONNECT_TIMEOUT_SECONDS` | connexion applicative, 3 secondes |
| `OPENAI_BRIDGE_CAPABILITIES_TIMEOUT_SECONDS` | capabilities, au plus 2 secondes |
| `OPENAI_BRIDGE_MAX_ATTEMPTS` | tentatives réseau bornées avec clé stable |

### Quatre bornes indépendantes

Elles ne se remplacent pas et ne doivent jamais être confondues :

    watchdog avant le premier tour
        ≠ tour surveillé avec `.streaming-animation` active
        ≠ idle timeout serveur
        ≠ total timeout serveur

1. **Idle timeout réseau/extension** — `BRIDGE_IDLE_TIMEOUT` (300 s). Silence
   total de l’extension côté serveur : plus aucun paquet, heartbeat compris. Un
   heartbeat le réarme, parce qu’il prouve que l’extension et l’onglet vivent.
2. **Watchdog d’activité avant le premier tour assistant** — dans le content
   script, `FIRST_ASSISTANT_ACTIVITY_STALL_MS` (300 s). Il mesure l’activité
   *observable du DOM* : apparition, disparition, changement de signature ou
   d’état d’un signal Stop/reasoning/streaming. Un signal apparu puis
   strictement figé n’est **pas** de l’activité et n’en repousse pas l’échéance.
   Un heartbeat ne peut donc jamais masquer indéfiniment une UI bloquée : le
   heartbeat réarme la borne 1, jamais celle-ci. Cette borne reste volontairement
   locale : « aucun tour assistant n’apparaît jamais » doit échouer en
   `bridge_ui_timeout` sans attendre `BRIDGE_TOTAL_TIMEOUT`.
3. **Garde-fous du tour assistant surveillé** — `FINALIZATION_STALL_MS` (45 s)
   quand l’UI ne se dit plus active, et `WATCHED_TURN_ACTIVE_SIGNAL_STALL_MS`
   (300 s) quand elle se dit encore active alors que le texte ne bouge plus.

   **Exception `.streaming-animation`.** Quand ce détecteur-là est visible dans
   le périmètre du tour surveillé, la génération est active : le texte peut
   légitimement rester inchangé pendant plusieurs minutes de recherche
   approfondie (deux runs de production sont restés à ~30 caractères pendant
   300 003 ms et 352 002 ms, puis le même tour a rendu la réponse complète). La
   stabilité du texte ne prouve alors rien et ne produit **jamais**
   `active_signal_stalled` : le content script continue d’observer le même tour,
   continue ses heartbeats sans contenu, n’émet ni `done` ni `incomplete` et ne
   resoumet rien. La borne dure redevient la borne 4.

   `.result-streaming` et `[data-is-streaming='true']` gardent leur sémantique
   bornée : aucune preuve de production ne les montre longuement actifs sans
   mutation. `assistant_actions` reste le signal final le plus fort et finalise
   immédiatement, même si un signal d’activité est encore présent.
4. **Total generation timeout** — `BRIDGE_TOTAL_TIMEOUT` (3600 s). Plafond
   absolu d’une génération, quelle que soit l’activité observée. Une recherche
   approfondie ChatGPT dépasse couramment le quart d’heure : cette borne protège
   d’une génération réellement bloquée, elle n’arbitre pas la durée normale
   d’une recherche. `JOB_ACTOR_TIME_LIMIT_SECONDS` (4500 s) doit rester
   au-dessus pour laisser au worker le temps de parser et persister.

Aucune de ces bornes ne resoumet le prompt : elles terminent le run
(`bridge_timeout` ou `bridge_ui_timeout`) et laissent la réconciliation
explicite décider.

### Autonomie en arrière-plan

L’onglet de génération est créé volontairement inactif (`active: false`) et ne
doit **jamais** avoir besoin d’être focalisé pour qu’une réponse soit consommée.
Le focus reste un outil de debug humain, jamais un mécanisme de complétion.

Chrome ralentit les minuteries d’une page masquée : ~1 s en arrière-plan, puis
au plus **une exécution par minute** au-delà de cinq minutes cachées
(*intensive throttling*). Une boucle d’observation uniquement minutée en subit
deux conséquences, qu’il faut distinguer :

- **latence** — constater une fin déjà rendue pouvait prendre deux réveils
  minutés, soit jusqu’à ~2 minutes. C’est légitime, borné, et invisible pour le
  résultat ;
- **correction** — un unique réveil throttlé faisait bondir `stable_for_ms` de 0
  à 60 000 ms, donc au-delà de `FINALIZATION_STALL_MS` (45 s) *à la première
  observation qui suivait la fin*. Une réponse parfaitement terminée
  (`completion_signal=assistant_actions`) partait alors en
  `incomplete/finalization_stalled` au lieu d’un `done`. C’est le seul défaut de
  correction imputable à l’arrière-plan, et il est corrigé.

Trois protections indépendantes, aucune ne pouvant conclure seule :

1. **MutationObserver** (content script) — réveille la boucle d’observation dès
   qu’un nœud, un texte ou un attribut surveillé change. Les callbacks
   d’observateur ne sont pas soumis au throttling des minuteries. La portée est
   la racine du document (React remplace le tour surveillé), compensée par un
   filtre d’attributs fermé et un callback trivial. Les observateurs sont
   déconnectés à la fin de chaque job (`disconnectDomWatchers()`), jamais
   partagés entre deux runs.
2. **`observe_tick`** (service worker → onglet exact) — cadencé par le ping du
   serveur (`KEEPALIVE_INTERVAL`, 20 s), donc par une horloge extérieure à la
   page. Le tick ne fait que **réveiller** la boucle du run exact : il n’émet ni
   heartbeat ni `done` et ne peut donc jamais prétendre que l’observateur DOM
   est vivant. Le content script reste seul auteur de la liveness et de la fin.
3. **Minuterie `POLL_MS`** — repli borné, throttlé, jamais supprimé. Sans ping
   serveur et sans mutation, la boucle continue de tourner (au pire une fois par
   minute), donc les heartbeats continuent : ~60 s au pire face à
   `BRIDGE_IDLE_TIMEOUT` (300 s), soit une marge de 5×. **Ne pas descendre
   `BRIDGE_IDLE_TIMEOUT` sous 120 s**, et ne jamais l’augmenter pour masquer un
   problème d’observation.

`MIN_STALL_OBSERVATIONS` (3) complète ces bornes : un verdict de « figé »
(`finalization_stalled`, `active_signal_stalled`, watchdog du premier tour)
exige désormais une durée longue **et** plusieurs observations réelles. Un seul
réveil tardif n’est pas la preuve que la boucle n’a jamais conclu.

**Déchargement d’onglet (*discard*).** Pendant tout un run lié, l’onglet exact
est marqué `autoDiscardable = false` (jamais activé, jamais focalisé), et il le
reste tant qu’une conversation live (KEEP) ou une target y est liée. Si Chrome
le décharge malgré tout, `chrome.tabs.onUpdated` le détecte et le run échoue de
façon typée et fermée : `bridge_extension_disconnected`,
`submission_state=post_submission`, `retryable=false`, `tab_state.discarded=true`
dans les diagnostics. La target exacte est conservée pour une recovery
explicite — aucune resoumission, aucun onglet de remplacement, aucune
conversation reconstruite.

**Ce qui reste interdit** comme chemin de complétion :
`chrome.tabs.update(tabId, { active: true })`, `window.focus()`, un clic
synthétique dans la page ChatGPT, ou tout changement de fenêtre active.

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

## Test manuel d’autonomie (onglet jamais focalisé)

Procédure de reproduction et de preuve. Elle doit permettre de trancher
objectivement entre « a exigé un focus humain » et « terminé masqué ».

1. `make up`, puis vérifier le pont : `make bridge-status`.
2. Recharger l’extension dans Chrome et vérifier la version du content script :
   dans la console de l’onglet ChatGPT, la ligne
   `🔌 ChatGPT Mini-Bridge : content script prêt — version 30`. Une version plus
   ancienne signifie que Chrome sert encore le code précédent.
3. Lancer une production normale (deux articles, recherche approfondie).
4. **Ne toucher à aucun onglet ChatGPT** : ne pas cliquer dessus, ne pas le
   survoler, ne pas le mettre au premier plan. Travailler dans une autre
   application, idéalement dans une autre fenêtre, pour que la fenêtre du
   navigateur elle-même ne soit pas focalisée.
5. N’inspecter les logs qu’**après** la fin du run : `make bridge-logs`.

Ce que la télémétrie permet de vérifier, sans aucun contenu :

| Question | Où regarder |
| --- | --- |
| L’onglet est-il resté masqué ? | `bridge_run_autonomy … visibility_state=hidden` |
| Est-il resté sans focus ? | `has_focus=False`, `focus_gains=0`, `visible_transitions=0` |
| Un focus humain a-t-il précédé la détection ? | `focus_gains`/`visible_transitions` > 0 sur le `done` |
| Quand le DOM final est-il apparu ? | `ms_since_dom_mutation` sur le `done` (recul depuis la dernière mutation) |
| Comment la fin a-t-elle été détectée ? | `wake_mutation` / `wake_tick` / `wake_timer` |
| L’onglet a-t-il été déchargé ? | `tab_state.discarded=true` (console du service worker, ou diagnostics de l’erreur `bridge_extension_disconnected`) |
| L’onglet est-il resté en arrière-plan ? | `bridge_run_phase phase=bound_tab_state` (console du service worker) : `active=false`, `auto_discardable=false` |

Un `done` accompagné de `visibility_state=hidden`, `has_focus=False`,
`focus_gains=0` et `visible_transitions=0` **prouve** une complétion autonome :
la page n’est jamais repassée au premier plan avant que la fin ne soit
constatée. À l’inverse, `focus_gains>0` sur ce même `done` est la signature d’une
complétion qui a suivi une intervention humaine, et doit être traitée comme une
régression.

Les timeouts journalisent les mêmes champs (`bridge_idle_timeout` /
`bridge_total_timeout` portent `visibility_state`, `has_focus`, `focus_gains`,
`ms_since_dom_mutation`, `ms_since_heartbeat`), ce qui distingue un onglet
masqué mais sain d’un onglet gelé ou déchargé.

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
