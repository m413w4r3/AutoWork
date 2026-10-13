# Analyse locale contrôlée et Ghidra facultatif

## Catalogue local

`LocalCommandCatalog` ne publie que les outils nommés ci-dessous. Il vérifie
leur présence avec `shutil.which`, puis leur version avec `--version`. Un outil
absent ou sans réponse de version donne `unavailable`. Les paramètres passent
par une whitelist typée ; les arguments libres, options inconnues et noms de
fichiers fournis par l'appelant sont refusés.

| Commande | Types reconnus | Paramètres acceptés |
|---|---|---|
| `file` | ELF, PE, autre | `mime_type` booléen |
| `strings` | ELF, PE, autre | `min_length` 1–256, `max_strings` 1–10 000 |
| `readelf` | ELF | `mode`: en-tête, segments, sections, dynamique ou symboles |
| `objdump` | ELF, PE | `mode`: en-tête, sections, en-têtes privés ou désassemblage |
| `yara` | ELF, PE, autre | 1–32 motifs texte (128 caractères chacun), combinaison `any` ou `all` |

Le wrapper YARA construit une règle locale à partir de motifs encodés en
hexadécimal ; il ne reçoit ni chemin de règle ni syntaxe YARA libre. Pour PE,
les extracteurs déjà présents, dont `StaticFeatureExtractor`/`pefile`, restent
la voie d'analyse structurée. Aucun binaire PE arbitraire n'est lancé.

Les limites par défaut par commande sont de 10 à 30 secondes, 16 KiB à 1 MiB
de sortie, 5 à 15 secondes CPU, 256 à 512 MiB d'espace mémoire et 1 MiB de
taille de fichier créée. `Settings` permet de réduire ou d'élargir ces plafonds
dans les bornes configurées. Le budget prévisionnel d'une commande est son
timeout maximal (`LocalCommandBudgetEstimate.tool_seconds`).

Avant l'exécution, le port d'échantillons résout les octets par SHA-256 ; le
service recalcule ce hash, refuse les fichiers trop grands et détecte ELF/PE
depuis leur en-tête. Le runner écrit `sample.bin` dans un répertoire temporaire
privé, passe le fichier en lecture seule, et l'efface à la fin. L'appel utilise
`create_subprocess_exec`, jamais un shell. Le journal archivable de
`LocalCommandResult.archive_record()` contient la commande normalisée, le hash,
le statut, le code retour, la durée et la sortie bornée ; il ne contient ni
octets de fichier ni chemin temporaire.

L'environnement enfant est reconstruit (`PATH`, `LANG`, `LC_ALL`, `HOME`,
`TMPDIR`) sans variables proxy, clés ou secrets. Le processus reçoit des limites
`RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE` et `RLIMIT_CORE` ; le
runner tue tout son groupe à l'expiration du délai ou du plafond de sortie.
Le runner préfère Bubblewrap (`bwrap --unshare-net`), qui confine aussi le
système de fichiers, puis un namespace réseau `unshare --net`, puis le filtre
seccomp réseau seul. Le test de disponibilité de bwrap utilise les mêmes
options de namespace, le répertoire `/analysis`, les binds en lecture seule de
`sample.bin`/`rules.yar` et le répertoire de travail que l'exécution. Le test
sonde ainsi aussi la configuration de loopback de bwrap. Si un lanceur de
namespace échoue malgré la sonde avec une erreur d'infrastructure émise par
`bwrap` ou `unshare` avant le démarrage de l'outil, le runner le désactive pour
le processus et réessaie une seule fois sous seccomp.

Le bootstrap installe toujours un filtre seccomp Linux qui refuse les appels
système de sockets réseau avant `exec`, y compris lorsque le namespace
fonctionne. Au moins ce filtre doit être actif avant que l'outil soit exécuté.
Si le bootstrap ou l'installation du filtre échoue, le résultat est
`unavailable` et la commande n'est pas lancée : aucune voie d'exécution sans
isolement réseau n'existe. Si Bubblewrap n'est pas disponible, `unshare` et
seccomp n'isolent pas le système de fichiers. Le résultat et les observations
portent alors la limitation « isolation réseau seule, pas de confinement du
système de fichiers ». Les déploiements de production doivent activer
`LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT=true` ; le défaut reste `false`
pour les hôtes où les user namespaces ou Bubblewrap ne sont pas disponibles.
Avec ce réglage activé, une analyse retourne `unavailable` sans lancer l'outil
si Bubblewrap ne passe pas sa sonde. Le test
`test_process_has_no_proxy_or_key_environment_and_socket_calls_are_blocked`
vérifie l'absence des variables et que la création d'un socket échoue avec
`EPERM`. Le filtre est défini pour Linux x86_64 et aarch64.

Le résultat est une capacité locale : une commande absente ou désactivée,
un type de fichier non admis ou une erreur d'exécution n'empêche pas les autres
outils ni l'avis de l'analyste.

## GhidraMCP derrière un port lecture seule

Le seul contrat applicatif est `GhidraAnalysisPort`. Il accepte les opérations
`functions`, `imports`, `xrefs_to`, `xrefs_from`, `decompile_function` et
`search`. L'adaptateur ne propose aucune opération de renommage, commentaire,
prototype ou modification. Toute opération non enregistrée est refusée avant
la requête HTTP.

`GhidraMcpHttpAdapter` utilise `httpx` avec `trust_env=False`, délais bornés,
taille maximale de réponse, redirections refusées et restriction de l'hôte. Le
loopback est autorisé par défaut ; un hôte distant exige son inscription
explicite dans `GHIDRA_ALLOWED_HOSTS`. Cette allowlist ne résout ni n'épingle
les adresses DNS : un hôte non-loopback autorisé est réservé à un déploiement
isolé, et son activation produit un avertissement de journal au démarrage. Une
observation normalisée porte le hash du binaire, l'opération, l'adresse demandée,
les versions Ghidra/plugin,
l'horodatage UTC et un indicateur `truncated`. Les champs d'octets sont écartés
et les URL présentes dans le texte de réponse sont masquées.

### État local de compatibilité

La référence de produit retenue est le dépôt
`LaurieWired/GhidraMCP` indiqué dans la spécification. Aucun clone local,
commit, numéro de version, endpoint ni schéma HTTP de ce projet n'était
disponible dans le checkout, et l'accès Internet n'a pas été utilisé. Il n'est
donc pas possible d'annoncer ici une version ou un commit compatible. Les
chemins d'endpoints sont des paramètres vides par défaut (`GHIDRA_*_PATH`) :
ils doivent être relevés et validés par un humain avec la version/commit
déployés avant activation. Le port attend des chemins de lecture HTTP GET ;
si le transport réel de GhidraMCP ne correspond pas, il faut confirmer le
contrat et adapter ce client avant de le câbler. Les chemins ne sont pas
présentés comme des endpoints vérifiés de GhidraMCP.

`GHIDRA_BASE_URL` pointe par défaut vers `http://127.0.0.1:8787` et
`GHIDRA_ENABLED` est désactivé. Les versions attendues sont optionnelles ; si
le service ne renvoie pas les versions, l'observation les marque
`unverified` tant qu'une version attendue n'est pas configurée. Une différence
par rapport à une version attendue produit une erreur typée.

Le déployeur doit démarrer Ghidra en mode headless comme service séparé des
services de base, sous un compte sans privilèges, avec sortie réseau bloquée
par le système d'exploitation ou le conteneur. Le port limite la destination
HTTP de l'adaptateur ; il ne remplace pas le pare-feu sortant du processus
Ghidra. En cas d'absence ou d'échec du service, l'adaptateur renvoie une erreur
typée `ghidra_unavailable` pour cette capacité, afin que SMDA/capa et le reste
de l'enquête puissent continuer.

## Configuration ajoutée

Les nouvelles capacités sont désactivées par défaut. `LOCAL_ANALYSIS_*` règle
les limites du catalogue et le confinement fichiers requis en production ;
`GHIDRA_*` règle l'activation, l'hôte, les délais,
la taille des réponses, les versions attendues, l'allowlist et les chemins
d'endpoints à confirmer. Aucune clé Ghidra n'est requise ni enregistrée.
