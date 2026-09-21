Oui. Je partirais sur une application web, plus précisément un **cockpit de production CTI avec des traitements asynchrones**.

Le navigateur ne communique jamais directement avec OpenAI, VirusTotal ou Shodan. Il permet de lancer des tâches, d’examiner leurs résultats et de franchir les validations humaines. Le backend conserve l’état réel du sujet, les preuves, fichiers et décisions.

La recherche OpenAI peut être exécutée en arrière-plan et suivie par l’application. La Responses API prend en charge la recherche web sourcée et les traitements asynchrones : [Web search](https://developers.openai.com/api/docs/guides/tools-web-search) et [Background mode](https://developers.openai.com/api/docs/guides/background).

## Workflow cible

Une édition est un conteneur mensuel vivant, en état canonique `OPEN` ou
`ARCHIVED`. Elle regroupe les sujets et leurs livrables sans porter de phase
de production courante. Plusieurs sujets peuvent donc occuper simultanément
des `ProductionStatus` et des étapes différentes : l’avancement se lit au
niveau de chaque sujet, sans phase de production portée par l’édition.

Discovery, Fusion et Selection sont des capacités accessibles indépendamment depuis l'édition.
Discovery produit et versionne les `DiscoveryCandidate`, Fusion décide et versionne les
regroupements explicables, et Selection décide si un `DiscoverySubject` doit devenir un `Subject`.
Il n'existe ni nouvelle phase globale obligatoire, ni état `fusion_complete`.

Le Dashboard de l’édition donne la vue d’ensemble et permet d’accéder aux
capacités indépendantes. Ces destinations de navigation ne représentent ni
des étapes terminées ni des prochaines étapes à faire avancer dans une
séquence.

### 1. Création de l’édition

L’utilisateur crée une édition :

* pays : Iran ;
* période : juillet 2026 ;
* TLP ;
* langues ;
* profils de sources ;
* cible indicative : deux articles longs et six brèves.

L’application charge :

* les sujets déjà traités pendant le mois ;
* les éditions précédentes ;
* les acteurs, campagnes, malwares et IOC connus ;
* les règles YARA et recherches déjà exécutées.

### 2. Découverte des sujets

L’utilisateur clique sur « Rechercher les sujets ». Cette action crée ou réutilise un
`DiscoveryRun` via `POST /api/editions/{edition_id}/discovery/runs`, avec une clé
`Idempotency-Key` explicite. `GET /api/editions/{edition_id}/discovery/runs` liste ensuite les
runs de l’édition et leurs résultats. Une édition peut en posséder plusieurs, y compris avec la
même configuration ; une nouvelle action utilise une nouvelle clé.

Le run est l’intention métier de la vague. Le `Job` associé porte l’exécution asynchrone et son
statut canonique ; le `ModelRun` porte l’interaction avec le modèle ; le `DiscoveryBatch` porte le
résultat parsé et sa révision. Les endpoints de candidats et de rapports restent distincts des
endpoints de création et de lecture des runs.

Le backend lance une recherche OpenAI en arrière-plan avec plusieurs axes :

* activités APT liées à l’Iran ;
* acteurs étatiques ou supposés étatiques ;
* rapports techniques comportant IOC, échantillons ou configurations ;
* nouvelles campagnes, familles, variantes ou infrastructures ;
* victimologie, chaînes d’infection et évolutions de TTP ;
* publications dans la période demandée.

En parallèle, le système interroge les sources suivies, RSS, résultats Livehunt et imports manuels.

L’inventaire des endpoints conserve séparément les opérations de rapports :
`POST /api/editions/{edition_id}/discovery/reports/reprocess` relance le parsing d’un rapport
archivé et `GET /api/editions/{edition_id}/discovery/reports/{run_id}` le consulte. Le batch
initial et ses remplacements restent rattachés par `batch.discovery_run_id` au run d’origine ;
un retraitement ou une récupération ne crée pas une nouvelle identité de run.

Un import manuel confirmé devient un `MANUAL_IMPORT` `DiscoveryRun`, tandis que sa prévisualisation
reste non persistante. Une édition archivée peut lister et lire les runs et résultats existants,
mais ne peut pas créer de run ni confirmer un nouvel import.

OpenAI est chargé de :

* trouver les publications ;
* identifier la source originale ;
* signaler les relations possibles entre reprises d’un même rapport ;
* distinguer les sources indépendantes des simples relais ;
* proposer une compatibilité de sujet, sans créer l’identité ni la décision de fusion ;
* évaluer la richesse technique du sujet.

Le résultat affiché est une liste de cartes :

| Champ                    | Exemple                        |
| ------------------------ | ------------------------------ |
| Sujet                    | Nouvelle campagne MuddyWater   |
| Source principale        | Rapport technique de l’éditeur |
| Sources associées        | 4                              |
| Échantillons disponibles | Oui                            |
| IOC disponibles          | 38                             |
| Potentiel de chasse      | Élevé                          |
| Nouveauté                | Nouvelle chaîne d’infection    |
| Déjà traité              | Non                            |
| État de Fusion           | Groupe `DiscoverySubject`      |

Les signaux de compatibilité ne reposent pas seulement sur le titre. Ils utilisent les URLs,
dates, acteurs, familles, hash, IOC et similarité du contenu. Ils n'effacent ni ne fusionnent les
candidats : un sujet déjà traité peut apparaître comme « mise à jour », puis être revu dans Fusion.

### 3. Sélection éditoriale

Fusion décide la structure ; Selection décide s’il faut matérialiser un `Subject` ;
`Subject` est stable ; Production décide quand produire ce `Subject`.

L’utilisateur peut, dans Selection, traiter un groupe du snapshot actif (`SELECT`) ou
l’ignorer (`IGNORE`). Ces décisions sont les seules écritures de la route `/selection`.
Selection ne compose pas le lot de production, ne choisit pas `brief` ou `major`, et ne
lance aucun batch. La matérialisation d’un `Subject` est atomique avec la décision `SELECT`.
La production reprend ensuite le `subject_id` canonique quand elle constitue son prochain lot.

Les actions `merge` et `split` sont effectuées dans Fusion, sur des UUID métier et un
`snapshot_version`. La revue Fusion reste lisible pour une édition `ARCHIVED`, mais ses décisions
et toute autre mutation y sont interdites.

À la décision `SELECT`, l’application matérialise atomiquement le `Subject` et son
`SubjectDiscoveryOrigin`. Une décision `IGNORE` conserve l’historique sans créer de `Subject`.

C’est le gate humain de matérialisation : **le modèle propose, Fusion structure, l’utilisateur
décide si un Subject doit exister**.

## Production d’un Subject

Les parcours de rédaction et d’analyse ci-dessous décrivent la Production d’un `Subject` déjà
matérialisé ; ils ne constituent pas des choix `brief`/`major` de Selection.

### 4A. Constitution du dossier de preuves

Le système :

1. télécharge et archive les publications originales ;
2. extrait le texte et les pièces jointes ;
3. identifie la source primaire et les reprises ;
4. extrait les faits, dates, acteurs, malwares, CVE, TTP, victimologie et IOC ;
5. vérifie que chaque IOC est réellement présent dans une source ;
6. normalise et déduplique les indicateurs ;
7. construit un `brief_evidence_pack`.

Qwen traite le volume, les traductions et l’extraction initiale. OpenAI intervient pour les ambiguïtés, le regroupement et la rédaction.

### 5A. Rédaction et validation

La brève est générée à partir du dossier de preuves :

* fait central ;
* contexte ;
* portée opérationnelle ;
* éventuelles limites ;
* sources ;
* IOC associés.

L’application affiche chaque affirmation avec sa preuve. Les choix de forme et de cadence sont
des décisions de Production, après matérialisation du `Subject`.

Après validation, le parcours s’arrête. Aucune chasse étendue ni règle YARA n’est lancée par défaut.

## Parcours d’un article principal

### 4B. Acquisition et extraction technique

Le système effectue le même travail que pour une brève, puis va plus loin :

* téléchargement des échantillons originaux disponibles ;
* triage statique ;
* récupération des informations VT ;
* extraction de configurations connues ;
* reconstruction provisoire de la chaîne d’infection ;
* inventaire des outils, commandes et techniques ;
* préparation de la victimologie ;
* chronologie des campagnes et variantes ;
* première cartographie ATT&CK.

Il produit une **synthèse technique de travail**, pas encore le texte définitif.

Cette distinction est importante : le premier texte sert à repérer les lacunes de l’analyse. La version publiable ne sera écrite qu’après les pivots et la rétroconception.

### 5B. Plan d’analyse et demandes à l’analyste

À partir des lacunes du dossier, OpenAI ou Qwen propose une liste de tâches.

Exemples :

* fournir la fonction de déchiffrement identifiée à telle adresse ;
* extraire les ressources du PE ;
* rechercher une configuration dans un blob donné ;
* confirmer l’algorithme de génération de domaine ;
* fournir le résultat de FLOSS, capa ou d’un décompilateur ;
* comparer deux fonctions ;
* vérifier la persistance ;
* analyser un paquet réseau ;
* confirmer que telle chaîne est spécifique à la famille.

Chaque demande doit comporter :

| Champ               | Rôle                                      |
| ------------------- | ----------------------------------------- |
| Question            | Ce que l’on cherche à établir             |
| Justification       | Pourquoi l’information manque             |
| Entrée attendue     | Fichier, fonction, capture, JSON, PCAP…   |
| Outil proposé       | Ghidra, IDA, FLOSS, capa, script…         |
| Commande indicative | Action reproductible                      |
| Risque              | Faible, isolé, manuel                     |
| Résultat attendu    | Format que l’application pourra réingérer |

Les tâches sûres et déterministes peuvent être automatisées dans une sandbox. La rétroconception interprétative reste humaine.

### 6. Boucle analyste–modèle

L’analyste sélectionne une tâche, réalise l’analyse et dépose le résultat :

* fonction décompilée ;
* notes ;
* configuration ;
* capture ;
* script d’extraction ;
* fichier déballé ;
* PCAP ;
* conclusion structurée.

Le système :

1. archive le résultat ;
2. le rattache à l’échantillon et à la question ;
3. met à jour le dossier de preuves ;
4. révise la synthèse technique ;
5. propose la question suivante ou indique que le niveau de preuve est suffisant.

L’analyste peut à tout moment :

* corriger l’interprétation ;
* ajouter une hypothèse ;
* interdire une conclusion ;
* déclarer une question hors périmètre ;
* arrêter la boucle ;
* définir le niveau de confiance.

L’« avis de l’analyste » peut être préparé par le modèle, mais ses champs sensibles restent contrôlés :

* constat technique ;
* interprétation ;
* hypothèses alternatives ;
* niveau de confiance ;
* limites ;
* attribution.

## 7. Pivots et chasse

Je modifierais légèrement l’ordre de votre exemple : les pivots interviennent **avant la version définitive de l’avis de l’analyste**, car leurs résultats peuvent modifier l’analyse.

L’application génère un plan de pivots depuis les invariants validés :

* hash et similarités ;
* chaînes rares ;
* configurations ;
* certificats ;
* imports ;
* PDB ;
* ressources et icônes ;
* relations VT ;
* domaines, IP et URLs ;
* TLS, favicon, JARM/JA4+ ;
* Shodan et passive DNS.

L’écran présente chaque pivot avec :

* graine ;
* requête exacte ;
* justification ;
* spécificité attendue ;
* risque de faux rapprochement ;
* portée découverte/attribution ;
* profondeur proposée.

L’utilisateur coche les pivots autorisés avant exécution.

### 8. Validation des résultats de chasse

Les résultats apparaissent sous forme de tableau et de graphe. Ils sont regroupés automatiquement, mais restent initialement `non validés`.

L’analyste classe chaque hit :

* lié au corpus ;
* variante ;
* contexte uniquement ;
* infrastructure partagée ;
* faux positif ;
* à examiner.

Seuls les résultats validés sont téléchargés dans le dossier du sujet. Ils sont ensuite réinjectés dans la boucle d’analyse.

Les vagues de chasse continuent jusqu’à ce que :

* aucun nouveau hit pertinent n’apparaisse ;
* les nouveaux liens soient trop faibles ou partagés ;
* le corpus soit suffisamment représentatif ;
* l’analyste décide d’arrêter.

## 9. Production des détections

Une fois le corpus validé :

1. constitution des corpus positif, négatif et holdout ;
2. extraction des invariants ;
3. génération d’une YARA ;
4. compilation et tests locaux ;
5. Retrohunt ;
6. analyse des faux positifs ;
7. itération ;
8. approbation humaine.

Suricata n’est proposée que si l’analyse fournit un invariant protocolaire stable et un moyen de le tester.

Les nouveaux IOC sont vérifiés, normalisés et associés à leur provenance avant d’être ajoutés au bulletin.

## 10. Rédaction finale

Le système gèle une version de l’`evidence_pack`, puis produit :

* synthèse ;
* contexte et chronologie ;
* analyse technique ;
* chaîne d’infection ;
* outils et TTP ;
* victimologie ;
* travail de pivot ;
* résultats de chasse ;
* avis de l’analyste ;
* YARA/Suricata ;
* IOC ;
* sources et figures.

Si une preuve change, le texte concerné repasse en état « à régénérer ».

L’utilisateur valide section par section, puis l’article rejoint le compositeur de l’édition.

## Écrans principaux

Le point d’entrée d’une édition est le Dashboard
`/editions/{edition_id}` : il présente le conteneur mensuel, ses sujets, leurs
statuts de production et les éléments de synthèse utiles à l’opérateur.
Depuis ce Dashboard, les capacités sont indépendantes :

* `/editions/{edition_id}/discovery` — découverte et lecture des candidats.
* `/editions/{edition_id}/fusion` — revue, résolution, fusion et séparation explicables.
* `/editions/{edition_id}/selection` — décisions `SELECT` ou `IGNORE` sur le snapshot actif.
* `/editions/{edition_id}/production` — choix du prochain `subject_id`, production et suivi des traitements.
* `/editions/{edition_id}/review` — revue des articles et validation éditoriale.
* `/editions/{edition_id}/publication` — assemblage, publication et téléchargement.

Ces routes sont des destinations de navigation, et non une séquence de
transitions d’état ou une indication de capacité terminée ou suivante.

Le workbench pourrait utiliser ces onglets :

`Vue générale | Sources | Preuves | Échantillons | Analyse | Demandes analyste | Pivots | Chasse | Détections | Rédaction | QA`

## Juste milieu humain–automatisation

| Étape                     | Automatique              | Humain obligatoire                    |
| ------------------------- | ------------------------ | ------------------------------------- |
| Discovery et signaux de compatibilité | Oui          | Revue des candidats                   |
| Fusion (merge/split)      | Proposition              | Décision humaine et résolution         |
| Extraction technique      | Oui                      | Correction des ambiguïtés importantes |
| Production d’un Subject   | Oui                      | Validation finale                     |
| Plan de rétroconception   | Proposition              | Réalisation/interprétation            |
| Pivots                    | Proposition et exécution | Autorisation du plan                  |
| Regroupement des hits     | Oui                      | Validation du corpus                  |
| Avis de l’analyste        | Brouillon assisté        | Conclusions et confiance              |
| YARA/Suricata             | Génération et tests      | Approbation                           |
| Publication               | Assemblage               | Validation finale                     |

La règle générale serait : **la machine prépare, exécute les tâches bornées et montre les preuves ; l’humain tranche tout ce qui change le sens analytique du livrable.**

Techniquement, je construirais donc le backend et une interface web centrée
sur le Dashboard et les sujets. Le cycle canonique de l’édition reste
`OPEN`/`ARCHIVED`, tandis que les statuts de production, preuves et décisions
restent portés par chaque sujet et ses artefacts. Il ne faut surtout pas faire
d’une conversation OpenAI la mémoire du sujet : l’état canonique doit rester
dans la base, les manifestes et les `evidence_packs`.
