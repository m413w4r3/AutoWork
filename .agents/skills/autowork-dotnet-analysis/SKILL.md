---
name: autowork-dotnet-analysis
description: Exploiter un PE/.NET pour révéler ses données, invariants, protocole C2 et motifs YARA, avec field.data.read et décompilation IL ciblée ; sans analyse exhaustive imposée.
---

# Analyse .NET statique ciblée

**Périmètre :** assembly associé et accessible dans CETTE enquête. Ne jamais exécuter, charger ou instancier le sample. La présence d'un type/méthode ne prouve pas son activation ; la présence d'un appel ne prouve pas son exécution.

## Niveau 0 — Identité et visibilité

1. Vérifier sample SHA, association, taille et observations source ; distinguer rapport VT, feature pack et octets acquis.
2. Établir PE/CLI, architecture, éventuel assembly mixte, entry point, noms d'assembly/modules et imports/refs par outils disponibles : sample.get_feature_pack, assembly.inspect, references.imports, types.list, strings.list.
3. Noter moteurs indisponibles ou sorties tronquées. Si un pack est absent, ne pas déduire que le contenu est absent.

## Niveau 1 — Cibles de décompilation

- Lister classes, héritages, méthodes et signatures. Chercher points d'entrée pertinents (Main, module initializer, DllMain natif éventuel, override, .cctor, callbacks).
- Sélectionner des méthodes par hypothèse : configuration, réseau, fichiers/registre, accès ressources, crypto, chargement/réflexion, tâches ou processus. Utiliser methods.list / method.decompile et les pages d'observations, jamais « tout décompiler » par défaut.
- Pour AppDomainManager : constater précisément classe dérivée, constructeur, override InitializeNewDomain ; chercher le mécanisme de sélection/activation (fichier .config, environnement, appdomain loader) et les appelants. Une DLL contenant ce type ne prouve pas que RuntimeBroker.exe.config l'a chargé.
- Pour etwEnable : distinguer chaîne/attribut de configuration, lecture effectuelle, interprétation par le runtime .NET et effet mesuré sur la télémétrie. Ne pas convertir « etwEnable=false est mentionné » en « ETW désactivé sur l'hôte ».

## Niveau 2 — Données statiques et décodage

1. Déterminer rapidement quels champs statiques, ressources, byte arrays et méthodes de transformation peuvent fournir des chaînes, constantes C2 ou motifs de détection ; resources.list, method.decompile et strings.list selon disponibilité.
2. **Utiliser field.data.read si offert** pour lire le contenu d'un FieldRVA par token ou nom de champ : cet outil résout RVA → section → offset et restitue les octets bornés avec leur empreinte. Vérifier la référence de l'observation et la longueur mesurée ; le champ peut avoir une longueur à fournir si son type ne la détermine pas.
3. Si field.data.read est absent/inadapté et qu'un offset **fichier** est déjà établi, local.run_command/xxd peut lire une plage autorisée. Ne pas assimiler RVA à offset fichier ni imposer une nouvelle extraction de preuve déjà disponible.
4. Reconstituer statiquement les transformations réellement observées dans le CIL (XOR, index, encodages, décompression, etc.). Ne pas exécuter l'assembly ni contacter les endpoints découverts.
5. Convertir dès que possible les données vérifiées en **invariants candidats**, constantes protocole, configuration, hypothèses de framing ou pivots vers des samples apparentés. Une extraction partielle peut déjà suffire à une candidate YARA.

Un octet opaque n'est pas nécessairement du chiffrement ; distinguer preuve de stockage, transformation, chaîne décodée et utilisation possible.

## Niveau 3 — Produire du renseignement et des détections

- Rechercher des éléments distinctifs : constantes de déobfuscation, configuration, structures internes, noms ou ressources rares, séquences CIL, propriétés de chargement. Noter leur stabilité connue, pas une universalité présumée.
- Décompiler les fonctions qui construisent/parsent des messages ou gèrent la crypto. Fournir les champs et transitions **démontrés**, en indiquant les chemins non observés. Passer à autowork-network-protocol-detection pour C2 et Suricata si utile.
- Construire des motifs YARA candidats à partir des octets et structures effectivement retrouvés, y compris sur un seul spécimen clairement identifié ; enrichir les positifs et goodware ultérieurement.
- Comparer des assemblies sur leurs caractéristiques distinctives si des variantes sont disponibles, puis pivoter VT vers de nouveaux échantillons.
- Un fait statique vérifié peut nourrir un finding ; ne pas attendre un statut supported pour poursuivre l'analyse ou créer une candidate.

## Sortie et arrêt

Sortie concise : méthode/champ/ressource, octets ou transformations vérifiés, invariants et éventuels motifs YARA, élément C2, références canoniques, prochaine piste utile. Ne pas exiger un tableau complet pour chaque lecture.

Arrêter si octets inaccessibles, décompilation incomplète, sélection/activation non démontrée ou nouvelles lectures non discriminantes ; ne pas demander d'exécution live pour combler le manque.

**Oracle :** une donnée FieldRVA est lue via field.data.read si disponible ; l'agent en extrait des motifs candidats et pistes C2 sans conclure à une exécution réelle, ni exiger la preuve ETW pour poursuivre.