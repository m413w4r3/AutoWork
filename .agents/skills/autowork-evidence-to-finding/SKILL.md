---
name: autowork-evidence-to-finding
description: Proposer des findings CTI atomiques dont la portée et l'objet correspondent exactement aux observations canoniques, sans promouvoir un claim large. Charger après un cycle informatif.
---

# Observation → fait atomique → finding

Un finding n'est pas une hypothèse, un avis Bridge ou un rapport de sous-agent. Ne proposer que la propriété directement étayée par une observation persistée de la même enquête.

## Étapes

1. Relire observation ID, SHA, invocation, objet source exact, version, résultat et limites de couverture ; une response_ref illisible n'est pas suffisante.
2. Formuler l'assertion MINIMALE. Exemple : « cet assembly définit un type dérivant de System.AppDomainManager et une méthode InitializeNewDomain » ; ne pas prétendre qu'il a été chargé ou qu'il désactive ETW.
3. Chercher une hypothèse concurrente ou une restriction de portée. Séparer présence de code, atteignabilité, activation, exécution et effet réel.
4. Décider du statut : supported pour constat direct étroit ; qualified pour interprétation incomplète ; inconclusive pour une question ouverte. Empty/unavailable/partial ne fondent pas une réfutation globale.
5. Lire le schéma réel de finding.propose ou finding.revise et les références permises. Utiliser l'object_id canonique attendu pour subject_ref : ne pas fabriquer un préfixe « sha256: » ou substituer un autre type de référence.
6. Conserver les IDs/SHA des observations, la version et l'invocation qui soutiennent **exactement** l'assertion. Si la même constatation existe déjà, ne pas la dupliquer.
7. Après persistance, relire finding et provenance ; vérifier qu'aucun champ ne dépasse la couverture des observations.

## Comparaison de portée

| Mesure | Fait pouvant être supported | Extrapolation interdite |
| --- | --- | --- |
| Fonction IL | Méthode et appel présents dans le code inspecté | Exécution effective |
| Rapport VT | Le rapport associe un nom au hash | Nom confirmé sur disque / attribution |
| Strings | Octets présents dans la plage inspectée | Utilisation réelle / absence ailleurs |
| Capture | Champ et paquets effectivement capturés | Trafic de l'hôte non capturé |
| Membre ZIP | Membre avec hash mesuré | Exécution du membre |
| Match YARA | Match sur les cas mesurables exacts | Validation d'une famille entière |

Ne pas forcer un finding pour augmenter un compteur. Si les observations sont incomplètes, consigner proprement la limite dans l'expérience et le prochain test qui pourrait établir le fait. La revue critique peut mobiliser autowork-cti-evidence-gate sans demander au reviewer d'effectuer une mutation.

**Oracle :** un claim ETW reste inconclusive alors que des faits .NET directement observés et bien référencés peuvent former des findings supported distincts.