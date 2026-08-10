# Conversations modèles persistantes

Les conversations d’analyse sont un agrégat applicatif distinct de l’interface
ChatGPT. `ModelConversation` conserve l’identité, le provider, le transport, le
but, le rattachement édition/sujet, le locator externe opaque, la tête et une
version optimiste. `ModelConversationTurn` conserve une séquence immutable, son
parent, le `ModelRun`, les références de blobs et leurs SHA-256, l’idempotence,
le `correlation_id`, les dates et une erreur typée éventuelle.

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
`pivot_research`, exige une tête et un locator vérifiés, et n’est actuellement
effectif que pour les transports conversationnels. Le domaine est prêt à
recevoir un futur mode `fork`, mais ce mode n’est pas accepté par les contrats.

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

Le bridge accepte un objet `conversation` avec `mode`, UUID applicatif et
`external_locator`. Il retourne les mêmes identité et mode sous
`metadata.conversation`, avec le locator opaque, le tour externe et `verified`.

L’extension conserve durablement la relation UUID → locator. Les identifiants
d’onglet et de fenêtre ne sont que des caches éphémères. Pour `fresh`, elle ouvre
un onglet géré non actif puis attend le locator attribué. Pour `continue`, elle
retrouve l’URL exacte ou l’ouvre dans un nouvel onglet, vérifie son chargement et
n’utilise jamais l’onglet actif comme repli. Seules les origines HTTPS
`chatgpt.com` et `chat.openai.com` sont admises. Un onglet occupé n’est jamais
navigué et le bridge conserve une seule génération globale simultanée.

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
