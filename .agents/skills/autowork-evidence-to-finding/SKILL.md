---
name: autowork-evidence-to-finding
description: Conserver les faits CTI déterminants pour la détection, le C2 et les pivots sous forme de findings proportionnés, sans exiger de finding à chaque lecture.
---

# Observation → fait atomique → finding

Un finding n'est ni une hypothèse ni une réponse Bridge. Proposer un finding **lorsqu'il apporte une connaissance persistante utile** ; les résultats intermédiaires peuvent rester dans les observations d'outils. Ne jamais bloquer une règle candidate, un pivot ou une rétro-analyse sur l'absence d'un finding.

## Étapes

1. Relire observation ID, SHA, invocation, objet source exact, version, résultat et limites de couverture ; une response_ref illisible n'est pas suffisante.
2. Formuler une assertion bornée mais utile : invariant binaire, structure d'un message, existence d'une méthode ou relation directement observée. Exemple : une classe dérivée de System.AppDomainManager ne prouve pas qu'elle a été activée.
3. Chercher une hypothèse concurrente ou une restriction de portée. Séparer présence de code, atteignabilité, activation, exécution et effet réel.
4. Décider du statut : supported pour constat direct étroit ; qualified pour interprétation incomplète ; inconclusive pour une question ouverte. Empty/unavailable/partial ne fondent pas une réfutation globale.
5. Lire le schéma réel de finding.propose ou finding.revise et les références permises. Utiliser l'object_id canonique attendu pour subject_ref : ne pas fabriquer un préfixe « sha256: » ou substituer un autre type de référence.
6. Conserver les IDs/SHA des observations, la version et l'invocation qui soutiennent **exactement** l'assertion. Si la même constatation existe déjà, ne pas la dupliquer.
7. Après persistance, relire finding et provenance ; vérifier qu'aucun champ ne dépasse la couverture des observations.

## Audit et prose livrée

La provenance (observations, versions, auteurs, hashes) vit dans la couche canonique. Le texte destiné au lecteur n'imprime aucun marqueur interne (`SUPPORTED`, `PARTIAL`, `bridge_authored`, `observation_ref`, `confidence=`) ; les affirmations techniques importantes doivent rester retrouvables dans l'état canonique par un auditeur externe.

## Comparaison de portée

| Mesure | Fait pouvant être supported | Extrapolation interdite |
| --- | --- | --- |
| Fonction IL | Méthode et appel présents dans le code inspecté | Exécution effective |
| Rapport VT | Le rapport associe un nom au hash | Nom confirmé sur disque / attribution |
| Strings | Octets présents dans la plage inspectée | Utilisation réelle / absence ailleurs |
| Capture | Champ et paquets effectivement capturés | Trafic de l'hôte non capturé |
| Membre ZIP | Membre avec hash mesuré | Exécution du membre |
| Match YARA | Match sur les cas mesurables exacts | Validation d'une famille entière |

Ne pas forcer un finding pour augmenter un compteur. Si les observations sont incomplètes, qualifier la limite dans l'artefact disponible ; ne pas créer une expérience ni un finding uniquement pour justifier la poursuite de l'analyse. La revue critique peut mobiliser autowork-cti-evidence-gate sans demander au reviewer d'effectuer une mutation.

**Oracle :** l'opérateur conserve l'observation d'un invariant utile à YARA/C2 sans attendre un finding ; lorsqu'il propose un finding, la portée et l'identité de l'objet respectent le contrat, y compris les limites .NET subject_ref.