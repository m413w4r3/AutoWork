# Outils d’analyse locale de l’enquête

Le module `cti_app.application.investigation_local_tools` ajoute au
`ToolRegistry` le lancement de commandes déjà présentes dans
`LocalCommandCatalog` et cinq lectures Ghidra facultatives. Il ne modifie pas
la composition de l’enquête : le point d’extension `ToolRegistrar` fourni par
K1 peut appeler `register_local_analysis_tools`.

## Capacités et enregistrement

`register_local_analysis_tools` reçoit le registre, le
`LocalCommandService`, le `LocalCommandCatalog` partagé par ce service, le port
Ghidra en lecture seule, le lecteur d’octets, le dépôt des associations de
samples et l’`EvidenceRecorder`. Le lecteur d’octets accepte le port F1
`SampleBytesForAnalysisPort` ou le lecteur G2 `QuarantineRuleCorpusBytes`.
Le service local doit utiliser le même lecteur que celui passé au registrar.

`local_tool_capabilities(catalog, ghidra_enabled=...)` renvoie la clé
`local.run_command` seulement si le catalogue a au moins une commande installée
et versionnée. Le schéma expose exactement ces identifiants dans l’`enum`
`command_id`. Les outils Ghidra ont chacun leur clé `ghidra.*` lorsque
`ghidra_enabled` vaut `True`. Cette clé signifie que l’enquête peut tenter
l’opération ; elle ne sonde pas l’endpoint. Si Ghidra est désactivé, non
configuré, non confirmé ou hors ligne, son port rend `unavailable` et les
capacités locales restent utilisables.

## `local.run_command`

L’outil reçoit `command_id`, le SHA-256 d’un sample et un objet `options`.
L’identifiant doit appartenir au catalogue actif. Le service F1 vérifie la
whitelist d’options de cette commande, le type de fichier, le hash des octets,
la disponibilité du binaire et ses limites d’exécution. L’appel de l’outil
ajoute une vérification préalable : le hash doit être associé à l’enquête
courante dans `SampleAssociationRepository`. L’outil ne lance aucune
acquisition.

Le registrar réutilise le lecteur d’octets fourni par G2 pour Ghidra ; les
commandes locales utilisent le même lecteur déjà injecté dans
`LocalCommandService`. `QuarantineRuleCorpusBytes` vérifie le SHA-256, la taille
et les buckets qu’il sait lire. L’association empêche un hash provenant d’une
autre enquête d’être analysé.

Les commandes s’exécutent par le runner F1 avec `create_subprocess_exec`, un
nom de fichier interne fixe, des limites CPU/RAM/temps/sortie, un environnement
nettoyé et le filtre réseau prévu par F1. Aucune entrée Bridge ne devient un
nom de binaire, un chemin, un argument shell ou une option libre. Une option
hors whitelist est refusée par `LocalCommandService` avec `policy_denied`.

L’invocation réserve avant l’adaptateur le plafond de temps maximal estimé par
le catalogue parmi les commandes annoncées. Le dispatcher existant porte un
coût fixe par définition d’outil, donc cette réservation est conservatrice
pour les commandes plus rapides. Le résultat engage le temps réel arrondi à la
seconde et borné par la réservation.

## Outils Ghidra en lecture seule

| Outil | Entrées principales | Opération du port |
|---|---|---|
| `ghidra.list_functions` | sample SHA-256, limite | liste de fonctions |
| `ghidra.list_imports` | sample SHA-256, limite | liste d’imports |
| `ghidra.xrefs` | sample SHA-256, adresse, direction `to`/`from`, limite | XREF vers ou depuis une adresse |
| `ghidra.decompile_function` | sample SHA-256, une adresse | décompilation d’une fonction |
| `ghidra.search` | sample SHA-256, texte de recherche, limite | recherche localisée |

Les outils ne publient que `GhidraAnalysisPort.analyze` avec ces opérations.
Le MCP brut et les opérations d’écriture ne sont pas enregistrés. Avant un
appel, l’adaptateur vérifie l’association dans l’enquête et résout les octets
du sample pour confirmer le hash. Les sorties normalisées et les chaînes de
décompilation sont des données non fiables.

Les schémas sont en JSON Schema Draft 2020-12, refusent les propriétés
inconnues à leur niveau et utilisent uniquement les mots-clés compris par le
validateur du registre. Les requêtes et options sont bornées ; les champs
credential-like, valeurs de credential et URL signées sont refusés ou
assainis. Les réponses ne contiennent ni octets bruts ni secret.

## États, preuves et reprise

| Résultat local | Statut d’invocation |
|---|---|
| commande réussie | `succeeded` |
| sortie vide | `empty` |
| binaire, sample ou endpoint indisponible | `unavailable` |
| hash non associé, commande inconnue ou politique refusée | `policy_denied` |
| erreur d’exécution ou réponse Ghidra invalide | `failed` |
| délai expiré, sortie locale tronquée ou réponse Ghidra tronquée | `partial` |

Les délais et sorties plafonnés portent un marqueur `[TRUNCATED: ...]`. Une
commande produit une observation `BINARY_STRING` pour `strings`, sinon
`BINARY_ANALYSIS`. L’observation archive par `EvidenceRecorder` un journal
borné avec le hash d’entrée, la commande normalisée, la version du binaire, le
code retour, la durée, le statut et la sortie. Ghidra produit une observation
`BINARY_ANALYSIS` qui comprend le hash, l’opération, l’adresse, les versions
Ghidra/plugin, l’heure, les données normalisées et le marqueur de troncature.
L’identité d’objet de chaque observation est le SHA-256 du sample ; son propre
SHA-256 couvre le JSON archivé.

Les clés `EvidenceRecorder` partent de `ToolExecutionContext.idempotency_key`,
qui est la `tool_operation_key` déterministe de la boucle B. `RegistryToolDispatcher`
réutilise le résultat d’une clé terminée sans relancer l’outil. Les résumés
marquent explicitement `untrusted_data`; les limitations interdisent de traiter
les chaînes d’un binaire comme instructions. Les octets du sample ne sont ni
archivés dans ces observations JSON ni ajoutés au contexte Bridge.

## Écarts d’intégration connus

Le validateur de schéma du `RegistryToolDispatcher` transforme actuellement
un argument hors schéma en statut `failed` avant d’appeler l’adaptateur. Ainsi,
un `command_id` absent de l’`enum` réel est refusé sans exécution, mais ne peut
pas être traduit en `policy_denied` par F2 seul. L’adaptateur retourne bien
`policy_denied` si un tel identifiant lui parvient directement. Corriger le
statut en passant par le dispatcher nécessiterait une modification de
`investigation_tools.py`, qui est hors des fichiers autorisés pour ce lot.

Le catalogue F1 courant annonce `file`, `strings`, `readelf`, `objdump` et
`yara`. Il n’expose pas d’identifiants de commande distincts pour les
extracteurs PE structurés existants ; F2 ne les invente pas et conserve le
comportement d’`objdump` déjà catalogué.
