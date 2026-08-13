# Découverte CTI mensuelle

Le parcours comporte trois étapes visibles : recherche ChatGPT, analyse locale du rapport,
puis sélection éditoriale. Une recherche normale effectue un seul `POST /v1/bridge/runs` dans
une conversation `fresh`. Le bridge archive la réponse dans le `ModelRun` de recherche avant
que l'application ne la parse. L'API OpenAI officielle n'est pas utilisée.

## Prompt métier exact

Les paramètres entre accolades sont injectés depuis l'édition canonique :

```text
Mission : rechercher les publications CTI significatives concernant
{country} et ses alias {country_aliases}, publiées entre
{period_start} et {period_end}, dans les langues {languages}.

Axe complémentaire : {complementary_axis}

Priorise les activités APT étatiques ou supposées étatiques et les publications
techniques comportant des IOC, des échantillons, des configurations, une chaîne
d’infection ou des règles de détection.

Regroupe les publications qui décrivent manifestement la même campagne, le même
incident ou la même recherche. Distingue le rapport original, les analyses
réellement indépendantes et les simples reprises.

N’invente jamais une URL, une date, un nombre d’IOC ou une attribution.
Utilise `unknown` lorsqu’une information n’est pas disponible.

Les publications hors période peuvent être rattachées comme contexte à un sujet
dans la période. Elles ne doivent pas devenir seules un sujet candidat, sauf si
l’axe complémentaire le demande explicitement.

Ne présente pas les résultats comme exhaustifs.

Retourne uniquement un rapport Markdown utilisant autant que possible le format
suivant :

# SUJETS CANDIDATS

## SUBJECT S1

title: <intitulé proposé>
presentation: <deux phrases neutres maximum>
actor_or_campaign: <valeur explicite ou unknown>
technical_potential: <entier de 0 à 4>
technical_potential_reason: <une phrase>
artifacts: <liste parmi ioc, samples, configurations, pcap, yara, suricata, none, unknown>
uncertainties: <une ou deux incertitudes courtes>

### PUBLICATION P1

title: <titre>
url: <URL HTTP(S) exacte>
publisher: <entité éditrice ou unknown>
published_at: <YYYY-MM-DD ou unknown>
period_relation: <in_period, outside_period ou unknown>
source_role: <primary, independent, relay ou unknown>
ioc_presence: <none, declared, visible ou unknown>
ioc_declared_count: <entier ou unknown>
ioc_visible_count: <entier ou unknown>

### PUBLICATION P2

...

## SUBJECT S2

...

# LIMITES

<limites principales de la recherche>
```

L'instruction système sur le contenu web non fiable est ajoutée une seule fois par le bridge.

## Parsing et provenance

`chatgpt-markdown-v1` reconnaît les blocs `SUBJECT` et `PUBLICATION` sans tenir compte de la
casse, accepte les champs réordonnés, absents ou multilignes et tolère les espaces et petites
variations de ponctuation. Les champs inconnus produisent un avertissement et le bloc Markdown
original est conservé.

Toutes les URLs HTTP(S) des champs, liens Markdown, chevrons, URLs nues et citations visibles
sont extraites. La valeur brute est conservée, `canonicalize_http_url` retire les paramètres de
tracking connus, puis la déduplication se fait uniquement par URL canonique. `source_ref` est
dérivé de façon déterministe de cette URL. Aucune URL n'est inventée ou réparée. Une publication
sans URL valide reste visible comme incomplète ; un sujet est sélectionnable dès qu'il possède
une URL valide et n'est pas marqué comme contexte.

Les dates ne sont acceptées qu'au format explicite `YYYY-MM-DD`. Les rôles, artefacts, potentiel
technique et compteurs IOC restent provisoires. Aucune valeur IOC n'est extraite et aucun objet
`Indicator` n'est créé pendant la découverte.

Le parseur de compatibilité reconnaît les anciennes sections de niveau 2, les mentions de
publication dans la fenêtre et les zones `axe complémentaire`, `hors fenêtre`, `contexte` ou
`contrôle a posteriori`. Les sections hors fenêtre restent visibles comme contexte non
sélectionnable. Le rapport Iran archivé retrouve ainsi CYFIRMA et NCC Group sans nouvelle
recherche.

Le batch conserve le ModelRun ChatGPT, le SHA-256 du rapport, la version du parseur, son statut
et ses avertissements. Le champ historique `structuring_model_run_id` référence le même
ModelRun ChatGPT afin de préserver la lecture des anciennes lignes sans migration destructive ;
aucun ModelRun de structuration n'est créé.

## Reprise et sélection

`POST /api/editions/{edition_id}/discovery/reports/reprocess` relit le blob du ModelRun choisi et
crée un nouveau résultat de parsing. Il effectue zéro appel bridge et zéro appel Qwen. Le rapport
original n'est pas modifié. `GET /api/editions/{edition_id}/discovery/reports/{run_id}` permet de
le consulter.

Une relance web explicite envoie `confirm_new_research=true`, crée une nouvelle clé d'idempotence,
un nouveau ModelRun et une nouvelle conversation `fresh`, tout en conservant les rapports
précédents. L'interface demande confirmation avant cette action.

Les regroupements ambigus ne sont pas confiés à Qwen pendant discovery. Le board conserve les
décisions humaines append-only de fusion, séparation, rejet et sélection comme brève ou article
principal.
