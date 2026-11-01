---
name: autowork-pe-native-analysis
description: Analyser un PE natif ou packé pour découvrir ressources, invariants YARA, fonctions C2 et variantes ; choisir des chemins de rétro-ingénierie proportionnés.
---

# PE natif : de la structure à une question de comportement

L'extension .exe/.dll, le nom fournisseur et le langage supposé ne suffisent pas à qualifier le binaire. Préserver SHA et association exacts. Ne jamais exécuter le PE ou contourner le confinement.

## Profil initial

1. Identifier format, architecture et régions pertinentes depuis les packs existants. Approfondir les sections, imports/exports, ressources, TLS callbacks ou entry point **selon la question**, sans exiger de profil exhaustif.
2. Utiliser en priorité packs locaux existants (sample.get_feature_pack, sample.analyze s'il est offert), strings.list et local.run_command pour file/objdump/xxd si autorisés. Conserver provenance des résultats CAPA, SMDA et statiques ; une annotation CAPA est une capacité déduite, pas une exécution observée.
3. Caractériser packing/obfuscation : entropie, sections anormales, imports rares, import resolution et tables manquantes sont des indices ; ils ne prouvent pas un packer précis.
4. Repérer signatures génériques de toolchain (CRT, libstdc, Go, Rust, Delphi, packers courants) pour **éviter** de les promouvoir en similarité malveillante. Un imphash seul n'est pas une preuve de parenté.

## Analyse ciblée

- Choisir une cible CTI productive : configuration, construction des messages, routine de déchiffrement, ressource distinctive ou code commun. Pour une inspection directe, nul besoin de protocole expérimental formel.
- Avec Ghidra si disponible : list_functions/imports, search, xrefs vers/depuis une adresse, decompile_function ; relever adresse, appelants, branches, chaînes et limitations de décompilation.
- Construire une chaîne « entrée → condition → donnée → transformation → effet potentiel », avec observation pour chaque maillon. Des imports de WinHTTP, CryptoAPI, ETW ou registre ne prouvent aucun appel effectué.
- Pour ressources ou données opaques, localiser précisément offset de fichier/section, lire seulement des plages bornées via le catalogue attesté ; ne pas traiter un RVA comme offset. Vérifier toute transformation par les bytes observés, sans lancer le code du sample.
- Extraire des motifs YARA candidats même à partir d'un seul spécimen clairement limité ; en présence de variantes, comparer fonctions/features pour isoler des invariants et pivoter vers des samples additionnels. Un seul résultat n'autorise pas une validation de famille.

## Attention aux formes composites

- PE managé ou mixte : basculer vers autowork-dotnet-analysis pour metadata CLI, tout en conservant le parcours natif nécessaire.
- PE auto-extracteur ou archive embarquée : inventaire puis dérivation membre, provenance parent→enfant, limites d'extraction.
- DLL : DllMain, exports et callbacks sont des chemins potentiels, pas la preuve qu'un processus les a invoqués.
- Loader/packer : un déballage dynamique non autorisé n'est jamais une étape implicite ; noter la région non visible.

## Conclusions et sortie

Conserver les caractéristiques vérifiées, leur provenance et l'usage possible : motif YARA, piste VT, élément de protocole/crypto, hypothèse Suricata. Utiliser autowork-network-protocol-detection lorsqu'un parsing/framing C2 est identifiable. Une fonction statique observée n'est pas la preuve d'une communication exécutée. Le finding est facultatif selon l'utilité et le contrat.

**Oracle :** une caractéristique native spécifique alimente une candidate YARA ou un pivot, sans exiger d'analyse exhaustive ; les imports communs restent des pistes, non des preuves d'appartenance.