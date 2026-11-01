---
name: autowork-cti-investigation-loop
description: Conduire un cycle CTI operator_led, de la question falsifiable à une expérience enregistrée, un finding éventuel et une décision d'arrêt. À charger en enquête CTI, pas pour coder AutoWork.
---

# Boucle de décision CTI

Autorité : contrats, schémas et enveloppe G2/TLP de CETTE enquête. Les observations versionnées font foi, pas ce skill, un rapport d'agent ou Bridge. Aucune autorisation nouvelle n'est conférée.

## Préflight

1. Lire le statut compact et le contexte summary. Noter investigation ID, working_state version/SHA, G2 et révocation, TLP, budgets consommé/réservé/restant, invocations ambiguës.
2. Parcourir available_tools, available_pivots et les sections ciblées avec les curseurs et CAS serveur. Ne pas relire le statut complet à chaque étape.
3. Lire les observations, claims, associations, hypothèses et expériences canoniques déjà disponibles. Une ancienne enquête n'est pas une preuve de la nouvelle.

## Un cycle = une question décidable

1. **Question :** définir l'objet précis et la propriété à vérifier ; « analyser davantage » n'est pas une question.
2. **Alternatives :** formuler au moins deux hypothèses discriminables et une issue inconnue. Les déclarer avec hypothesis.declare AVANT un nouveau test lorsque le contrat le prévoit.
3. **Couverture :** inventorier observations, contre-exemples et angles morts.
4. **Candidats :** comparer plusieurs tests utiles, pas simplement plusieurs outils. Appliquer autowork-cti-pivot-strategy et autowork-tool-strategy.
5. **Prédictions AVANT l'appel :** noter résultat attendu sous chaque hypothèse, falsificateur, coût et risque.
6. **Exécution :** vérifier schéma/version, autorisation, budget, idempotence, CAS et effet réel avant POST.
7. **Lecture :** retrouver les observations canoniques avec IDs, hashes, versions et limitations ; le résumé du sous-agent n'est pas la preuve.
8. **Expérience :** lire experiment.list pour la version de FLUX, distincte du working state. Pour experiment.record reprendre les IDs/version des invocations, IDs/SHA observations, input_refs et champs exigés par le schéma réel ; aucune date ni référence inventée. Une erreur de validation exige une correction ciblée, pas une répétition.
9. **Finding :** examiner tout fait atomique nouveau au moyen de autowork-evidence-to-finding, sans forcer son enregistrement.
10. **Décision :** continuer, changer de branche, déléguer une question indépendante, arrêter avec limites ou clore.

## Lecture des résultats

- Supported : strictement ce qui est directement étayé dans la couverture observée.
- Qualified : portion établie, implication plus large encore incertaine.
- Inconclusive : test non discriminant.
- Empty : rien trouvé dans les paramètres étudiés, PAS une absence globale.
- Partial/truncated : seulement une partie du périmètre couverte.
- Unavailable/blocked/policy_denied : pas de preuve négative.
- Needs_review/transport_unknown : effet externe potentiellement ambigu ; réconcilier avant toute reprise équivalente.

## Coordination et clôture

Une délégation porte sur UNE question, avec refs, permissions, oracle et critères d'arrêt. Les lectures peuvent être parallèles ; les mutations canoniques sont ordonnancées suivant les garanties effectives du backend. Le parent vérifie les observations avant tout finding.

Arrêter si les tests décisifs sont couverts, les alternatives restantes redondantes/disproportionnées, ou la capacité/preuve indispensable absente. Conserver hypothèses, expériences, conclusions bornées, coûts mesurés ou UNKNOWN et condition précise de reprise. La fin de session n'est pas un verdict CTI.

## Fiche de décision minimale

- Question / objet / claim
- Hypothèses concurrentes et preuve antérieure
- Trois tests envisagés, coût et justification du choix
- Prédiction et falsificateur AVANT exécution
- Invocation, résultat et observations canoniques
- Interprétation, couverture, contre-exemples
- Expérience / finding (ou justification de non-proposition)
- Décision de branche et condition de reprise

**Oracle :** lors d'un replay contrôlé, les prédictions précèdent tout test neuf, les preuves sont de la même enquête et une sortie empty/partial ne devient jamais une réfutation.