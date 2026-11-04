---
name: autowork-yara-bridge-workflow
description: Proposer tôt des candidates YARA depuis les invariants observés, les compiler et les tester selon le corpus disponible, puis les améliorer avec Bridge et les nouveaux échantillons.
---

# YARA + Bridge

Une candidate précoce peut aider à trouver des variantes. Bridge conseille, le workbench importe et teste, et les observations déterminent le statut. Un match initial ne valide pas une famille; l’absence de goodware ou de holdout ne bloque pas la première candidate.

## Choisir des motifs

Partir des octets, chaînes, ressources, structures ou fonctions effectivement observés. Préciser si le but est un spécimen, un sous-groupe ou une famille présumée. Un motif mesuré sur un seul échantillon peut soutenir une règle de spécimen explicitement limitée. Ajouter variantes, contre-exemples et goodware au fil de l’enquête.

## Demander l’avis de Bridge

Quand une première candidate ou une révision mérite une critique, appeler `advisor.consult` avec `consultation_type="yara_draft"` et une question naturelle qui résume en une phrase les motifs, le test ou la découverte à examiner. Le dossier est assemblé automatiquement; les références de preuve sont facultatives. Omet tout identifiant que tu ne peux pas confirmer et ne bloque pas l’échange pour une sélection de mesures ou un format de réponse. Si la réponse est lisible, exploite-la même lorsque `parse_status` indique un résultat partiel ou non parsé.

## Importer, compiler, tester

Dès qu’une réponse Bridge contient du code YARA, importe sa source depuis l’observation persistée de cette consultation, quel que soit le statut de parsing :

```text
rule.propose_bridge({"bridge_observation_id":"<UUID de l'observation advisor.consult>"})
```

`bridge_observation_id` est l’unique champ requis. `rule_index` et `rule_name` sont facultatifs; une réponse contenant plusieurs règles peut créer plusieurs candidates. L’import conserve la source de Bridge comme candidate non validée et n’exige ni références de mesure ni hashes supplémentaires. Pour chaque candidate, appelle `rule.test` avec le `candidate_id` retourné afin de compiler et éprouver le corpus disponible. Pour une correction syntaxique, utilise `rule.operator_syntax_fix`; pour une amélioration, crée une nouvelle version liée au parent courant. Corrige ou révise selon les faux positifs/négatifs mesurés; une candidate non testée garde ce statut.

Envoie à Bridge les résultats qui aident la critique ou la révision : matches, non-matches mesurables, erreurs, cas non mesurables et contre-exemples. Pose une question qui résume ces résultats et demande ce qu’ils changent; ne répète pas une question déjà répondue sans résultat nouveau. Agis sur chaque recommandation utile.

## Qualifier et poursuivre

- Distingue candidate, compilation réussie, tests réalisés et validation selon le cycle réel de l’outil.
- Un résultat absent ou non mesurable ne vaut pas non-match. Un petit corpus limite la conclusion, sans interdire la candidate.
- Une règle peut alimenter un pivot VT autorisé; les nouveaux échantillons peuvent apporter des positifs, des contre-exemples ou des motifs discriminants.
- Pour chaque section analytique du rapport, demande aussi un `draft_text`; demande `review_text` si une seconde lecture apporte une critique utile. Vérifie les faits et intègre une prose naturelle sans marqueurs internes.
- L’approbation ou la publication reste une décision humaine séparée.

**Oracle :** tout texte YARA proposé par Bridge et persisté est importé comme candidate par son observation, testé selon les capacités disponibles et réévalué à partir des résultats mesurés.
