---
name: autowork-artifact-triage
description: Identifier le véritable format d'un artefact CTI et sélectionner le parcours statique PE natif, .NET, ELF, archive, document, script ou capture. Charger au premier triage d'un objet en operator_led.
---

# Routeur d'artefacts

Ne déduire ni le format ni le comportement d'une simple extension, d'un nom VT ou d'un label AV. La preuve vient d'en-têtes/structures observés dans l'objet exact.

## Parcours commun

1. Établir SHA-256, association à la présente enquête, origine, taille, version et observation. Documented hash ≠ octets acquis et associés.
2. Relire les observations existantes, puis identifier statiquement le format réel via les capacités exposées (sample.get_feature_pack, sample.analyze, commande file si présente). Signaler polyglotte, corruption, données partielles.
3. Formuler une question analytique ; ne pas lancer tous les outils par réflexe.
4. Dispatcher selon la structure réellement observée, puis relier chaque nouveau résultat à son objet, son invocation et sa couverture.
5. Pour chaque sous-objet extrait : créer/obtenir l'association autorisée et conserver parent→membre→hash, sans fusionner les preuves du conteneur et du contenu.

## Choix du parcours

| Signature ou structure constatée | Première voie | Limite à garder |
| --- | --- | --- |
| PE avec CLI metadata | autowork-dotnet-analysis | Une méthode InitializeNewDomain n'est pas la preuve de son invocation |
| PE natif / COFF | autowork-pe-native-analysis | Imports et chemins statiques ne sont pas des appels observés |
| PE packé/opaque | autowork-pe-native-analysis (et .NET si CLI) | Manque de strings ≠ absence de code ou config |
| ELF | readelf/objdump/strings puis Ghidra si réellement offerts | Bibliothèque importée ≠ fonction appelée |
| ZIP/7z/tar ou conteneur composite | Lister membres avant extraction ciblée | Vérifier hash, décompression, chemin et limites ; ne pas deviner |
| PDF, Office, RTF, HTML, XML/MSBuild | Parser/lecture statique disponible ; examiner liens et embeds | Ne pas prétendre analyser des macros sans outil réel |
| PowerShell, JS, VBS, shell, batch | AST/texte statique, paramètres, décodages, dépendances | Instruction conditionnelle ≠ exécution réussie |
| PCAP, flux, rapport sandbox | Voie réseau si disponible | Conditions de collecte, horodatage et couverture |
| Chiffré/inconnu | Identité et couverture ; état inconclusive | Ne pas classer de force |

## Questions de triage

- L'objet est-il un conteneur, un loader, un fichier de config, une bibliothèque ou une capture ? Quelles preuves l'établissent ?
- Les octets et moteurs sont-ils réellement accessibles, ou seulement des métadonnées externes ?
- Quels points d'entrée, imports, ressources, méthodes, scripts ou membres changeraient la question ?
- Une absence de résultat vient-elle d'un outil, d'un filtre, d'une région non couverte ou d'une hypothèse fausse ?
- Quel finding atomique est déjà vérifiable sans inférer de comportement ?

Pour une donnée opaque, identifier stockage, plage et transformateur ; vérifier qu'une commande confinée comme xxd est annoncée et récupérer les octets par offset de fichier si possible, sans exécuter le malware.

## Sortie de triage

object_id, sample SHA, format et observation exacts, provenance/parent, couverture, outils réellement disponibles, route choisie, question à trancher et première action proportionnée.

**Oracle :** une archive contenant un XML et un PE produit des objets distincts ; une DLL renommée n'est pas automatiquement .NET ; des macros Office non analysables restent UNKNOWN.