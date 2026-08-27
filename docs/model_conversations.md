# Conversations modèles persistantes

Les conversations d’analyse sont un agrégat applicatif distinct de l’interface
ChatGPT. `ModelConversation` conserve l’identité, le provider, le transport, le
but, le rattachement édition/sujet, un `external_locator` **diagnostique
uniquement**, la tête et une version optimiste. `ModelConversationTurn`
conserve une séquence immutable, son parent, le `ModelRun`, les références de
blobs et leurs SHA-256, l’idempotence, le `correlation_id`, les dates et une
erreur typée éventuelle.

L’identité applicative (l’UUID `ModelConversation.id`) est logique ; elle ne
présuppose rien du navigateur. L’identité navigateur vivante — l’onglet Chrome
exact — n’est connue que de l’extension, jamais de l’application ni de la
base de données : voir « Routage ChatGPT » ci-dessous.

Les textes complets sont dans les buckets d’objets
`model-conversation-inputs` et `model-outputs`. Ils ne sont ni stockés dans les
logs, ni copiés dans les tables conversationnelles. La rétention applicative se
configure avec `MODEL_CONVERSATION_RETENTION_DAYS` (90 jours par défaut). Ce
paramètre prépare la politique de rétention ; aucun effacement distant ChatGPT
n’est effectué automatiquement.

## Contrats et politique

L’API expose :

- `POST /api/model-conversations` ;
- `GET /api/model-conversations` avec filtres `edition_id`, `subject_id`,
  `purpose`, `status` et `provider` ;
- `GET /api/model-conversations/{id}` ;
- `GET /api/model-conversations/{id}/turns` ;
- `POST /api/model-conversations/{id}/turns` ;
- `POST /api/model-conversations/{id}/archive` ;
- `POST /api/model-conversations/{id}/reconcile`.

Chaque tour exige un mode explicite et une clé d’idempotence. `fresh` ouvre une
conversation isolée. `continue` est limité à `analyst_assistance` et
`pivot_research`, exige une tête applicative (`head_turn_id`) et le tour
externe vérifié du parent (`external_turn_id`) — jamais un locator —, et n’est
actuellement effectif que pour les transports conversationnels. Le domaine est
prêt à recevoir un futur mode `fork`, mais ce mode n’est pas accepté par les
contrats.

`add_turn` accepte une `lifecycle_policy` (`ConversationPolicy.KEEP` par
défaut, ou `DELETE_ON_SUCCESS`) qui gouverne uniquement la session navigateur
Temporary Chat du transport `chatgpt_bridge` — jamais la persistance de
l’historique ChatGPT :

- **KEEP** : après un tour réussi, l’onglet Temporary Chat exact et son
  rattachement en `chrome.storage.session` restent vivants pour un futur
  `continue` (c’est le défaut de Q1/Q4, les conversations multi-tours).
- **DELETE_ON_SUCCESS** : après une opération bornée réussie, la sortie
  applicative est d’abord rendue durable, puis la `ModelConversation` est
  archivée, puis l’onglet Temporary Chat exact est fermé (best-effort — un
  échec de fermeture est journalisé, jamais réécrit sur une sortie déjà
  réussie).

Un archivage explicite (`ModelConversationService.archive` /
`POST .../archive`) marque d’abord la conversation `ARCHIVED`, puis ferme la
session bridge le cas échéant ; un appel répété est sûr et retente la
fermeture externe sans jamais réactiver la conversation.

Les rôles evidence-first existants — discovery, extraction structurée,
rédaction de brève/finale et critic — ne fournissent aucun contexte au gateway :
ils restent donc en `fresh`. Une sortie conversationnelle porte explicitement
`primary_evidence=false`. Le service ne possède aucune dépendance vers les
dépôts de claims, IOC ou evidence packs : extraction, provenance et validation
humaine restent obligatoires.

La décision `external_llm_allowed` est réévaluée à chaque tour. L’appelant doit
la calculer depuis la classification et la politique de diffusion du nouveau
contenu ; `false` bloque le ModelRun avant tout appel externe.

## Routage ChatGPT

Le bridge accepte un objet `conversation` avec `mode`, UUID applicatif
(`id`) et, pour `continue`, `expected_turn_id` — jamais de locator/URL en
entrée. Il retourne la même identité et le même mode sous
`metadata.conversation`, avec le tour externe (`turn_id`), `verified`,
`ephemeral`, et un `external_locator` **optionnel, diagnostique uniquement**
(il peut valoir la même URL Temporary Chat pour plusieurs conversations
distinctes simultanées).

L’identité navigateur vivante est :

    UUID de conversation applicative -> tab_id Chrome exact ->
    dernier tour assistant externe vérifié (expected_turn_id)

Toute conversation fraîche ouverte par le bridge est un ChatGPT *Temporary
Chat* (`https://chatgpt.com/?temporary-chat=true`), positivement confirmé
avant l’envoi du prompt. Pour `fresh`, l’extension ouvre toujours cette URL
canonique dans un onglet géré non actif. Pour `continue`, elle retrouve
l’onglet exact déjà lié en mémoire (`chrome.storage.session`, jamais
`chrome.storage.local`), exige que `expected_turn_id` corresponde au dernier
tour connu de cette session, et **ne crée jamais d’onglet de remplacement** —
ni une recherche par URL, onglet actif, titre, index DOM ou similarité
visuelle. Seules les origines HTTPS `chatgpt.com` et `chat.openai.com` sont
admises. Un onglet occupé n’est jamais navigué et le bridge conserve une
seule génération globale simultanée.

Une session live perdue — service worker relancé sans restaurer l’état côté
navigateur, onglet fermé manuellement, extension rechargée, navigateur
redémarré — n’est **jamais** reconstruite depuis l’historique ChatGPT ou une
URL : le bridge répond `conversation_unavailable`, de façon déterministe. Il
n’existe pas de pipeline de suppression/nettoyage séparé : fermer l’onglet
(Temporary Chat n’étant jamais écrit dans l’historique) est la totalité du
nettoyage — voir `chatgpt-bridge/AGENTS.md`, section « Ephemeral
conversations ».

Un retry du même ModelRun rejoint le journal SQLite du bridge et le même tour.
Après un clic au résultat incertain, le tour et la conversation passent en
`needs_review` ; une réconciliation explicite clôt le tour incertain sans le
resoumettre.

## Limites

Le transport `chatgpt_bridge` pilote une interface web dont les sélecteurs et le
cycle de navigation peuvent changer. Il ne fournit ni les garanties de l’API
Responses officielle, ni un identifiant de Conversation OpenAI natif. Le
transport `openai_responses` et le champ générique `external_id` permettent une
future intégration native sans dépendre d’une URL ChatGPT et sans réinjecter
l’historique. Cette intégration native et le mode `fork` ne font pas partie de
cet incrément.

## Vérification manuelle

```bash
docker compose up -d --build --wait
docker compose exec backend alembic current
curl -fsS -X POST http://127.0.0.1:8000/api/model-conversations \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openai","purpose":"analyst_assistance","subject_id":"<SUBJECT_UUID>","title":"Analyse A"}'
curl -fsS 'http://127.0.0.1:8000/api/model-conversations?subject_id=<SUBJECT_UUID>'
curl -fsS -X POST 'http://127.0.0.1:8000/api/model-conversations/<CONVERSATION_UUID>/turns?subject_id=<SUBJECT_UUID>' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Première question","mode":"fresh","external_llm_allowed":true,"idempotency_key":"manual-a1"}'
curl -fsS -X POST 'http://127.0.0.1:8000/api/model-conversations/<CONVERSATION_UUID>/turns?subject_id=<SUBJECT_UUID>' \
  -H 'Content-Type: application/json' \
  -d '{"message":"Question suivante","mode":"continue","external_llm_allowed":true,"idempotency_key":"manual-a2"}'
curl -fsS 'http://127.0.0.1:8000/api/model-conversations/<CONVERSATION_UUID>/turns?subject_id=<SUBJECT_UUID>'
curl -fsS -X POST 'http://127.0.0.1:8000/api/model-conversations/<CONVERSATION_UUID>/archive?subject_id=<SUBJECT_UUID>'
docker compose down
```
