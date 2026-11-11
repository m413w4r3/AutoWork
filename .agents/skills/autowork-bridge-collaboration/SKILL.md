---
name: autowork-bridge-collaboration
description: Consulter Bridge aux étapes de chaque mission operator_led : cadrage, pivots VT, interprétation, règles YARA/Suricata et rédaction de chaque bloc de l’avis.
---

# Bridge, partenaire de l’enquête

Bridge conseille; l’opérateur vérifie les faits, réalise les tests disponibles et choisit la suite. Une réponse n’est ni une preuve ni un finding automatique. Une réponse partiellement parsée ou non parsée reste utile dès que son texte est lisible.

## Quand le consulter

Utilise cette séquence pour chaque mission :

| Moment | Consultation | Action |
|---|---|---|
| Début de mission | `plan_investigation` | Dès que l’article et l’inventaire initial sont disponibles, demande le cadrage à partir de l’état canonique courant avant d’approfondir l’analyse. |
| Résultat important ou ambigu | `plan_investigation` | Après une découverte CTI importante ou un résultat ambigu, demande ce qui change et quelle vérification suit. |
| Chaque lot VT / nouveaux échantillons | `pivot_strategy` | Avant chaque lot de pivots VT ou de recherche d’échantillons, demande les pivots VT discriminants pour la question de ce lot. |
| Rétroconception | `plan_investigation` | Demande une interprétation critique de la configuration ou des variantes à partir des octets réellement observés. |
| Règles YARA | `yara_draft` | Consulte avant d’écrire toute règle à la main; l’opérateur vérifie, affine et teste les candidates proposées. |
| Protocole / réseau | `suricata_draft` | Consulte dès que la compréhension du protocole et les faits réseau observés justifient une candidate. |
| Avis final | `draft_text` | Fais rédiger chaque section analytique et chaque bloc final de l’avis par Bridge; demande `review_text` si utile, rattache chaque bloc à `bridge_draft_ref` et n’écris aucun texte final comme opérateur. |

Le budget de 500 tours ne limite pas le recours attendu à Bridge. Sérialise les appels `advisor.consult` : un à la fois; attends leur résultat (5–25 min) avant l’appel suivant et ne rends pas la main pendant l’attente. Ne pose pas une question identique sans élément nouveau.

Dans le handoff, ajoute `CONSULTATIONS_BRIDGE` et liste chaque consultation avec son type, son objet et l’apport retenu ou écarté. Une mission d’analyse substantielle avec 0–2 consultations est incomplète.

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

L’opérateur vérifie les affirmations de chaque brouillon contre les observations canoniques; conserve les faits incertains comme incertains et attribue chaque bloc à son `bridge_draft_ref`. Si Bridge est indisponible ou refusé par l’enveloppe, marque la section incomplète et reprends la collaboration lorsqu’il redevient disponible; ne remplace pas le brouillon par un texte final écrit par l’opérateur.

Le `purpose` peut résumer librement une disposition utile. Cette trace structurée facultative est acceptée par le parseur, mais sa présence ou sa forme ne conditionne ni l’action ni l’échange suivant :

```
[[advisor-trace.v1]]{"recommendation":{"invocation_id":"<id advisor.consult>","decision":"accepted","reason":"<facultatif>"}}
<but de l'appel, une phrase>
```

Si un POST Bridge a un effet ambigu, réconcilie l’invocation existante avant toute reprise; ne renvoie jamais le POST ambigu.

**Oracle :** Bridge est sollicité aux moments utiles, son texte lisible est exploité malgré un parsing partiel, les recommandations utiles mènent à des actions, et les sections analytiques sont vérifiées avant intégration.
