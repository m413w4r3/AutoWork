---
name: autowork-dotnet-analysis
description: Faire le triage statique ciblé d'un PE/.NET/CLI : metadata, IL, AppDomainManager, ressources, chargement et chaînes obfusquées. Charger lorsque la structure CLI est observée.
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

## Niveau 2 — Ressources et obscurcissement

1. Identifier ressources/manifests, champs statiques, byte arrays, points d'accès et méthodes de transformation ; utiliser resources.list et décompilation ciblée si disponibles.
2. Si une donnée opaque est référencée (ex. blob 964 octets), relever objet source, longueur, RVA/documentation de metadata, usage et source de l'adresse.
3. Vérifier si local.run_command offre xxd dans le catalogue attesté. xxd lit par **offset de fichier** : traduire et vérifier RVA→offset avec la table de sections PE ; ne pas appliquer directement le RVA en offset.
4. Récupérer seulement une plage autorisée, bornée et rattachée à l'association canonique, vérifier longueur, empreinte et couverture. Si l'API expose un aperçu tronqué, lire l'observation paginée ; ne pas reconstituer les octets manquants.
5. Reconstituer uniquement les transformations statiquement démontrées (XOR, index, encodages, Base64, RC4, décompression, assemblages dynamiques). Les branches non couvertes sont UNKNOWN.
6. Distinguer données décodées, destination possible et communication exécutée. Ne pas contacter les URL, charger l'assembly ou lancer le déchiffreur à partir du sample.

## Niveau 3 — Valeur CTI

Comparer deux assemblies uniquement sur éléments discriminants : hashes, structures, méthodes, ordre des opérations, données constantes et ressources, et non sur la seule présence de mscoree.dll, AppDomainManager ou une API commune.

Émettre une observation/finding atomique directement démontré si possible ; maintenir les questions d'exécution, de configuration et d'ETW inconclusive sans preuve supplémentaire. Pour une similarité, voir autowork-cti-pivot-strategy.

## Sortie et arrêt

Table : sample + association ; méthode/type et ref canonique ; comportement **possible** dans le code ; transformateur et données effectivement lues ; chemin d'activation prouvé ou absent ; couverture ; affirmation possible ; prochain test discriminant.

Arrêter si octets inaccessibles, décompilation incomplète, sélection/activation non démontrée ou nouvelles lectures non discriminantes ; ne pas demander d'exécution live pour combler le manque.

**Oracle :** avec les deux samples M09, l'agent remarque l'override AppDomainManager et essaie les voies statiques autorisées pour le blob, tout en conservant le lien RuntimeBroker/ETW non établi.