---
name: autowork-pe-native-analysis
description: Enquêter statiquement sur un PE natif ou packé (C/C++, Go, Rust, Delphi, loader) en reliant header, imports, fonctions, données et observations sans inférer l'exécution. Charger après identification PE sans CLI.
---

# PE natif : de la structure à une question de comportement

L'extension .exe/.dll, le nom fournisseur et le langage supposé ne suffisent pas à qualifier le binaire. Préserver SHA et association exacts. Ne jamais exécuter le PE ou contourner le confinement.

## Profil initial

1. Établir format, architecture, taille, timestamp PE (indice non fiable), sections, caractéristiques d'entropie, imports/exports, ressources, TLS callbacks et entry point selon les outils réellement disponibles.
2. Utiliser en priorité packs locaux existants (sample.get_feature_pack, sample.analyze s'il est offert), strings.list et local.run_command pour file/objdump/xxd si autorisés. Conserver provenance des résultats CAPA, SMDA et statiques ; une annotation CAPA est une capacité déduite, pas une exécution observée.
3. Caractériser packing/obfuscation : entropie, sections anormales, imports rares, import resolution et tables manquantes sont des indices ; ils ne prouvent pas un packer précis.
4. Repérer signatures génériques de toolchain (CRT, libstdc, Go, Rust, Delphi, packers courants) pour **éviter** de les promouvoir en similarité malveillante. Un imphash seul n'est pas une preuve de parenté.

## Analyse ciblée

- Définir une question falsifiable : quel chemin mène à la config, quelle donnée alimente la fonction réseau, quelle persistance peut être créée ?
- Avec Ghidra si disponible : list_functions/imports, search, xrefs vers/depuis une adresse, decompile_function ; relever adresse, appelants, branches, chaînes et limitations de décompilation.
- Construire une chaîne « entrée → condition → donnée → transformation → effet potentiel », avec observation pour chaque maillon. Des imports de WinHTTP, CryptoAPI, ETW ou registre ne prouvent aucun appel effectué.
- Pour ressources ou données opaques, localiser précisément offset de fichier/section, lire seulement des plages bornées via le catalogue attesté ; ne pas traiter un RVA comme offset. Vérifier toute transformation par les bytes observés, sans lancer le code du sample.
- Envisager comparaison de fonctions/features et signatures candidates seulement si plusieurs échantillons mesurables existent ; garder séparées similarité structurelle, lien d'infrastructure et attribution.

## Attention aux formes composites

- PE managé ou mixte : basculer vers autowork-dotnet-analysis pour metadata CLI, tout en conservant le parcours natif nécessaire.
- PE auto-extracteur ou archive embarquée : inventaire puis dérivation membre, provenance parent→enfant, limites d'extraction.
- DLL : DllMain, exports et callbacks sont des chemins potentiels, pas la preuve qu'un processus les a invoqués.
- Loader/packer : un déballage dynamique non autorisé n'est jamais une étape implicite ; noter la région non visible.

## Conclusions et sortie

Produire un tableau « propriété mesurée / fonction ou section / observation + SHA / portée / hypothèse alternative / prochaine vérification ». Un finding supported peut établir une chaîne d'appels statique observée ; le comportement réseau, l'exécution et le résultat réel requièrent d'autres preuves.

**Oracle :** un import réseau partagé et un imphash identique ne deviennent pas une appartenance de famille ; le skill sélectionne une comparaison discriminante plutôt qu'une décompilation exhaustive.