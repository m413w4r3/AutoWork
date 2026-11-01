---
name: autowork-artifact-triage
description: Triage CTI rapide par format et dispatch vers extraction d'invariants, C2, YARA/Suricata ou nouveaux échantillons, sans imposer une analyse exhaustive.
---

# Routeur d'artefacts

Ne déduire ni le format ni le comportement d'une simple extension, d'un nom VT ou d'un label AV. La preuve vient d'en-têtes/structures observés dans l'objet exact.

## Parcours orienté résultat

1. Identifier l'objet exact (SHA-256/association) et son format réel depuis les observations déjà disponibles ; une extension ou un nom VT n'est qu'un indice.
2. Choisir une piste présentant une valeur immédiate : configuration, constantes, code de communication, ressources, enfant d'archive ou indicateur discriminant. Lire le minimum nécessaire, pas tous les moteurs par principe.
3. Extraire un invariant candidat et noter où il a été observé ; lorsqu'un membre est extrait, conserver la relation parent→membre→hash. Les observations d'une autre enquête ne deviennent pas des preuves par ressemblance.
4. Router vers le guide spécialisé utile. Une analyse statique peut produire YARA ou hypothèses de protocole même sans capture ; elle ne démontre pas une communication réellement exécutée.

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
| PCAP, flux, rapport sandbox | autowork-network-protocol-detection ; inspect/reassemble/segment si exposés | Suricata replay seulement sur capture autorisée ; hypothèse C2 statique possible sans PCAP |
| Chiffré/inconnu | Identité et couverture ; état inconclusive | Ne pas classer de force |

## Questions de triage

- L'objet est-il un conteneur, un loader, un fichier de config, une bibliothèque ou une capture ? Quelles preuves l'établissent ?
- Les octets et moteurs sont-ils réellement accessibles, ou seulement des métadonnées externes ?
- Quels points d'entrée, imports, ressources, méthodes, scripts ou membres changeraient la question ?
- Une absence de résultat vient-elle d'un outil, d'un filtre, d'une région non couverte ou d'une hypothèse fausse ?
- Quel finding atomique est déjà vérifiable sans inférer de comportement ?

Pour une donnée opaque .NET, utiliser d'abord field.data.read si offert (FieldRVA → octets bornés) ; xxd reste une voie complémentaire si un offset fichier fiable est connu. Chercher ensuite chaînes/structures exploitables, invariants et pistes de pivot sans exécuter le malware.

## Sortie de triage

Noter l'objet, le format étayé, la piste à forte valeur et le prochain outil. Dès qu'un motif, un indice de C2 ou un nouvel enfant est découvert, passer à la détection ou au pivot ; ne pas attendre un triage exhaustif.

**Oracle :** une archive contenant un XML et un PE produit des objets distincts ; une DLL renommée n'est pas automatiquement .NET ; des macros Office non analysables restent UNKNOWN.