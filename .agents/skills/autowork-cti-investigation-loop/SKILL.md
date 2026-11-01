---
name: autowork-cti-investigation-loop
description: Faire progresser l'enquête CTI vers les invariants, les règles YARA/Suricata, le protocole C2 et de nouveaux échantillons, sans rituel ni quotas de tests. Pour operator_led uniquement.
---

# Boucle de décision CTI

Autorité : contrats, schémas et enveloppe G2/TLP de CETTE enquête. Les observations versionnées font foi, pas ce skill, un rapport d'agent ou Bridge. Aucune autorisation nouvelle n'est conférée.

## Cap et mode opératoire

Les livrables métier priment sur la conformité à une checklist : invariants de code/données/protocole, YARA candidate, Suricata candidate, description C2 et nouveaux échantillons qualifiés. Une découverte peut alimenter une règle, la règle un pivot, puis un nouvel échantillon une révision. Choisir une branche productive et suivre ses résultats.

Au démarrage ou après un changement matériel, lire le statut et le contexte utiles : enquête, autorisations G2/TLP, capacités disponibles, budgets et éventuelles invocations ambiguës. Ne pas repaginer tous les outils ni relire tout le statut à chaque action.

### Boucle souple

1. **Choisir une cible utile :** octets opaques, méthode, message réseau, invariant candidat, règle ou nouveau hash. Dire brièvement ce qu'apporterait la prochaine lecture.
2. **Faire l'analyse :** utiliser le meilleur outil réellement disponible. Une inspection directe d'une ressource ou d'une méthode ne nécessite ni liste de contre-hypothèses ni comparaison de trois tests.
3. **Formaliser quand c'est utile :** pour une question causale disputée ou une expérience destinée au journal, expliciter hypothèse, alternative et critère discriminant **avant** le test. Déclarer via hypothesis.declare uniquement lorsque le contrat de l'action l'exige ou que cela aide réellement l'enquête. Ne pas antidater.
4. **Lire le résultat :** conserver les observations canoniques pertinentes, l'objet, leur couverture, les réserves et les contre-exemples utiles ; ne pas recharger d'anciennes preuves sans besoin.
5. **Exploiter immédiatement la découverte :** tester si elle fournit un invariant, un motif YARA, un critère Suricata, une étape du protocole C2 ou un pivot de découverte. Consigner le finding pertinent lorsqu'il existe, sans le rendre obligatoire pour chaque opération.
6. **Persister au bon niveau :** une expérience formelle utilise experiment.record et la version du flux experiment.list, avec les références de provenance exactes. Un constat technique simple n'exige pas toujours un nouvel enregistrement formel ; utiliser les observations déjà produites par les outils.
7. **Enchaîner ou changer de piste :** continuer tant qu'une décision ou un artefact de détection peut progresser ; arrêter une branche stérile sans immobiliser les autres objectifs.

Une règle candidate ou un modèle C2 partiel sont des livrables utiles. L'absence de positifs indépendants, de goodware, de holdout, de capture ou de protocole complet **limite le verdict**, mais n'interdit pas une proposition expérimentale ou une exploration autorisée. Un résultat qualifié ne devient pas validé par simple intention.

## Lecture des résultats

- Supported : strictement ce qui est directement étayé dans la couverture observée.
- Qualified : portion établie, implication plus large encore incertaine.
- Inconclusive : test non discriminant.
- Empty : rien trouvé dans les paramètres étudiés, PAS une absence globale.
- Partial/truncated : seulement une partie du périmètre couverte.
- Unavailable/blocked/policy_denied : pas de preuve négative.
- Needs_review/transport_unknown : effet externe potentiellement ambigu ; réconcilier avant toute reprise équivalente.

## Coordination et clôture

Déléguer si les recherches sont réellement indépendantes : analyse de méthodes .NET, données de configuration, hypothèses de protocole, signatures. Aucune obligation de créer des sous-agents. Les lectures peuvent se chevaucher ; conserver les garanties effectives de CAS/idempotence sur les mutations. Une synthèse d'enfant ne remplace pas les observations de l'enquête.

Arrêter une branche quand elle n'apporte plus de nouveaux invariants, échantillons, preuves ou pistes de détection. Pour clôturer une session, dire brièvement : **découvert / détectable / encore incertain / prochaine piste utile**, avec références et coûts disponibles. Ne pas exiger la conclusion de tous les axes CTI ni la production d'un avis complet pour continuer.

## Note de travail facultative

Question ou artefact → outil choisi → fait observé et couverture → invariant ou conséquence pour YARA/Suricata/C2/découverte → prochaine action. Ajouter hypothèse/contre-hypothèse, expérience et finding seulement si la nature du test ou le contrat le justifient.

**Oracle :** une inspection directe ne déclenche aucune cérémonie obligatoire ; un test formel conserve son hypothèse préalable ; une observation utile mène à un invariant, une détection candidate ou un pivot, sans transformer empty/partial en réfutation.