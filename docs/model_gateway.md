# Passerelle de modèles et routage par tâche

## Frontières

Les ports applicatifs `ResearchModel`, `StructuredExtractionModel`, `DraftingModel` et
`CriticModel` ne connaissent ni Responses API, ni Chat Completions, ni le SDK d'un
fournisseur. `ModelGateway` applique la politique, nettoie les entrées, crée le `ModelRun`,
appelle l'adaptateur choisi et stocke la sortie comme blob adressé par SHA-256.

Une sortie de modèle reste un artefact dérivé. Elle ne modifie pas une preuve, une attribution
ou une décision humaine et ne devient jamais l'état canonique d'un sujet.

## Routage

Le domaine sépare trois notions :

| Notion | Valeurs | Rôle |
| --- | --- | --- |
| `ModelProvider` | `openai`, `gemini`, `qwen`, `fake` | fournisseur réel du modèle |
| `ModelBackend` | `chatgpt_bridge`, `gemini_webai`, `qwen`, `fake` | backend choisi par le routeur |
| `ModelTransport` | `openai_responses`, `openai_chat_completions`, `fake` | protocole wire utilisé par AutoWork |

`OpenAI-compatible` décrit uniquement le protocole HTTP. Cela ne signifie pas que chaque
implémentation expose toutes les capacités OpenAI.

| Usage | Adaptateur par défaut |
| --- | --- |
| Recherche web | OpenAI via `chatgpt-bridge` |
| Structuration de la découverte | Qwen |
| Regroupement ambigu | OpenAI via `chatgpt-bridge` |
| Synthèse premium et critique | OpenAI via `chatgpt-bridge` |
| Enrichissement éditorial en blocs textuels | OpenAI via `chatgpt-bridge` |
| Extraction volumique | Qwen |
| Brouillon standard ou contenu sensible | Qwen |

Les variables `MODEL_ROUTE_<HINT>` configurent le mapping `ModelRoutingHint -> ModelBackend`.
`MODEL_ROUTE_EDITORIAL_ENRICHMENT` configure séparément le backend de l’enrichissement éditorial ;
il accepte `chatgpt_bridge`, `qwen` ou `fake` (Gemini WebAI n’a pas encore de contrat structured).

Sur `chatgpt_bridge`, le routeur choisit l’adaptateur selon le rôle **et** l’exigence de sortie
structurée : un `draft()` sans schéma utilise l’adaptateur drafting textuel, un `draft()` avec
`output_schema` utilise l’adaptateur structured qui garde `OPENAI_DRAFTING_MODEL`. SYNTHESIS et
EDITORIAL_ENRICHMENT utilisent le drafting textuel ; leurs blocs sont parsés puis validés
localement. Le bridge annonce
`structured_output=prompt_and_client_validation` : le contrat JSON est injecté dans le prompt et
validé côté AutoWork. `STRUCTURED_EXTRACTION` conserve `OPENAI_STRUCTURED_MODEL`.
L'extraction canonique des sources (production) n'utilise pas ce rôle : elle passe par `draft` en texte
libre (format compact `FACT`/`EVENT`/`IOC`/`RULE`/`UNCERTAINTIES`, parsé localement de façon tolérante),
car le bridge ne produit pas de JSON fiable (voir `docs/design/extraction-wire-format-recovery.md`).
`MODEL_FORCE_ADAPTER=chatgpt_bridge|gemini_webai|qwen|fake` permet un forçage uniquement lorsque
`APP_ENV=development`; `openai` et `gemini` restent des alias de compatibilité. `auto` conserve
la politique ci-dessus.

Capacités déclarées par AutoWork :

| Backend | web search | background | conversation | structured output |
| --- | ---: | ---: | ---: | ---: |
| `chatgpt_bridge` | oui | oui | oui | oui, contrat textuel et validation locale |
| `gemini_webai` | non | non | non | non (fail-closed) |
| `qwen` | non | non | non | oui, contrat Qwen et validation locale |
| `fake` | permissif pour les tests | oui | permissif | oui |

Le routage conceptuel est le suivant :

```text
Application
    |
    v
ModelGateway
    |
    +-- chatgpt_bridge -> OpenAI-compatible Responses -> ChatGPT UI
    +-- gemini_webai  -> OpenAI-compatible Chat Completions -> Gemini
    +-- qwen          -> OpenAI-compatible Chat Completions -> Qwen
    +-- fake          -> fake transport
```

Chaque adaptateur expose `is_external`. ChatGPT est toujours externe, même si le premier saut
HTTP vise un service Bridge externe. Qwen appartient explicitement à la frontière de confiance locale de
ce déploiement, quelle que soit la forme de son URL ; `QWEN_IS_EXTERNAL=true` permet de changer
cette décision sans modifier le domaine. Si l'adaptateur retenu est externe et que
`external_llm_allowed=false`, le run passe à `blocked` avant tout transport réseau.

Les requêtes n'acceptent que du texte et des métadonnées JSON. `bytes`, `bytearray` et
`memoryview` sont rejetés. Les secrets usuels, Bearer tokens, chemins internes et clés de
métadonnées sensibles sont retirés avant calcul du hash et avant appel.

### Recherche REFERENCES de Production

Dans AW-010, la recherche de références de Production passe directement par `ModelGateway` avec
le routage de recherche web (`ModelRole.RESEARCH`, `ModelRoutingHint.WEB_RESEARCH` et
`web_search=true`). Le stage ne dépend pas d'une conversation : il transmet une requête
stateless, et aucune conversation REFERENCES n'est une identité canonique ou un prérequis de
reprise. Le choix du backend reste celui du routeur, sans branche propre au fournisseur dans
REFERENCES. Si la politique interdit un modèle externe et que le backend retenu est externe, le
stage requiert une revue (`external_llm_blocked`).

La sortie brute conserve temporairement le format wire legacy requis par les consommateurs
actuels. L'artifact canonique reste `ProductionReferenceCorpusV1`; une réutilisation cross-run
est fondée sur le hash fonctionnel d'entrée, indépendamment de l'identité d'exécution. Le run de
modèle et ses identifiants de reprise restent dans leurs propres enregistrements de traçabilité.

### EXTRACTION archive-only et provider-agnostic

`EXTRACTION` ne collecte rien : il lit exclusivement les documents archivés référencés par
`ProductionReferenceCorpusV1` et envoie au modèle le contenu extrait de l'archive, jamais
l'instruction de rouvrir une URL. Le stage appelle `ModelGateway.extract(request, schema)` et
décrit seulement la capacité demandée : profil (`FULL`/`IOC_RULES`), TLP, `external_llm_allowed`,
`do_not_submit`, sensibilité, taille et `web_search=false`. Le domaine ne contient aucune branche
`provider == ...`, aucun nom de modèle et aucune URL de fournisseur : le `ModelRouter` choisit
l'adaptateur autorisé, et le même scénario fonctionnel produit le même contrat canonique
`ProductionExtractionV1` derrière deux adaptateurs différents.

Une source interdite à un provider externe ne lui est jamais soumise « pour terminer le lot » :
le router utilise un provider autorisé ou l'extraction de cette source échoue localement. Pour une
source `CORE`, cet échec bloque la progression vers Synthesis tant qu'aucune extraction conforme
n'existe ; pour `SUPPORTING`/`TECHNICAL`, il devient une omission contrôlée avec warning.

La sortie d'un modèle reste une proposition source-local. Le contrat canonique n'est construit
qu'après parsing, validation de schéma, vérification contre l'archive exacte (SHA-256 puis preuve
locale), attribution déterministe de provenance et agrégation. Le modèle ne fournit jamais
`source_document_id`, `subject_id`, `production_run_id`, `checkpoint_id`, `model_run_id`, ni
provenance interne : ces identités sont attachées par AutoWork.

Chaque appel porte une identité de `ModelRun` déterministe, fonction du run, de sa génération et
du travail demandé (contenu, profil, versions de prompt et de texte source, fragment). Un rejeu de
la même génération retrouve donc la réponse durable au lieu de resoumettre, ne resoumet qu’après
un échec prouvé avant envoi (`allow_failed_resubmit`), et une nouvelle génération obtient de
nouvelles identités. La validation du schéma est différée à la frontière d’extraction
(`defer_validation`) : une réponse hors schéma est un échec source-local, jamais une ambiguïté de
soumission.

Classification des erreurs : une connexion impossible avant envoi reste retryable ; un timeout
après envoi ou une réponse non réconciliée devient `NEEDS_REVIEW` avec identité de réconciliation
et sans replay automatique ; une réponse incompatible ou une preuve locale absente est un échec
source-local ; un SHA divergent du corpus est une erreur d'intégrité explicite, sans fallback Web.
Les versions `model_policy_version` et `routing_policy_version` font partie de l'identité
fonctionnelle des checkpoints `source_extractions` : une politique différente crée un nouveau
checkpoint au lieu de réutiliser silencieusement l'ancien.

### SYNTHESIS AW-012 : brouillon stateless

Synthesis envoie son pack d’évidence complet à `ModelGateway.draft` avec `web_search=false` et
sans contexte de conversation. Le gateway valide la sortie structurée `SynthesisProposalV1` ;
l’application vérifie ensuite son schéma métier, ses handles d’évidence et son ancrage dans
`ProductionExtractionV1` avant de construire `ProductionSynthesisV1`. Synthesis organise et rédige
les faits prouvés par Extraction ; elle ne cherche pas de faits nouveaux.

Un `ModelRun` durable et déterministe identifie la génération et porte la reprise ainsi que la
réconciliation de soumission. Seul un échec prouvé avant soumission peut être retenté avec cette
identité. Si la requête a probablement été soumise, elle passe en `NEEDS_REVIEW` sans replay
automatique. Une réponse structurée invalide passe aussi en revue : aucun échange de réparation
de format n’est ouvert. AW-012 retire l’identité de conversation fonctionnelle propre à Synthesis ;
cela ne supprime pas les capacités de conversation des autres usages de `ModelGateway`.

### EDITORIAL_ENRICHMENT AW-016 : propositions structurées

`EDITORIAL_ENRICHMENT` appelle `DraftingModel.draft` avec une proposition stricte
`EditorialEnrichmentProposalV1`. L’appel est stateless (`web_search=false`,
`conversation=None`, `background=false`) et utilise le routage dédié
`ModelRoutingHint.EDITORIAL_ENRICHMENT`, configuré par
`MODEL_ROUTE_EDITORIAL_ENRICHMENT`. L’application résout les handles de preuve exactement vers
`ExtractionEvidenceRefV1` et valide la proposition avant de persister le canonical.

Une requête possiblement soumise ou une sortie structurée invalide passe en `NEEDS_REVIEW` sans
replay automatique. Une proposition vide est un résultat éditorial valide ; une indisponibilité du
modèle ou une policy d’accès bloquante ne produit pas un enrichissement vide.

Le modèle ne génère ni langage de renderer ni figure source. `source_figures` reste vide dans
AW-016 ; l’inventaire des images sources relève d’AW-017.

## Responses API et bridge ChatGPT

Les adaptateurs construisent une requête Responses standard. `ChatGPTBridgeClient`, qui hérite
de `HttpResponsesTransport`, l'envoie vers `POST /v1/responses` et reprend un run avec
`GET /v1/responses/{id}`. Le champ `model` est une étiquette de traçabilité : AutoWork ne
l'utilise pas pour changer le sélecteur de modèle de l'interface.

Les endpoints `/v1/bridge/*` restent réservés aux capacités spécifiques du Bridge :
capabilities, visible recovery, release, archive/close et diagnostics/control. Les extensions
`bridge_profile`, `bridge_ui_model` et `bridge_recovery` sont conservées dans les payloads
Responses lorsqu'elles existent.

Une recherche demande `web_search=true` au bridge. Celui-ci active l'outil de recherche de
l'interface quand il peut le vérifier, et retombe sinon sur une instruction dans le prompt ;
`metadata.web_search_mode` dit laquelle des deux voies a été prise. Il ne prétend dans aucun
cas pouvoir reconstruire les appels d'outils natifs.

Le bridge accepte aussi `ui_model` et `profile`, réglages d'interface appliqués puis vérifiés
dans le DOM, et refuse le run quand la vérification échoue. Côté AutoWork, seul
`ConversationContext.ui_model` est transmis, comme `bridge_ui_model` ; `requested_model` (le
`model` OpenAI) reste une étiquette de traçabilité, sans effet sur l'interface. La forme côté adaptateur reste `tools: [{"type": "web_search"}]`, conformément à la
`include: ["web_search_call.action.sources"]`, conformément à la
[documentation Web search](https://developers.openai.com/api/docs/guides/tools-web-search).

Les appels longs utilisent `background: true`. L'identifiant `resp_*` est conservé dans le
`ModelRun`; un job `model.openai.background.poll` appelle ensuite `GET /v1/responses/{id}`.
Il retry seulement tant que le statut est `queued` ou `in_progress`, conformément à la
[documentation Background mode](https://developers.openai.com/api/docs/guides/background).
Un futur transport direct vers OpenAI devra en plus tenir compte du fait que ce mode n'est pas
compatible Zero Data Retention, avant de l'autoriser pour une classification sensible.

`OpenAIStructuredAdapter` ajoute un contrat textuel à l'entrée, puis revalide la réponse texte
avec le modèle Pydantic attendu. Le ChatGPT Bridge ne fournit pas de Structured Outputs natifs
OpenAI : ce chemin ne doit donc pas être décrit comme une garantie fournisseur de JSON Schema.
Qwen conserve son comportement : schéma JSON du modèle Pydantic injecté dans le prompt système,
`response_format={"type":"json_object"}` et validation locale finale. Gemini WebAI utilise le
même protocole HTTP Chat Completions mais reste textuel : il ne reçoit jamais `response_format`,
`text.format` ni `json_schema`. Aucun contrat structuré n'étant défini pour lui, il est
fail-closed : une route `STRUCTURED_EXTRACTION` vers `gemini_webai` échoue avec
`ModelCapabilityError` avant tout appel réseau et sans créer de `ModelRun`.
Les extractions structurées de fond sont refusées pour l'instant : reprendre un tel run exige
de persister l'identité du schéma, ce qui appartient à un incrément ultérieur.

### Limites assumées de `chatgpt-bridge`

Le bridge fournit le sous-ensemble `POST /v1/responses` et
`GET /v1/responses/{id}` pour le data plane, ainsi que le contrat interne
`/v1/bridge/*` pour le contrôle. `GET /v1/bridge/capabilities` décrit les garanties réellement disponibles.
Il traduit ensuite la requête vers l'interface ChatGPT :

- il rapporte le libellé lu dans le sélecteur de modèle de l'interface (`metadata.model_source
  = ui_observed`), et retombe honnêtement sur `chatgpt-web` quand ce libellé n'est pas
  lisible ; AutoWork enregistre alors `actual_model_version=None`, puisque `chatgpt-web`
  signale une absence d'observation. Le libellé observé reste celui affiché par l'UI, pas
  le snapshot exact servi par OpenAI ;
- son usage est estimé ;
- il peut activer l'outil de recherche de l'interface et le vérifie
  (`metadata.web_search_mode = ui_tool`), sinon il retombe sur l'instruction dans le prompt
  (`prompt_instructed`) ; dans les deux cas il ne fabrique pas les objets sources natifs
  absents de l'interface ;
- le contrat de structure est injecté comme instruction textuelle et validé par l'application,
  sans prétendre à une garantie native du bridge ;
- les contrôles de récupération du Bridge possèdent un registre SQLite durable et dédupliquent
  sur l'UUID du `ModelRun`. Une exécution terminée survit au redémarrage ; une exécution
  interrompue échoue sans resoumission implicite. Le data plane reste la façade Responses.

L'intégration est donc remplaçable par le service Responses officiel sans modifier les ports
métier.

### Erreurs Chat Completions (Qwen, Gemini WebAI)

`HttpChatCompletionsTransport` exige son `provider` et lève `ChatCompletionsTransportError` :
`provider`, `code`, `status_code`, `retryable`, `submission_state` et des diagnostics bornés
(code, type et paramètre provider, message nettoyé). Le prompt, les en-têtes `Authorization`, les
cookies et les secrets ne sont jamais conservés ; un message provider qui cite la requête est
écarté, tout comme les listes de validation FastAPI qui recopient l'entrée.

| Cas | `code` | retryable | `submission_state` |
| --- | --- | ---: | --- |
| connexion jamais ouverte | `provider_unreachable` | oui | `pre_submission` |
| timeout de lecture/écriture, coupure | `provider_timeout`, `provider_transport_error` | oui | inconnu |
| 400 | `provider_bad_request` | non | inconnu |
| 401 / 403 | `provider_auth_failed` | non | `pre_submission` |
| 404 | `provider_not_found` | non | `pre_submission` |
| 422 | `provider_validation_failed` | non | inconnu |
| 429 | `provider_rate_limited` | oui | inconnu |
| 5xx (504 : `provider_timeout`) | `provider_server_error` | oui | inconnu |
| 2xx sans JSON objet | `provider_protocol_error` | non | `post_submission` |

`pre_submission` n'est déclaré que lorsqu'il est prouvé : connexion jamais ouverte, refus
d'authentification ou ressource absente, ou `submission_state` explicite dans le contrat
d'erreur. Un 400 (filtre de contenu), un 422 ou un 429 (WebAI y mappe les limites d'usage
Gemini) peut suivre un prompt reçu : l'état reste inconnu et le `ModelRun` passe en
réconciliation, sans second POST implicite. Les diagnostics sont persistés dans
`error_details.bridge_diagnostics`.

## Table `model_runs`

Les échecs de transport typés conservent dans `error_details` uniquement le fournisseur, la
phase, le caractère retryable et le nombre de tentatives. La description publique reste dans
`error_message`; aucun secret ou contenu de requête n'est stocké dans ces champs.

| Groupe | Colonnes |
| --- | --- |
| Routage | `provider`, `backend`, `transport`, `model_role`, `requested_model`, `actual_model_version` |
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
| `OPENAI_BRIDGE_BASE_URL` | base `/v1` d'un service Bridge externe joignable par HTTP |
| `OPENAI_BRIDGE_API_KEY` | clé Bearer optionnelle du bridge |
| `OPENAI_RESEARCH_MODEL` | nom configurable pour la recherche |
| `OPENAI_STRUCTURED_MODEL` | nom configurable pour l'extraction structurée |
| `OPENAI_DRAFTING_MODEL` | nom réservé aux futures synthèses premium |
| `OPENAI_CRITIC_MODEL` | nom réservé aux futures critiques |
| `QWEN_BASE_URL` | base du endpoint compatible Chat Completions |
| `QWEN_API_KEY` | clé du gateway, jamais versionnée |
| `QWEN_MODEL` | modèle demandé, par défaut `Qwen3-32B` |
| `QWEN_IS_EXTERNAL` | change explicitement la frontière de confiance Qwen |
| `WEBAI_BASE_URL` | base `/v1` du gateway WebAI-to-API |
| `WEBAI_API_KEY` | clé Bearer optionnelle de WebAI |
| `WEBAI_MODEL` | identifiant Gemini demandé à WebAI, par défaut `gemini-3-flash` |
| `WEBAI_IS_EXTERNAL` | frontière de confiance WebAI, `true` par défaut |
| `MODEL_ROUTE_<HINT>` | backend choisi pour chaque type de tâche |
| `MODEL_ROUTE_EDITORIAL_ENRICHMENT` | backend dédié aux propositions d’enrichissement éditorial |
| `MODEL_FORCE_ADAPTER` | `auto`, ou forçage de développement |
| `MODEL_REQUEST_TIMEOUT_SECONDS` | timeout HTTP borné |
| `DISCOVERY_CHATGPT_STRUCTURING_FALLBACK` | fallback explicite, désactivé par défaut |

Le Bridge est une stack Docker séparée : AutoWork ne le construit, ne le démarre
ni ne dépend de lui pour démarrer. `backend`, `worker` et `job-recovery`
déclarent `host.docker.internal:host-gateway` et joignent par défaut
`http://host.docker.internal:8001/v1`. Sous Linux, cette adresse n'atteint pas
un port publié sur `127.0.0.1` : le Bridge doit être publié sur la passerelle
`docker0` (`BRIDGE_BIND_ADDRESS=172.17.0.1` dans son `.env`, invisible depuis le
LAN) ou sur `0.0.0.0` derrière un pare-feu. Hors loopback, le Bridge exige un
`BRIDGE_API_KEY` fort, que `OPENAI_BRIDGE_API_KEY` reprend côté AutoWork. Les
deux dépôts gardent chacun leur nom de variable.

Avec la stack MetaHarness (`MetaHarness-/Bridges`), utiliser l'override
`compose.models.yaml` : `backend`, `worker` et `job-recovery` rejoignent le réseau externe
`metaharness-models` et reçoivent `OPENAI_BRIDGE_BASE_URL=http://chatgpt-bridge:8001/v1` et
`WEBAI_BASE_URL=http://web_ai:6969/v1`, qui priment sur `.env` pour ces services. Le Bridge
reste publié sur loopback côté hôte ; `OPENAI_BRIDGE_API_KEY` doit égaler le `BRIDGE_API_KEY`
de la stack. Configuration effective :
`docker compose -f compose.yaml -f compose.models.yaml config`.

Chaque appel au Bridge est une seule tentative HTTP : une relance éventuelle relève du
`ModelGateway`, à partir de l'erreur typée et de son `submission_state`.

Le `.env.example` pointe vers le gateway Qwen retenu. Placer la clé uniquement dans `.env` ou
un secret manager ; elle n'est jamais nécessaire pour les tests. La décision de confiance
actuelle conserve `QWEN_IS_EXTERNAL=false`.

Le modèle WebAI reste celui fourni par la configuration (`gemini-3-flash` par défaut) ; le
catalogue `/v1/models` de WebAI n'est pas utilisé pour le remplacer.
