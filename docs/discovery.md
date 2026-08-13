# Découverte CTI mensuelle

Le parcours comporte trois étapes visibles : recherche ChatGPT, analyse locale du rapport,
puis sélection éditoriale. Une recherche normale effectue un seul `POST /v1/bridge/runs` dans
une conversation `fresh`. Le bridge archive la réponse dans le `ModelRun` de recherche avant
que l'application ne la parse. L'API OpenAI officielle n'est pas utilisée.

## Prompt métier `monthly-cti-discovery` 4.0

Le prompt reçoit la date de recherche, la période demandée et la période réellement observable.
Cette dernière se termine à `min(period_end, as_of_date)` : aucune publication postérieure à la
date de recherche ne doit être recherchée. Le pays, ses alias dédupliqués, les langues
dédupliquées et l'axe complémentaire proviennent de l'édition canonique.

```text
Mission : rechercher les publications CTI significatives concernant
{country}{formatted_aliases}.

Date de recherche : {as_of_date}
Période demandée : {period_start} au {period_end}
Période observable : {period_start} au {observable_end}
Langues : {languages}
Axe complémentaire : {complementary_axis}

Ne recherche pas de publication postérieure à la date de recherche.

Priorise les activités APT étatiques ou supposées étatiques et les publications
techniques comportant des IOC, des échantillons, des configurations, une chaîne
d’infection, des outils, des TTP ou des règles de détection.

Propose tous les sujets significatifs retrouvés. Il n’existe aucune limite ni
quota de sujets, de brèves ou d’articles approfondis. La sélection finale sera
effectuée par un analyste humain.

Regroupe dans un même SUBJECT les publications décrivant manifestement la même
campagne, le même incident ou la même recherche.

Une synthèse mensuelle ou trimestrielle peut être liée à plusieurs SUBJECT.
Ne fusionne pas des campagnes différentes uniquement parce qu’elles sont
mentionnées dans la même synthèse.

Chaque SUBJECT doit normalement comporter au moins une publication dans la
période observable. Les publications antérieures peuvent être ajoutées comme
rapport original, analyse indépendante ou contexte technique.

Pour les IOC :

- signale uniquement les IOC explicitement visibles dans les pages consultées ;
- reproduis leurs valeurs exactes sans les corriger ni les compléter ;
- indique leur type lorsqu’il est identifiable ;
- distingue un total annoncé par l’éditeur des valeurs effectivement visibles ;
- n’estime jamais un nombre d’IOC ;
- utilise `unknown` si tu ne peux pas déterminer l’information ;
- utilise `none` seulement si la publication indique clairement qu’aucun IOC
  n’est fourni ou si son contenu visible permet de l’établir ;
- une URL normale de publication ou de navigation n’est pas un IOC ;
- un domaine d’éditeur ou de CDN n’est pas un IOC sauf s’il est explicitement
  présenté comme tel dans la source.

N’invente aucune URL, date, attribution, disponibilité d’artefact ou valeur d’IOC.

Retourne uniquement du Markdown, sans bloc de code et sans texte avant le titre.
N’échappe pas les tirets des noms de champs.
N’insère pas de citation Markdown dans les champs de description.
Toutes les URL de référence doivent apparaître dans un bloc PUBLICATION.

# SUJETS CANDIDATS

## SUBJECT S1

title: <intitulé proposé>
presentation: <deux phrases neutres maximum>
actor-campaign: <acteur ou campagne explicitement rapporté, sinon unknown>
technical-potential: <entier de 0 à 4>
technical-reason: <raison en une phrase>
artifacts: <liste parmi ioc, samples, configurations, pcap, yara, suricata, none, unknown>
uncertainty: <une ou deux incertitudes courtes>

### PUBLICATION P1

title: <titre exact>
url: <URL HTTP(S) exacte>
publisher: <éditeur ou unknown>
published-at: <YYYY-MM-DD ou unknown>
period: <in-period, outside-period ou unknown>
role: <primary, independent, relay, aggregator ou unknown>
ioc-visibility: <none, declared, visible ou unknown>
visible-ioc-types: <liste des types visibles ou none/unknown>
visible-iocs: <valeurs exactes explicitement visibles ou none/unknown>
publisher-ioc-count: <entier explicitement annoncé ou unknown>
ioc-note: <une phrase courte ou none>

### PUBLICATION P2

...

## SUBJECT S2

...

# LIMITES

<limites principales de la recherche et de l’accès aux sources>
```

L'instruction système sur le contenu web non fiable est ajoutée une seule fois par le bridge.

## Parsing et provenance

`chatgpt-markdown-v2` reconnaît les blocs `SUBJECT` et `PUBLICATION` sans tenir compte de la
casse, accepte les champs réordonnés, absents ou multilignes et tolère les espaces et petites
variations de ponctuation. Il normalise les clés avec tirets, underscores ou underscores
échappés et les valeurs d'énumération comme `in-period`, `in_period` ou `in\_period`. Les champs
inconnus produisent un avertissement et le bloc Markdown original est conservé, sans
déséchappement global des textes ni des URL.

Toutes les URLs HTTP(S) des champs, liens Markdown, chevrons, URLs nues et citations visibles
sont extraites. La valeur brute est conservée, `canonicalize_http_url` retire les paramètres de
tracking connus, puis la déduplication se fait uniquement par URL canonique. `source_ref` est
dérivé de façon déterministe de cette URL. Aucune URL n'est inventée ou réparée. Une publication
sans URL valide reste visible comme incomplète ; un sujet est sélectionnable dès qu'il possède
une URL valide et n'est pas marqué comme contexte.

Les dates ne sont acceptées qu'au format explicite `YYYY-MM-DD`. Les rôles, artefacts, potentiel
technique, comptes et valeurs IOC restent provisoires. Les valeurs explicitement visibles sont
conservées avec le statut `provisional_visible`, leur valeur brute, leur provenance et un type
déterministe proposé. Elles sont dédupliquées par valeur normalisée dans un sujet, mais ne sont
jamais validées, ajoutées à un Evidence Pack, utilisées pour une chasse ou exportées dans un
livrable final pendant la découverte.

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

La découverte et son retraitement n'appellent jamais Qwen : le rapport ChatGPT archivé est parsé
localement par `chatgpt-markdown-v2`. Le regroupement éditorial ne transforme pas une citation
orpheline en sujet et ne fusionne pas deux blocs `SUBJECT` distincts d'un même lot. Le board
présente tous les groupes sans quota ; l'analyste humain choisit librement `Brève`, `Article
approfondi + pivots`, `Ignorer` ou laisse le sujet à décider. Les décisions de fusion, séparation,
rejet et sélection restent append-only.
