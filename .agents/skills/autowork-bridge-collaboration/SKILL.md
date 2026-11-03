---
name: autowork-bridge-collaboration
description: Travailler avec Bridge comme analyste-conseil tout au long de l'enquête (plan, critique, YARA, pivots, rédaction), agir sur ses conseils et consigner comment ils ont été utilisés. Pour operator_led uniquement.
---

# Bridge, partenaire de l'enquête

Bridge cadre → l'opérateur analyse → Bridge critique → l'opérateur teste → Bridge réévalue les résultats avec l'opérateur. Une réponse de Bridge est une **hypothèse de travail crédible**, jamais une preuve ni un statut de finding : la mesure par les outils reste de l'opérateur. Une recommandation utile mène normalement à une décision ou un test ; une réponse partielle reste exploitable selon ce qui a été reçu, extrait, testé et vérifié.

Autorité : G2/TLP, `external_llm_allowed`, capacités et budgets de CETTE enquête. Si Bridge est indisponible ou refusé, poursuivre les branches locales autorisées et écrire dans le résumé final « collaboration Bridge non démontrée : <raison> ».

## Quand consulter (déclencheurs, pas quotas)

Lire `advisor_guidance` dans `GET /operator-status` ou `/operator/context?view=summary` au démarrage et après chaque lot de résultats : `checkpoint_due`, `reason`, `suggested_consultation_type`, `last_advice`. Une consultation due est l'option par défaut ; avancer sans elle est légitime si une branche locale est plus informative, et le serveur le consigne comme `skipped`.

| Moment | `consultation_type` | Question utile |
| --- | --- | --- |
| Article et inventaire initial disponibles, **avant d'analyser en profondeur** | `plan_investigation` | Plan concret : affirmations à vérifier, échantillons prioritaires, outils de `available_tools`, contradictions possibles, premières pistes YARA/C2/pivots VT, inconnues à mesurer |
| Premières analyses significatives | `plan_investigation` | Quels faits semblent établis, lesquels restent discutables, quelles analyses ont une valeur réelle ? |
| Avant une rétro-ingénierie C2 pertinente (protocole, config, crypto) | `plan_investigation` | Méthodes, données, messages ou fonctions les plus instructifs ; tests passifs qui départagent |
| Construction ou révision d'une YARA | `yara_draft` | Candidate ou motifs observés, puis critique de spécificité et réévaluation des résultats par hash |
| Avant une campagne de pivots VT | `pivot_strategy` | Planifier des pivots exécutables et discriminants parmi les outils VT réellement exposés |
| Résultat contradictoire, recherche vide, stagnation | `pivot_strategy` ou `plan_investigation` | Réévaluation : que signifie ce résultat, quelle alternative discriminante ? |
| Rédaction de chaque section du livrable | `draft_text`, puis `review_text` pour cette section | Prose fondée sur les faits transmis ; `suricata_draft` et `figure_plan` complètent les sections réseau et schéma |

Pas de quota de consultations, pas de question de suivi interdite, pas de regroupement forcé de sujets sans rapport. Ne pas répéter une demande identique si aucun fait nouveau ne change la question ; revenir vers Bridge lorsque de nouveaux résultats permettent une réévaluation. Une seule consultation de cadrage ne remplace pas les critiques et échanges de rédaction utiles pour les sections suivantes. Ne jamais renvoyer un POST ambigu (`advisor.reparse` ou réconciliation).

## Le dossier est assemblé côté AutoWork

Poser une question précise en prose avec les références les plus pertinentes (`evidence_refs`, `sample_sha256`) ; ne pas recopier les données. Vérifier que le statut réel des octets de chaque échantillon (acquis, rapporté, associé, mesurable) est celui transmis. Ce qui manque se redemande dans l'échange suivant ; ne jamais le présenter comme vérifié.

## Après chaque réponse importante

1. Lire la prose et les avertissements ; distinguer reçu, extrait, techniquement exploitable, testé, vérifié et encore manquant. Les éléments non reconnus restent accessibles dans le texte brut conservé.
2. **Agir** : transformer normalement une recommandation utile en décision ou test autorisé, puis mesurer le résultat. Copier les recommandations dans le rapport ne compte pas comme les avoir utilisées.
3. Revenir vers Bridge pour réévaluer un résultat qui change matériellement la couverture, la règle, le pivot ou le modèle C2 ; éviter les appels identiques sans faits nouveaux.
4. Consigner la disposition dans le `purpose` de l'appel d'outil qui suit, préfixé par une ligne de trace puis le but humain :

```
[[advisor-trace.v1]]{"recommendation":{"invocation_id":"<id advisor.consult>","decision":"accepted","reason":"<facultatif>"}}
<but de l'appel, une phrase>
```

`decision` ∈ `accepted`, `adapted`, `refused`, `deferred` ; `reason` (≤ 256 caractères) est requise sauf pour `accepted`. Ajouter `index` seulement pour viser une entrée structurée de `recommended_next_tools` (alors `accepted`/`refused` uniquement, et l'appel doit correspondre à l'outil proposé). Le checkpoint (`consulted`, `skipped`, `denied`) est ajouté par le serveur : ne pas l'écrire.

Le `purpose` de chaque appel dit l'objectif, le motif synthétique du choix et ce qu'on espère apprendre : une justification opérationnelle courte, pas un raisonnement privé.

Si Bridge est indisponible, ne lui attribuer aucun texte : poursuivre les analyses locales autorisées, marquer la section concernée incomplète, diagnostiquer la cause et reprendre l'échange lorsque Bridge redevient disponible.

## Rédaction

Pour chaque section substantielle, demander à Bridge un `draft_text` puis un `review_text` lorsque la relecture apporte une vérification utile. L'opérateur apporte les faits mesurés et vérifie la cohérence avec ses tests ; une édition opérateur reste identifiable. Ne pas rédiger seul tout l'avis après un unique cadrage. Le texte livré ne contient aucun marqueur interne (`SUPPORTED`, `PARTIAL`, `bridge_authored`, `observation_ref`, `confidence=`) : provenance et preuves restent dans la couche d'audit. Les IOC viennent des observations canoniques, la source YARA exacte va en annexe séparée, et on n'invente ni message C2 ni découverte.

**Oracle :** une consultation précoce produit des actions mesurables ; chaque conseil a une disposition retrouvable ; une réponse en prose est exploitée sans exiger de format ; l'absence de Bridge est signalée, jamais maquillée.
