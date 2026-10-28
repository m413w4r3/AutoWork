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

### Archives

`zip_members` traite un ZIP avec `zipfile` dans le worker confiné, sans extraction,
fichier temporaire, sous-processus ni exécution du contenu. Les membres restent dans
l’ordre du répertoire central. Chaque ligne rapporte le nom comme donnée, l’offset du
header local, les tailles, le CRC32, le type de compression, le marqueur dossier, le
SHA-256 si calculé et un statut par membre. La limite est de 2 000 membres, 64 MiB
décompressés par membre, 256 MiB au total et un ratio de 1 000:1; le préfixe et
`max_members` sont bornés.
Les entrées chiffrées sont signalées sans être ouvertes.

`archive_list` et `archive_extract_hash` utilisent `7z` uniquement si la version et la
famille archive du worker sont attestées. Ils couvrent les formats pris en charge par
7-Zip, dont ZIP, 7z, RAR, TAR, GZip, CAB et ISO. `archive_extract_hash` envoie un seul
membre à la fois sur stdout (`-so`) puis calcule son SHA-256; aucun chemin de membre
n’est utilisé comme destination et le contenu n’est jamais exécuté. Le worker limite
à 2 000 membres, 64 MiB par membre, 256 MiB au total, un ratio de compression de
1 000:1 et le délai de la commande. Le paramètre `archive_preset` accepte seulement
`none`, `infected` et `malware`. Les noms restent des données non fiables et les limites
ou erreurs sont marquées dans le JSON de sortie. L’image `analysis-worker` doit être
reconstruite pour inclure 7-Zip et le code de traitement.

Ces commandes observent ou hachent les membres, mais ne créent pas encore un nouvel
objet `Sample`/`SampleAssociation`; d’autres outils locaux ne peuvent donc pas adresser
un membre par son hash tant qu’un flux d’import dérivé avec provenance n’existe pas.
Le worker confiné persiste actuellement un résultat JSON, tandis que l’import opérateur
réutilisable exige un acteur, un motif, l’ingestion du blob en quarantaine et une écriture
idempotente de `Sample`, `SampleAssociation` et de sa provenance. Un futur outil
`archive.extract_member` devra aussi lier le hash parent et le chemin du membre, puis
appliquer une politique de divulgation qui ne relâche pas celle de l’archive.

Le registrar réutilise le lecteur d’octets fourni par G2 pour Ghidra ; les
commandes locales utilisent le même lecteur déjà injecté dans
`LocalCommandService`. `QuarantineRuleCorpusBytes` vérifie le SHA-256, la taille
et les buckets qu’il sait lire. L’association empêche un hash provenant d’une
autre enquête d’être analysé.

Les commandes qui utilisent un binaire s’exécutent par le worker confiné avec
`create_subprocess_exec`, un nom de fichier interne fixe, des limites
CPU/RAM/temps/sortie, un environnement nettoyé et le filtre réseau prévu par F1.
Aucune entrée Bridge ne devient un
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

Le catalogue F1 projette `zip_members` et les commandes 7-Zip seulement lorsque
leurs familles sont présentes et attestées dans le worker. Les extracteurs PE
structurés existants ne reçoivent pas d’identifiants distincts; F2 conserve le
comportement d’`objdump` déjà catalogué.
