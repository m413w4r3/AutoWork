---
name: autowork-bridge-collaboration
description: Collaborer librement avec Bridge pour cadrer, critiquer, tester et rédiger l’analyse CTI, sans quotas ni contrôle de forme entre échanges. Pour operator_led uniquement.
---

# Bridge, partenaire de l’enquête

Bridge propose et critique; l’opérateur vérifie les faits, réalise les tests disponibles et choisit la suite. Une réponse Bridge est un conseil, jamais une preuve. Une réponse partiellement parsée ou non parsée reste utile dès que son texte est lisible.

## Quand le consulter

Déclencher les échanges par leur utilité, sans quota :

- Dès que l’article et l’inventaire initial sont disponibles, demander un cadrage (`plan_investigation`) avant d’approfondir l’analyse.
- Après une découverte CTI importante, demander à Bridge (`plan_investigation`) ce qu’elle change et quelle vérification est utile.
- Pendant la création ou la révision d’une règle YARA (`yara_draft`), pour choisir des pivots VT discriminants (`pivot_strategy`), et pour le protocole C2 (`plan_investigation` ou `suricata_draft`) quand ces sujets sont pertinents.
- Pour rédiger **chaque section analytique** du rapport, demander `draft_text`; demander `review_text` ensuite si la relecture peut aider.

Évite une question identique lorsque sa réponse est déjà disponible. Reviens vers Bridge quand un résultat nouveau permet une critique ou une réévaluation utile; les sujets distincts peuvent être abordés librement.

## Poser la question

`advisor.consult` demande `consultation_type`; ajoute une `question` en langage naturel qui tient en une phrase et donne le contexte utile, la découverte ou le test concerné. Le dossier pertinent est assemblé automatiquement. `evidence_refs` et `sample_sha256` sont facultatifs : ne donne une référence que si elle est exacte et utile; omets un identifiant inconnu. Les avertissements ou omissions éventuels restent visibles dans la réponse. Respecte l’enveloppe et les capacités de l’enquête courante.

## Lire et exploiter la réponse

Lis d’abord le texte brut Bridge (`raw_output` ou `advisory_text`), puis `parse_status`, `extracted_elements`, `input_warnings`, `ignored_inputs` et `omissions`. Un statut `partially_parsed` ou `unparsed` ne rend pas inutile un texte intelligible. Traite ses conseils comme des pistes : vérifie les faits, puis exécute chaque recommandation utile avec les outils autorisés, adapte-la ou explique pourquoi elle ne convient pas. Envoie à Bridge les résultats qui permettent une critique ou une révision; ne répète pas la même question sans nouvel élément.

Si la réponse contient une règle YARA, importe-la depuis l’observation persistée de cette consultation, même si le parsing est partiel :

```text
rule.propose_bridge({"bridge_observation_id":"<UUID de l'observation advisor.consult>"})
```

Ce seul champ est requis. `rule_index` et `rule_name` sont des sélecteurs facultatifs. L’import crée une candidate non validée; appelle ensuite `rule.test` avec le `candidate_id` retourné pour compiler et tester selon le corpus disponible. Pour une correction de syntaxe, utilise `rule.operator_syntax_fix`; pour une amélioration, crée une nouvelle version reliée à son parent courant. Transmets les résultats mesurés à Bridge pour critique ou révision. Ne joins pas de références de mesure ou d’identifiants d’invocation à l’import minimal.

Si la réponse contient une règle Suricata, importe aussi la candidate avec l’observation de la consultation :

```text
rule.import_suricata_bridge_candidate({"bridge_observation_id":"<UUID de l'observation advisor.consult>"})
```

L’import est une candidate non validée et n’exige aucune référence de mesure. S’il existe une capture autorisée et les outils nécessaires, valide-la hors ligne; renvoie les résultats à Bridge pour critique ou révision. Sans capture, garde son statut non testé.

## Rédiger le rapport

Fais proposer par Bridge chaque section analytique avec `draft_text`. Demande `review_text` lorsque la relecture apporte quelque chose. L’opérateur vérifie les affirmations contre les observations, adapte le texte et l’intègre dans une prose naturelle. Les références non résolues n’empêchent pas la rédaction; conserve les faits incertains comme incertains. Le rapport final ne contient pas de marqueurs internes. Si Bridge est indisponible, n’attribue aucun texte à Bridge, marque la section incomplète et reprends la collaboration lorsqu’il redevient disponible.

Le `purpose` peut résumer librement une disposition utile. Cette trace structurée facultative est acceptée par le parseur, mais sa présence ou sa forme ne conditionne ni l’action ni l’échange suivant :

```
[[advisor-trace.v1]]{"recommendation":{"invocation_id":"<id advisor.consult>","decision":"accepted","reason":"<facultatif>"}}
<but de l'appel, une phrase>
```

Si un POST Bridge a un effet ambigu, réconcilie l’invocation existante avant toute reprise; ne renvoie jamais le POST ambigu.

**Oracle :** Bridge est sollicité aux moments utiles, son texte lisible est exploité malgré un parsing partiel, les recommandations utiles mènent à des actions, et les sections analytiques sont vérifiées avant intégration.
