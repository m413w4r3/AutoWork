# Diagnostic REA — 2026-10-19

## Verdict

**Le produit REA existe et paraît maintenu comme produit autonome. Le runner AutoWork reste `unavailable(reason=...)`.** Le dépôt amont examiné est `morluto/rea`, HEAD `dc936f433cf305ec9dbf9df70842303f89fab03f` (commit du 2026-10-19). Son CLI et son transport MCP sont réels et son catalogue a plusieurs primitives d’analyse statique proches des opérations AutoWork. Aucun protocole `native.inspect` ni mapping AutoWork testé n’est toutefois livré. La compatibilité des résultats, les opérations résiduelles et l’isolation d’un runner ne sont pas prouvées à l’exécution.

Raison à restituer pour ce diagnostic :

```text
unavailable(reason="REA 6.7.0 au commit dc936f433cf305ec9dbf9df70842303f89fab03f expose des outils MCP à sessions binaires/Evidence, mais aucun protocole natif AutoWork v1; le mapping des résultats, plusieurs sémantiques du catalogue et l'allowlist confinée ne sont pas testés")
```

Le code existant reste inchangé. Sans runner injecté, `build_native_analysis_port` retourne `NativeAnalysisRunnerNotConfigured("REA runner must be supplied by a confined deployment worker")`. Aucun appel REA ni résultat `native.inspect` n’a été fabriqué.

## Diagnostic classé

### PROUVÉ PAR TRACE

- Le clone en lecture seule a été créé hors du monorepo dans `/home/nill/audit-tmp/rea-c10-shallow-9MQy71zV` avec `git clone --depth 1 https://github.com/morluto/rea.git`. Le `HEAD` local et `refs/heads/main` renvoyé par `git ls-remote` sont `dc936f433cf305ec9dbf9df70842303f89fab03f`.
- Le commit examiné est daté du 2026-10-19 à 20:04 +0800. `package.json` déclare `rea-agents` version `6.7.0`; le dépôt publie aussi le tag `rea-agents-6.7.0` (référence `27dc42d10249f0410904a75c0997842eb03b931c`). La version publiée sur npm n’a pas été installée ni vérifiée auprès du registre.
- Aucun script du dépôt n’a été lancé : pas d’installation, build, CLI, serveur MCP ou test REA. La lecture s’est limitée aux fichiers source et manifestes.

### PROUVÉ PAR CODE

- **Identité, licence et maintenance.** Le README décrit « REA: Reverse Engineer Anything », le manifeste définit `rea` et `rea-agents` comme binaires du paquet, et la licence du code REA est MIT. Le dépôt contient des notices séparées : les dépendances, composants dérivés et moteurs gardent leurs propres licences; la licence MIT ne concède pas les droits sur Ghidra, Hopper, IDA ou JEB. Le manifeste exige Node `^22.19.0 || ^24.11.0 || >=26.0.0` et verrouille `@modelcontextprotocol/server` à `2.3.1`. Le commit courant, les tags successifs et la documentation détaillée sont des indices de maintenance; ils ne constituent pas une qualification de sécurité ou une preuve d’installation réussie.
- **Protocole attesté.** `scripts/rea.mjs` route `rea mcp` vers le runtime MCP compilé; `src/main/transport.ts` utilise le handle stdio du SDK MCP. Le CLI documente les commandes JSON `analyze`, `search`, `function`, `decompile`, `xrefs`, `trace` et `instructions`. En MCP, le client ouvre un binaire par chemin avec `open_binary`, choisit un fournisseur et utilise une session qui reste active jusqu’à fermeture ou changement explicite. Le protocole attesté est donc CLI et MCP stdio à sessions; aucun endpoint HTTP destiné à ce runner n’a été identifié dans les points d’entrée et contrats consultés.
- **Compatibilité des opérations.** AutoWork expose treize opérations via [`NATIVE_READ_OPERATIONS`](../../backend/src/cti_app/application/native_analysis.py#L18) : `list_functions`, `get_function`, `decompile`, `xrefs_to`, `xrefs_from`, `callers`, `callees`, `strings`, `search`, `read_value`, `imports`, `exports`, `sections`. Le catalogue MCP amont contient de vraies primitives ([`officialToolContracts.ts`](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f89fab03f/src/contracts/officialToolContracts.ts#L111)) : `list_procedures`, `procedure_info`, `procedure_pseudo_code`, `procedure_references` (incoming/outgoing), `procedure_callers`, `procedure_callees`, `list_strings`, `search_strings`, `search_procedures`, `read_bytes`, `list_names` et `list_segments`; le contrat mémoire définit aussi `search_bytes` ([`hopperMemoryContracts.ts`](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f89fab03f/src/contracts/hopperMemoryContracts.ts#L8)). Les mappings conceptuels sont plausibles : fonctions/procédures, décompilation, références, appels, chaînes, recherche de bytes et segments. Ils ne prouvent pas l’équivalence de couverture ou de format : `read_bytes` renvoie des octets mémoire, pas une valeur typée; la recherche numérique (`VALUE`) et la séparation imports/exports ne sont pas établies par les contrats consultés. `procedure_info` et `analyze_function` ont aussi une forme plus riche que la requête AutoWork. AutoWork exige une réponse identifiée par SHA-256, opération, empreinte de requête, versions et schéma `autowork-rea-read-response.v1` ([`rea_adapter.py`](../../backend/src/cti_app/infrastructure/rea_adapter.py#L226)); REA ne livre pas ce schéma. `open_binary` prend un chemin et ouvre une session; la requête AutoWork porte un SHA-256, pas un chemin. Il faut donc un mapper qui vérifie le fichier monté, pilote la session, filtre le catalogue MCP puis valide et convertit chaque sortie.
- **Correspondance indicative.** Les primitives REA sont de vrais outils, mais ce tableau n’est pas une preuve de compatibilité runtime.

  | Opération AutoWork | Outil REA candidat | Point restant à prouver |
  | --- | --- | --- |
  | `list_functions`, `get_function`, `decompile` | `list_procedures`, `procedure_info`, `procedure_pseudo_code` | Forme du dossier, couverture et identité |
  | `xrefs_to`, `xrefs_from`, `callers`, `callees` | `procedure_references`, `procedure_callers`, `procedure_callees` | Sens des références et pertes d’arêtes indirectes |
  | `strings`, `search` | `list_strings`, `search_strings`, `search_procedures`, `search_bytes` | `search_bytes` est Ghidra seulement; recherche numérique non attestée |
  | `read_value` | `read_bytes` | Conversion des octets en valeur typée/endianness |
  | `imports`, `exports` | `list_names` | Séparation imports/exports non attestée |
  | `sections` | `list_segments` | Couverture/champs à normaliser |

- **Surface de sécurité.** REA regroupe analyse statique et fonctionnalités capables de lancer ou piloter des processus, applications, navigateur ou interface native; certains outils peuvent aussi modifier un appareil Android. La documentation qualifie les annotations MCP d’indices et non de contrôles d’autorisation. Elle indique que les scénarios dynamiques tournent avec les permissions de l’utilisateur et peuvent modifier des fichiers ou produire du trafic réseau. Le guide d’installation précise qu’une inscription MCP ne retire pas l’accès au shell, au système de fichiers ou au réseau et recommande un compte ou conteneur séparé pour l’isolation. L’absence de réseau, le compte dédié, le montage d’échantillon en lecture seule, le système de fichiers confiné et l’absence de privilèges hôte doivent donc être imposés par le worker; le produit REA ne les atteste pas lui-même.
- **Téléchargements et dépendances.** Le parcours documenté passe par `npx`/npm; `rea update` met à jour l’installation globale et le setup peut installer Hopper après confirmation. Ce sont des actions d’installation ou de mise à jour, pas une preuve qu’elles soient requises au runtime du serveur. Elles n’ont pas été lancées. Une image worker devrait embarquer les artefacts et lockfiles épinglés au build, puis couper le réseau à l’exécution; aucun tel build n’a été vérifié ici.
- **Compatibilité statique.** Le recouvrement des primitives rend un mapper plausible. Le dépôt amont consulté ne démontre pas une allowlist MCP limitée aux lectures AutoWork ni un auto-test de cette capacité sous le contrat AutoWork. Le médiateur requis n’existe pas dans AutoWork; les lacunes de sémantique et d’identité/hash ci-dessus restent à résoudre et à tester sur une fixture.
- **Ghidra natif non bloqué.** La fabrique AutoWork distingue `REA` de `GHIDRA_MCP`. Pour `GHIDRA_MCP`, elle construit `GhidraMcpNativeAnalysisAdapter` et son client dédié indépendamment de la branche REA ([`rea_adapter.py`](../../backend/src/cti_app/infrastructure/rea_adapter.py#L257), [`build_native_analysis_port`](../../backend/src/cti_app/infrastructure/rea_adapter.py#L369)). Le diagnostic REA ne désactive pas cette voie; sa disponibilité réelle dépend toujours de la configuration et du worker Ghidra.
- **ILSpy .NET non bloqué.** L’analyse managée est un worker distinct ([`dotnet_analysis.py`](../../backend/src/cti_app/workers/dotnet_analysis.py#L1)) avec `ILSPYCMD_VERSION = 9.1.0.7988`, empreinte SHA-512 et sources épinglées dans [`application/dotnet_analysis.py`](../../backend/src/cti_app/application/dotnet_analysis.py#L1). Ses opérations couvrent `assembly`, `types`, `methods`, `decompile`, `references`, `strings` et `resources`; le code marque explicitement l’analyse CLI comme sans exécution de code. Son chemin ne dépend pas de l’adaptateur REA.

Sources amont examinées à ce SHA : [README](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f89fab03f/README.md), [LICENSE](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/LICENSE), [package.json](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/package.json), [scripts/rea.mjs](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/scripts/rea.mjs), [docs/cli.md](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/docs/cli.md), [docs/mcp-contracts.md](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/docs/mcp-contracts.md), [docs/native-investigation.md](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/docs/native-investigation.md), [docs/installation.md](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/docs/installation.md), [src/main/transport.ts](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/src/main/transport.ts), [src/contracts/enhancedToolContracts.ts](https://github.com/morluto/rea/blob/dc936f433cf305ec9dbf9df70842303f/src/contracts/enhancedToolContracts.ts).

### SUSPECTÉ

- La surface du serveur généraliste est acceptable seulement si un médiateur n’expose que les primitives statiques retenues et si le worker applique les contraintes de confinement demandées. Un conteneur peut imposer l’isolation du processus, mais aucune configuration de déploiement, allowlist ou preuve runtime ne le démontre dans cette passe. Je ne conclus pas qu’une intégration future soit impossible; son contrat complet et sa sûreté ne sont pas établis aujourd’hui.
- Ce diagnostic ne constitue pas une revue complète de sécurité des milliers de fichiers, de toutes les dépendances ou des moteurs externes. Les risques documentés ci-dessus sont suffisants pour ne pas lancer ce serveur comme runner AutoWork sans médiateur contrôlé.

### NON TESTÉ

- Installation du paquet, build, exécution du CLI/MCP, annonce runtime des outils, compatibilité réelle avec une fixture bénigne et essais d’isolation réseau/fichiers/utilisateur.
- Disponibilité de Ghidra et d’ILSpy dans la stack active. Leur indépendance est prouvée par le chemin de code, pas par un essai runtime dans cette passe.
- Publication exacte de `rea-agents@6.7.0` sur le registre npm et qualification de sécurité des dépendances transitives.

## Contrat et portée du changement

- Aucun endpoint, schéma, enum, version de schéma ou nom d’outil AutoWork n’a été ajouté ou modifié.
- Le contrat AutoWork attendu reste `NativeAnalysisRequestV1` et `autowork-rea-read-response.v1`; le catalogue de lecture reste `autowork-native-read.v1`.
- Verdict opérationnel : `unavailable(reason=...)` ci-dessus. Aucun Dockerfile, service Compose, script, faux adaptateur ou observation native n’a été ajouté. Il n’y a pas de migration.

## Fichiers

- Créé : `docs/operations/rea-diagnostic-2026-10-19.md`.
- Modifié : aucun fichier code.

## Vérifications et limites de la passe

- Diagnostic de dépôt : clone amont, `git rev-parse HEAD`, `git show -s --format=fuller HEAD`, `git ls-remote --symref` pour `main`, `git ls-remote --tags`, inventaire des chemins suivis et lectures ciblées README/licence/manifestes/docs/sources. Résultats consignés ci-dessus.
- Localisation du code AutoWork : deux requêtes `uv run scripts/ctx/ctx.py query ... -k 8`, puis lecture des plages source retournées et du code fournisseur .NET.
- Tests, installation et build : aucun lancé. Le livrable est un diagnostic statique et les critères de compatibilité/surface ne sont pas réunis pour un prototype runtime. Cela respecte l’interdiction de lancer le code amont sur l’hôte et évite une installation ou un build sans chemin d’intégration attesté.
- Aucun point de migration ni changement de base. Si une future passe fournit un médiateur limité aux opérations retenues, elle devra épingler image, commit, Node et lockfile; vérifier la capacité/version à l’exécution; monter une fixture par hash en lecture seule; et prouver réseau coupé, utilisateur non privilégié, système de fichiers confiné et absence de privilèges hôte avant de produire une observation.
