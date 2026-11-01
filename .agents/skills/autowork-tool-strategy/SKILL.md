---
name: autowork-tool-strategy
description: Choisir les capacités CTI réellement disponibles, vérifier schémas/prérequis/coûts, puis récupérer des erreurs CAS ou refus sans rejouer un effet ambigu. À utiliser en enquête avant un nouveau type d'outil ou après un échec.
---

# Stratégie d'outillage

Ce skill n'est PAS un catalogue fixe. L'autorité est available_tools, unavailable_tools, blocked_capabilities, status et le schéma effectif de CETTE enquête. Un outil historique ne vaut pas permission actuelle.

## Découverte et choix

1. Lire status compact et les pages de available_tools avec le curseur et CAS exacts. Pour chaque candidat seulement : nom, schema_version, arguments required, effet, limites, coût et dépendances.
2. Croiser available_pivots, indicator_inventory, documented_samples, sample_deltas et evidence_index. Un pivot « available » peut être refusé si ses références de provenance sont insuffisantes.
3. Vérifier **pour cet objet exact** : association à l'enquête, octets en quarantaine, worker attesté, capacité du fournisseur, droits et budget.
4. Distinguer GET de preuve, calcul local POST, lecture externe VT, acquisition et consultation Bridge. Un outil local peut créer des observations, consommer du budget et incrémenter le working state.
5. Choisir le test qui peut changer la décision ; préférer la lecture canonique existante au calcul redondant et le calcul local à un appel externe si celui-ci suffit.

## Carte indicative à vérifier dans le registre

| Question | Familles à inspecter | Préflight décisif |
| --- | --- | --- |
| Texte article / preuve | article.get_claims, evidence.get, graph.neighbors | Claim/observation exacts et lisibles |
| Statique PE/ELF | sample.get_feature_pack, sample.analyze, strings.list, local.run_command | Association + bytes + worker |
| IL .NET | assembly.inspect, types.list, methods.list, method.decompile, resources.list, references.imports | Assembly managé, couverture de méthode |
| Natif | ghidra.list_functions/imports, ghidra.xrefs, ghidra.decompile_function, ghidra.search | Service attesté, adresse valide |
| Octets opaques | local.run_command avec commande xxd si annoncée | OFFSET FICHIER vérifié, taille bornée ; RVA ≠ offset |
| Archives | local.run_command zip_members/archive_list, archive.extract_member | Membre exact, hash et limites |
| Similarité | samples.compare, features.common/explain, signatures.candidates | Samples comparables et mesures réelles |
| VT | vt.file_report, vt.relationships, vt.behaviour, vt.intelligence_search | Source, G2/TLP, utilité du pivot |
| Détection | rule.propose, rule.propose_bridge, rule.test, rule.compare | Workbench et corpus réellement disponibles |
| Bridge | advisor.consult, advisor.reparse | Dossier ciblé, droits, effet et coût |
| Réseau | network.capture.inspect, network.stream.reassemble, network.protocol.* | Capture et provenance vérifiées |

## Réactions aux réponses

- Erreur de schéma : relire la version réelle, required et enums ; corriger le champ.
- Policy_denied : signaler la garde ; ne pas contourner par un nouveau nom/ID.
- Unavailable : identifier capacité, pack ou worker manquant ; alternative ou blocage.
- Empty : expliciter les paramètres et la couverture, jamais réfutation générale.
- 409 stale_working_state : relire version/SHA courants, vérifier les effets, refaire une **nouvelle action correcte** uniquement lorsque l'absence de doublon est établie.
- stale_experiment_version : relire experiment.list ; version du flux ≠ version working state.
- transport_error/needs_review : retrouver invocation et relais, ne pas renvoyer un POST ambigu. Réconciliation seulement par le chemin/acteur autorisé.
- Sortie tronquée : paginer l'observation canonique ; ne pas reconstruire les octets manquants.

Reporter les coûts fournisseur UNKNOWN si non mesurés ; ne pas transformer octets ou tokens estimés en facturation.

**Oracle :** pour un blob .NET, essayer d'établir xxd et RVA→offset avant de demander un lecteur neuf ; pour un effet externe ambigu, aucun doublon n'est émis.