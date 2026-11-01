---
name: autowork-yara-bridge-workflow
description: Construire et réviser une règle YARA candidate via observations exactes et aide Bridge, puis compiler, tester et qualifier sur positifs/goodware/holdout. À charger lorsqu'une détection YARA est l'objectif.
---

# YARA + Bridge : concevoir, éprouver, qualifier

**Principe :** Bridge conseille, les moteurs locaux compilent/testent, les observations canoniques fondent les assertions, l'analyste qualifie. Un texte Bridge n'est pas un motif prouvé et un match sur le sample d'origine n'est pas une validation de famille.

## 1. Prérequis

- Vérifier G2/TLP, external_llm_allowed, autorisation de consultation, budgets et runtime ; aucun envoi Bridge si non autorisé.
- Vérifier l'inventaire réel : rule.propose_bridge, rule.propose_from_candidates, rule.propose, rule.test, rule.revise, rule.compare, rule.get, advisor.consult et advisor.reparse sont optionnels selon les ports.
- Vérifier samples associés, octets réellement lisibles, corpus positives, goodware, holdout et provenance. Unknown ou unavailable restent explicites.
- Définir la cible de la règle : spécimen exact, groupe d'artefacts ou famille hypothétique. Ne pas promouvoir cette dernière sans positifs qualifiés.

## 2. Sélectionner des motifs

Extraire des observations mesurées : octets invariants d'une ressource, chaînes spécifiques, structures rares, associations de caractéristiques stables, signatures candidates de features.common / signatures.candidates.

Pour chaque motif consigner sample SHA, offset/section ou référence de méthode, observation ID/SHA, degré d'ubiquité/banalité, transformations, encodage et risque de faux positifs. Écarter les chaînes de runtime, bibliothèques ou chemins génériques, les offsets non vérifiés, les marqueurs présents dans un seul échantillon sans justification et les données tronquées.

Comparer plusieurs échantillons et sous-ensembles si disponibles. Ne pas utiliser des chaînes non récupérables ou des « mesures » inventées pour satisfaire le validateur.

## 3. Consultation Bridge : si utile

Employer advisor.consult avec consultation yara_draft si disponible et pertinente. Le dossier ciblé contient objectif en prose, faits observés et refs autorisées, motifs envisagés, absences de couverture, contre-exemples et objectif de généralisation.

Demander à Bridge de critiquer spécificité, fragilité, faux positifs, conditions et propositions de règle **en prose / code YARA selon le contrat**, jamais de répondre en JSON ou de mettre la réponse dans un bloc JSON. Un contrat Bridge ou parseur peut produire des résultats partiels ; ne pas considérer un statut succeeded comme validation sémantique.

Ne répéter aucune soumission ambiguë ; si une réponse persistée est mal interprétée, envisager advisor.reparse avec sa version de schéma réellement annoncée, sans nouvel effet externe.

## 4. Enregistrer et tester

- Voie Bridge : rule.propose_bridge exige les champs réels bridge_observation_id, bridge_invocation_id et rule_source_sha256 du **rule_source complet persisté** ; ne pas les inventer à partir d'un résumé. Vérifier la version du schéma avant l'appel.
- Voie mesurée locale : rule.propose_from_candidates seulement si les candidats et leurs provenances P09/L2 sont réellement disponibles ; sinon rule.propose avec motifs vérifiés et provenance compatible avec son schéma.
- rule.operator_syntax_fix ne répare que la syntaxe YARA ; ne prouve ni qualité ni détection.
- Une proposition peut compiler sans être validée : lancer explicitement rule.test, consulter rule.get et le rapport canonique.
- Mesurer par hash et rôle : positifs qualifiés, candidats hors cluster, goodware, holdout indépendant. Distinguer matches, non_matches, non_measurable, erreurs et chevauchement des corpus. Un absent byte n'est PAS un non-match.
- Un match goodware mesuré invalide la spécificité de la version testée ; un unique match initial sans autres positifs donne au mieux une règle expérimentale. Un holdout utilisé pour l'optimisation n'est plus indépendant.

## 5. Réviser avec hypothèse de diagnostic

Sur faux négatif confirmé, chercher si le motif est non invariant, la condition trop stricte ou l'échantillon hors cluster. Sur faux positif, identifier la sous-expression banale et son contexte. Changer une hypothèse à la fois ; utiliser rule.revise avec parent_version courant puis rule.test. Comparer les versions sur le même corpus avec rule.compare si disponible.

Ne pas optimiser au hasard jusqu'à faire matcher l'échantillon test. Le status candidate/experimental/tested/rejected reste défini par le workbench et la couverture effective, pas par l'intention de l'agent.

## Sortie et arrêt

- Objectif, périmètre et motivation des motifs.
- SHA/version de source YARA et références des mesures.
- Bridge invocation et observation si utilisées ; conseil accepté/rejeté avec raison.
- Corpus exact par rôle, versions moteur, matches/non_matches/non_measurable et limites.
- Détection candidate, statut de validation réel, risques de faux positifs et condition de reprise.
- Approbation/publication : exclusivement humains, hors de ce skill.

**Oracle :** la règle M08 qui matche 1/10 fichiers non qualifiés reste expérimentale/non validée, même après une consultation Bridge ; l'agent demande des positifs qualifiés, du goodware mesurable et un holdout indépendant avant de prétendre à une règle testée.