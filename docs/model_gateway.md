# Passerelle de modèles OpenAI et Qwen

## Frontières

Les ports applicatifs `ResearchModel`, `StructuredExtractionModel`, `DraftingModel` et
`CriticModel` ne connaissent ni Responses API, ni Chat Completions, ni le SDK d'un
fournisseur. `ModelGateway` applique la politique, nettoie les entrées, crée le `ModelRun`,
appelle l'adaptateur choisi et stocke la sortie comme blob adressé par SHA-256.

Une sortie de modèle reste un artefact dérivé. Elle ne modifie pas une preuve, une attribution
ou une décision humaine et ne devient jamais l'état canonique d'un sujet.

## Routage

| Usage | Adaptateur par défaut |
| --- | --- |
| Recherche web | OpenAI via `chatgpt-bridge` |
| Structuration de la découverte | Qwen explicitement forcé |
| Regroupement ambigu | OpenAI via `chatgpt-bridge` |
| Synthèse premium et critique | OpenAI via `chatgpt-bridge` |
| Extraction volumique | Qwen |
| Brouillon standard ou contenu sensible | Qwen |

`MODEL_FORCE_ADAPTER=openai|qwen|fake` permet un forçage uniquement lorsque
`APP_ENV=development`. `auto` conserve la politique ci-dessus.

Chaque adaptateur expose `is_external`. ChatGPT est toujours externe, même si le premier saut
HTTP vise le bridge local. Qwen appartient explicitement à la frontière de confiance locale de
ce déploiement, quelle que soit la forme de son URL ; `QWEN_IS_EXTERNAL=true` permet de changer
cette décision sans modifier le domaine. Si l'adaptateur retenu est externe et que
`external_llm_allowed=false`, le run passe à `blocked` avant tout transport réseau.

Les requêtes n'acceptent que du texte et des métadonnées JSON. `bytes`, `bytearray` et
`memoryview` sont rejetés. Les secrets usuels, Bearer tokens, chemins internes et clés de
métadonnées sensibles sont retirés avant calcul du hash et avant appel.

## Responses API et bridge ChatGPT

Les adaptateurs construisent une requête selon les concepts de Responses API. Le transport
`ChatGPTBridgeTransport` la convertit ensuite vers le contrat honnête
`POST /v1/bridge/runs` : l'application ne suppose donc pas que l'interface web est l'API
OpenAI. La façade `/v1/responses` reste disponible uniquement pour les clients compatibles.

Une recherche demande `web_search=true` au bridge. Celui-ci active l'outil de recherche de
l'interface quand il peut le vérifier, et retombe sinon sur une instruction dans le prompt ;
`metadata.web_search_mode` dit laquelle des deux voies a été prise. Il ne prétend dans aucun
cas pouvoir reconstruire les appels d'outils natifs.

Le bridge accepte aussi `ui_model` et `profile`, réglages d'interface appliqués puis vérifiés
dans le DOM, et refuse le run quand la vérification échoue. L'application ne les utilise pas
encore : `requested_model` reste une étiquette de traçabilité, sans effet sur l'interface. La forme côté adaptateur reste `tools: [{"type": "web_search"}]`, conformément à la
`include: ["web_search_call.action.sources"]`, conformément à la
[documentation Web search](https://developers.openai.com/api/docs/guides/tools-web-search).

Les appels longs utilisent `background: true`. L'identifiant `resp_*` est conservé dans le
`ModelRun`; un job `model.openai.background.poll` appelle ensuite `GET /v1/responses/{id}`.
Il retry seulement tant que le statut est `queued` ou `in_progress`, conformément à la
[documentation Background mode](https://developers.openai.com/api/docs/guides/background).
Un futur transport direct vers OpenAI devra en plus tenir compte du fait que ce mode n'est pas
compatible Zero Data Retention, avant de l'autoriser pour une classification sensible.

`OpenAIStructuredAdapter` envoie `text.format.type=json_schema` avec `strict=true`, puis
normalise le schéma Pydantic vers le sous-ensemble strict (`required` et
`additionalProperties=false`), puis revalide malgré tout la réponse avec le modèle attendu.
Cette défense reste nécessaire pour le bridge et pour détecter toute incompatibilité
fournisseur. Voir la
[documentation Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Pour la découverte, Qwen reçoit plutôt un contrat compact versionné et
`response_format={"type":"json_object"}` ; le schéma Pydantic complet n'est jamais injecté dans
le prompt, mais reste la référence finale locale.
Les extractions structurées de fond sont refusées pour l'instant : reprendre un tel run exige
de persister l'identité du schéma, ce qui appartient à un incrément ultérieur.

### Limites assumées de `chatgpt-bridge`

Le bridge fournit le sous-ensemble `POST /v1/responses` et
`GET /v1/responses/{id}` pour compatibilité, ainsi que le contrat interne
`/v1/bridge/runs`. `GET /v1/bridge/capabilities` décrit les garanties réellement disponibles.
Il traduit ensuite la requête vers l'interface ChatGPT :

- il rapporte le libellé lu dans le sélecteur de modèle de l'interface (`metadata.model_source
  = ui_observed`), et retombe honnêtement sur `chatgpt-web` quand ce libellé n'est pas
  lisible. Ce libellé reste celui affiché par l'UI, pas le snapshot exact servi par OpenAI ;
- son usage est estimé ;
- il peut activer l'outil de recherche de l'interface et le vérifie
  (`metadata.web_search_mode = ui_tool`), sinon il retombe sur l'instruction dans le prompt
  (`prompt_instructed`) ; dans les deux cas il ne fabrique pas les objets sources natifs
  absents de l'interface ;
- le JSON Schema est injecté comme contrainte et validé par l'application, sans prétendre à
  une garantie native du bridge ;
- le contrat natif `/bridge/runs` possède un registre SQLite durable et déduplique sur l'UUID du
  `ModelRun`. Une exécution terminée survit au redémarrage ; une exécution interrompue échoue
  sans resoumission implicite. Seule la façade Responses historique garde un cache mémoire.

L'intégration est donc remplaçable par le service Responses officiel sans modifier les ports
métier.

## Table `model_runs`

Les échecs de transport typés conservent dans `error_details` uniquement le fournisseur, la
phase, le caractère retryable et le nombre de tentatives. La description publique reste dans
`error_message`; aucun secret ou contenu de requête n'est stocké dans ces champs.

| Groupe | Colonnes |
| --- | --- |
| Routage | `provider`, `model_role`, `requested_model`, `actual_model_version` |
| Prompt versionné | `prompt_template_id`, `prompt_template_version` |
| Preuves d'entrée | `authorized_input_hash`, `evidence_pack_hash` |
| Observabilité | `parameters`, `duration_ms`, `usage`, `status`, dates |
| Reprise | `response_id` unique |
| Sorties | `output_references`, références/hashes/tailles brut et normalisé |
| Parse | phase, versions sérialiseur/normalisation, transformations, ligne/colonne JSON |
| Validation | chemins/codes Pydantic, compteurs de citations et URLs |
| Erreur publique | `error_code`, `error_message` nettoyé |

Le texte du prompt, les preuves, les clés API et les réponses ne sont pas enregistrés dans la
table ni dans les logs. Les sorties complètes vivent dans `model-outputs/` sur le blob store.

## Variables d'environnement

| Variable | Usage |
| --- | --- |
| `OPENAI_BRIDGE_BASE_URL` | base `/v1` du bridge local |
| `OPENAI_BRIDGE_API_KEY` | clé Bearer optionnelle du bridge |
| `OPENAI_RESEARCH_MODEL` | nom configurable pour la recherche |
| `OPENAI_STRUCTURED_MODEL` | nom configurable pour l'extraction structurée |
| `OPENAI_DRAFTING_MODEL` | nom réservé aux futures synthèses premium |
| `OPENAI_CRITIC_MODEL` | nom réservé aux futures critiques |
| `QWEN_BASE_URL` | base du endpoint compatible Chat Completions |
| `QWEN_API_KEY` | clé du gateway, jamais versionnée |
| `QWEN_MODEL` | modèle demandé, par défaut `Qwen3-32B` |
| `QWEN_IS_EXTERNAL` | change explicitement la frontière de confiance Qwen |
| `MODEL_FORCE_ADAPTER` | `auto`, ou forçage de développement |
| `MODEL_REQUEST_TIMEOUT_SECONDS` | timeout HTTP borné |
| `DISCOVERY_CHATGPT_STRUCTURING_FALLBACK` | fallback explicite, désactivé par défaut |

Le `.env.example` pointe vers le gateway Qwen retenu. Placer la clé uniquement dans `.env` ou
un secret manager ; elle n'est jamais nécessaire pour les tests. La décision de confiance
actuelle conserve `QWEN_IS_EXTERNAL=false`.
