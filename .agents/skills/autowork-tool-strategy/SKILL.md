---
name: autowork-tool-strategy
description: Choisir rapidement les outils CTI utiles à l'analyse, aux règles YARA/Suricata, aux pivots VT et au protocole C2, en récupérant les erreurs sans formalités superflues.
---

# Stratégie d'outillage

Ce skill n'est PAS un catalogue fixe. L'autorité est available_tools, unavailable_tools, blocked_capabilities, status et le schéma effectif de CETTE enquête. Un outil historique ne vaut pas permission actuelle.

## Un appel auditable

Le `purpose` de chaque appel est le journal de décision : objectif, motif synthétique du choix, ce que le résultat doit apprendre, et si l'appel donne suite à un conseil Bridge (voir `autowork-bridge-collaboration`). Une phrase factuelle suffit ; ne pas y mettre de raisonnement privé ni de secret.

## Découverte proportionnée

1. Au début de la session, lire l'inventaire réel available_tools et les capacités importantes. Ensuite, consulter **seulement** le schéma de l'outil choisi lorsqu'il est inconnu, a changé ou refuse un argument ; éviter les relectures systématiques.
2. Pour l'objet pertinent, vérifier association, octets, worker et droits uniquement si ce sont de vrais prérequis de l'appel. Le backend reste l'autorité d'admission et de budget ; ne pas inventer de nouvelle approbation, de limite de messages Bridge ni de quota par phase.
3. Choisir la voie donnant un résultat métier : décoder des données, modéliser le C2, construire une candidate, rechercher une variante. Une lecture locale rapide est parfois préférable ; une recherche VT passive autorisée ou une consultation Bridge peut aussi être la meilleure prochaine action.
4. Les outils locaux indépendants compatibles avec la logique M10 n'avancent pas nécessairement le working state ; les écritures analytiques, acquisitions et effets externes conservent leur contrat spécifique. Se baser sur le comportement réel, pas sur une règle uniforme « chaque POST incrémente le CAS ».

## Carte indicative à vérifier dans le registre

| Question | Familles à inspecter | Préflight décisif |
| --- | --- | --- |
| Texte article / preuve | article.get_claims, evidence.get, graph.neighbors | Claim/observation exacts et lisibles |
| Statique PE/ELF | sample.get_feature_pack, sample.analyze, strings.list, local.run_command | Association + bytes + worker |
| IL .NET | assembly.inspect, types.list, methods.list, method.decompile, resources.list, references.imports | Assembly managé, couverture de méthode |
| Natif | ghidra.list_functions/imports, ghidra.xrefs, ghidra.decompile_function, ghidra.search | Service attesté, adresse valide |
| Données .NET opaques | field.data.read si annoncé ; sinon local.run_command/xxd si offset connu | FieldRVA résolu par l'outil .NET, ou offset fichier établi ; RVA ≠ offset |
| Archives | local.run_command zip_members/archive_list, archive.extract_member | Membre exact, hash et limites |
| Similarité | samples.compare, features.common/explain, signatures.candidates | Samples comparables et mesures réelles |
| VT | vt.file_report, vt.relationships, vt.behaviour, vt.intelligence_search | Source, G2/TLP, utilité du pivot |
| YARA | rule.propose, rule.propose_bridge, rule.propose_from_candidates, rule.test, rule.compare | Candidate possible avec corpus incomplet ; statut selon résultats mesurés |
| Bridge | advisor.consult, advisor.reparse | Dossier ciblé, droits, effet et coût |
| C2 / Suricata | network.capture.inspect, network.stream.reassemble, network.messages.segment, network.protocol.*, network.suricata_*, advisor.consult (suricata_draft) | Analyse statique possible sans PCAP ; replay Suricata nécessite une capture associée et le moteur disponible |

Pour YARA Bridge, `rule.propose_bridge` conserve la source exacte comme candidate attribuée, avec un gap explicite pour chaque string sans mesure liée. `rule.operator_syntax_fix` permet aussi `revision_mode="full_source"` pour une source opérateur dérivée de la v1 Bridge. Les résultats incluent `{stage, status, reason_code}` et un lifecycle dérivé (`candidate`, `compiled`, `tested`, `validated`) ; après proposition ou révision, appeler `rule.test` sur la candidate.

## Réactions aux réponses

- Réponse Bridge partielle ou prose seule : conserver et lire le texte brut, exploiter les sections, règles, pivots et avertissements reconnus ; garder la prose inconnue visible. Distinguer reçu, analysé par le parseur, techniquement exploitable, testé, vérifié et artefact validé.
- Erreur de schéma : relire la version réelle, required et enums ; corriger le champ.
- Policy_denied : signaler la garde ; ne pas contourner par un nouveau nom/ID.
- Unavailable : identifier capacité, pack ou worker manquant ; alternative ou blocage.
- Empty : expliciter les paramètres et la couverture, jamais réfutation générale.
- 409 stale_working_state : relire version/SHA courants, vérifier les effets, refaire une **nouvelle action correcte** uniquement lorsque l'absence de doublon est établie.
- stale_experiment_version : relire experiment.list ; version du flux ≠ version working state.
- transport_error/needs_review : retrouver invocation et relais, ne pas renvoyer un POST ambigu. Réconciliation seulement par le chemin/acteur autorisé.
- Sortie tronquée : paginer l'observation canonique ; ne pas reconstruire les octets manquants.

Reporter les coûts fournisseur UNKNOWN si non mesurés ; ne pas transformer octets ou tokens estimés en facturation.

**Oracle :** pour un champ .NET, découvrir field.data.read avant d'inventer un lecteur RVA ; un blocage YARA/Suricata ne bloque pas les autres pistes ; pour un effet externe ambigu, aucun doublon n'est émis.
