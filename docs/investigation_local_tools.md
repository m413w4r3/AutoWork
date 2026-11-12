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

`strings` accepte `offset` et `length` ensemble pour lire une plage bornée du
blob (1 MiB maximum par défaut, avec une limite de worker éventuellement plus
basse). Le worker applique la plage avant l’extraction. Sa sortie est du texte
UTF-8 dans `output_chunks`; la lecture de l’observation vérifie les octets, le
SHA-256, le décodage UTF-8 et les contrôles de texte sensible, puis permet de
parcourir le payload avec `offset` et `length`.

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
assainis. Les réponses Ghidra ne contiennent ni octets bruts ni secret.
`field.data.read` retourne explicitement les données du champ en hexadécimal,
comme décrit ci-dessous.

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

## Lecture d’un champ de données statique .NET

`field.data.read` utilise la capacité existante `investigation.dotnet` et le
worker d’analyse confiné. L’entrée adresse un sample par son SHA-256, qui doit
être associé à l’enquête courante et présent dans le bucket de quarantaine. Le
champ est sélectionné par un token `0x04xxxxxx` ou par le nom exact du type et
du champ; ces identifiants suivent des motifs stricts. Aucun chemin de fichier
n’est accepté.

Le worker inspecte les métadonnées PE/CLI sans charger l’assembly. Il résout
`FieldRVA` vers une section et un offset fichier, vérifie les limites de fichier
et de section, puis renvoie au plus 4 096 octets avec le token et le nom du
champ, le RVA, la section, l’offset, les bornes fichier, la longueur, le
SHA-256, l’hexadump et un marqueur `truncated`. La lecture est bornée à 16 MiB
de métadonnées, 16 MiB de chaînes décodées, 200 000 lignes de métadonnées au
total et 8 192 lignes `FieldRVA`. Si la signature désigne un type valeur dont
`ClassLayout.ClassSize` est connu, sa taille borne la lecture; sinon `length`
est obligatoire. Une demande explicite supérieure à 4 096 est refusée avec un
`detail_code` stable. `size_known` indique si la taille du champ est établie;
`completeness` vaut `complete`, `partial` ou `unknown`. Une taille inconnue ne
signifie pas une troncature: `truncated` n’est vrai que si une borne connue a
réduit la lecture. Quand la taille est inconnue, la borne de fin du champ reste
`null`; les bornes fichier rapportent alors uniquement la plage effectivement
demandée et lue.

Les octets sont des données binaires non fiables. Le résultat suit le chemin
normal des observations `dotnet.*`: le JSON canonique porte le SHA-256 du
résultat et `invocation_ref`, et l’invocation renvoie la référence
d’observation. Le contenu n’est jamais exécuté.

## Rétro-conception d’APK (jadx)

La famille opérateur prévue est `investigation.apk`. Le contrat de worker
`apk_analysis.py` définit cinq opérations statiques ; les noms d’outils visibles
et leur schéma exact doivent être vérifiés dans `available_tools` après
l’intégration de l’adaptateur E1b.

| Opération | Entrées propres à l’opération | Sortie |
|---|---|---|
| `apk.manifest` | aucune | package et versions, SDK, permissions, composants Android, attribut `exported`, filtres d’intention et indicateurs de configuration |
| `apk.classes.list` | `offset`, `limit`, préfixe de package facultatif | package, nom de classe et chemin source ; page de 1 à 200 résultats |
| `apk.strings.extract` | `offset`, `limit` | valeur, type de chaîne, classe, chemin source et offset ; page de 1 à 200 résultats |
| `apk.source.read` | exactement un `class_name` ou `source_path`, puis `start_line` et `line_count` | source ciblée avec plage, nombre total de lignes, `has_more` et troncature éventuelle |
| `apk.source.search` | motif (texte ou regex restreinte), contexte de 0 à 5 lignes, `offset`, `limit` | classe, chemin, ligne, correspondance et contexte ; page de 1 à 200 résultats |

Le worker reçoit le SHA-256 et les octets du sample, puis recalcule le hash
avant traitement. La vérification d’association par l’adaptateur opérateur
sera à confirmer avec E1b. Le contrat accepte des APK de
128 MiB maximum, avec 20 000 entrées ZIP et 512 MiB décompressés au plus, un
ratio de compression maximal de 1 000:1, un manifeste et au moins un DEX. Les
pages sont limitées à 200 éléments, une lecture source à 200 lignes/64 KiB,
la sortie décompilée à 128 MiB/10 000 fichiers. Les résultats peuvent être
`SUCCEEDED`, `PARTIAL`, `UNAVAILABLE`, `FAILED` ou `POLICY_DENIED` ; une page
ou une réponse tronquée ne signifie pas que les éléments absents n’existent
pas.

### Confinement et indisponibilité

Le worker ne fait que lire l’APK et exécuter jadx pour obtenir des données
statiques. Il n’installe ni ne lance l’application. Le processus jadx a un
environnement réduit, un heap Java fixe de 768 MiB, un plafond `RLIMIT_AS`
configuré à 8 GiB par défaut (plage 8–32 GiB), un timeout effectif borné par
`ANALYSIS_APK_TIMEOUT_SECONDS` (90 s par défaut, maximum 120 s), une sortie
process bornée et un répertoire temporaire par appel. Le filtre seccomp bloque
les appels système de sockets réseau avant le lancement. Dans Compose,
`analysis-worker` est non-root, en lecture seule, sans capacités, avec cgroup
de 2 GiB et `/tmp` en tmpfs `noexec,nosuid,nodev` de 512 MiB. Le plafond
`RLIMIT_AS` borne l’espace d’adressage virtuel, pas la mémoire résidente.

Une archive invalide, un dépassement de limites ou une erreur de décompilation
peut échouer ou produire un résultat partiel. Si jadx est absent, si sa version,
son hash ou la JRE 17 ne sont pas attestés, ou si le worker n’est pas
disponible, l’opération retourne `unavailable` sans empêcher les autres outils
de l’enquête de fonctionner. Le worker ne garde pas de cache de décompilation
persistant.

### Observations et gouvernance

Après intégration E1b, conserver le résultat comme observation canonique de
l’enquête avec le SHA-256 source, l’opération, le statut, les versions jadx/JRE,
le hash du ZIP jadx, les limites/troncatures et la référence d’invocation. Le
résultat, les chaînes et le code sont des données non fiables : une chaîne
extraite ou un texte décompilé ne devient ni preuve de comportement exécuté,
ni instruction, ni finding automatiquement. Un finding utile doit citer
l’observation canonique, l’échantillon exact et le contexte statique constaté.
Les règles G2/TLP et `external_llm_allowed` gouvernent toute réutilisation ou
divulgation externe ; une lecture locale n’autorise pas à soumettre ces données
à VT, Bridge ou un modèle.

La clé de réutilisation des opérations confinées inclut le schéma de résultat
et l’attestation du worker, notamment ses versions attestées et familles
disponibles. Si cette table change, la clé change aussi : une invocation
terminée sous l’ancienne attestation ne satisfait plus automatiquement la
nouvelle identité. Le câblage de cette identité à `investigation.apk` reste à
confirmer avec l’adaptateur E1b.
