---
name: autowork-yara-bridge-workflow
description: Proposer tôt des candidates YARA depuis les invariants observés, les compiler et les tester selon le corpus disponible, puis les améliorer avec Bridge et les nouveaux échantillons.
---

# YARA + Bridge

Une candidate précoce peut aider à trouver des variantes. Bridge conseille, le workbench importe et teste, et les observations déterminent le statut. Un match initial ne valide pas une famille; l’absence de goodware ou de holdout ne bloque pas la première candidate.

## Choisir des motifs

Partir des octets, chaînes, ressources, structures ou fonctions effectivement observés. Préciser si le but est un spécimen, un sous-groupe ou une famille présumée. Un motif mesuré sur un seul échantillon peut soutenir une règle de spécimen explicitement limitée. Ajouter variantes, contre-exemples et goodware au fil de l’enquête.

## Faire proposer et tester la règle

Avant d’écrire toute règle YARA à la main, consulte Bridge avec `yara_draft` selon `autowork-bridge-collaboration`. L’opérateur vérifie, affine et teste les propositions Bridge; il ne rédige pas la candidate initiale.

Pour importer une réponse Bridge et lui transmettre les résultats, suis `autowork-bridge-collaboration`. Teste chaque candidate avec `rule.test` et le `candidate_id` retourné; utilise `rule.operator_syntax_fix` pour corriger la syntaxe ou crée une nouvelle version liée au parent courant pour améliorer la détection.

## Qualifier et poursuivre

- Distingue candidate, compilation réussie, tests réalisés et validation selon le cycle réel de l’outil.
- Un résultat absent ou non mesurable ne vaut pas non-match. Un petit corpus limite la conclusion, sans interdire la candidate.
- Une règle peut alimenter un pivot VT autorisé; les nouveaux échantillons peuvent apporter des positifs, des contre-exemples ou des motifs discriminants.
- Pour chaque bloc final de l’avis, applique la rédaction et l’attribution Bridge de `autowork-bridge-collaboration`.
- L’approbation ou la publication reste une décision humaine séparée.

**Oracle :** tout texte YARA proposé par Bridge et persisté est importé comme candidate par son observation, testé selon les capacités disponibles et réévalué à partir des résultats mesurés.
