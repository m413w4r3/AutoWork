# Mission 01 Q — plan d'évaluation anti-slop

**Scout :** agent `a4991603`, GPT-6 Luna xhigh, lecture seule, base
`3fd966480c2963d1b3e19ec57b335d310493c049`.
**Décision :** GO conditionnel pour le corpus, les oracles structurels et un
patch de prompt étroit ; **NO-GO** pour conclure à une réduction sémantique
générale du slop sur la seule base offline.

## Faits vérifiés

- Le prompt Bridge demande déjà des preuves pour les conclusions, distingue
  fait/inférence/hypothèse, interdit d'inventer des IDs et traite le contexte
  comme non fiable (`bridge_investigator.py:1615`). L'advisor demande une
  réponse vérifiable et concise avec réserves matérielles
  (`investigation_advisor_tools.py:2794`).
- Le préflight exige des références canoniques pour `draft_text`; le parseur
  rattache chaque phrase à des assertions (`advisor_dossier.py:1470`,
  `advisor_contracts.py:1310`). Ces contrôles ne prouvent pas l'implication
  sémantique.
- `ScriptedTurnTransport` (`bridge_investigator.py:976`) permet des réponses
  fixes et l'enregistrement des prompts. Aucun jeu de cas anti-slop versionné
  ni harness dédié n'existe.
- Les oracles Bridge sont synthétiques ; le comportement d'un Bridge réel
  reste `UNKNOWN` (`BRIDGE_CHAIN_EVAL.md:60`).

## Définition opérationnelle

Est « slop » une sortie qui ajoute des affirmations non rattachées à une
source autorisée, masque une incertitude ou une contradiction connue, ou
répète/remplit sans apporter de fait ni de signal vérifiable.

## Métriques (par dimension, dénominateur explicite, pas de score global)

| Dimension | Formule | Seuil |
|---|---|---|
| Références invalides | réponses utilisables malgré une référence inconnue / cas à référence invalide | 0 |
| Ancrage structurel | phrases factuelles liées à une assertion prévalidée avec SHA canonique / phrases factuelles | 1,0 |
| Absence de preuve | cas sans preuve produisant une conclusion utilisable ou un prompt / cas sans preuve | 0 |
| Contradiction + UNKNOWN | cas citant les deux sources et marquant UNKNOWN/limite / cas ciblés | 1,0 (proxy lexical) |
| Duplication | phrases factuelles dupliquées après normalisation exacte / phrases factuelles | 0 sur positifs |
| Remplissage | correspondances à une liste versionnée de formulations génériques / clauses | 0 sur positifs |
| Overclaim | négatifs synthétiques annotés restant utilisables / négatifs de la classe | 0 |

L'ancrage mesure un rattachement structurel, pas la vérité du claim. Les
listes lexicales sont un proxy, pas un juge sémantique.

## Lots

**Q1 — corpus et oracle (4 fichiers).** `tests/fixtures/advisor_eval/
anti_slop.v1.jsonl` (16–32 cas, 8 types × négatif/positif), `manifest.v1.json`
(SHA-256 des octets exacts), `tests/test_advisor_anti_slop_eval.py`,
`docs/mission01/Q_ANTI_SLOP_EVAL.md`. Aucun code de production. Textes, IDs et
SHA synthétiques ; aucun secret, URL signée, byte brut ni fixture `real_*`.

**Q2 — patch borné et régressions (7 fichiers max).** Production :
`bridge_investigator.py`, `investigation_advisor_tools.py`,
`advisor_contracts.py`. Tests : `test_bridge_investigator.py`,
`test_investigation_advisor_tools.py`, `test_advisor_wire.py`,
`test_advisor_completeness.py`. Consignes courtes : chaque phrase factuelle
reste dans les assertions/sources transmises ; signaler UNKNOWN et la
contradiction matérielle ; Recommander une action seulement avec un signal
vérifiable ; éviter répétitions et conseils génériques. Réutiliser le
préflight/les parseurs. Si le prompt Bridge change, incrémenter
`INVESTIGATION_PROMPT_VERSION` (elle participe à la clé d'idempotence).

## Oracle et limites

```sh
make test-backend PYTEST_ARGS="tests/test_advisor_anti_slop_eval.py tests/test_advisor_wire.py tests/test_bridge_investigator.py tests/test_investigation_advisor_tools.py tests/test_advisor_completeness.py -q --tb=short"
```

- Baseline et patch utilisent le même corpus, les mêmes réponses fixes, le
  SHA des prompts et les résultats par classe.
- `ScriptedTurnTransport` ne produit pas de réponse selon le prompt : le
  protocole compare les prompts et les refus/parseurs, il ne prouve pas que
  les consignes réduisent le slop d'un modèle. Cette qualité sémantique reste
  `UNKNOWN`.
- Complexité : ≤3 modules de production, ≤8 fichiers par lot, 0 dépendance,
  aucune nouvelle persistance. Delta de dépendances et `git diff --stat`
  rapportés.
- Risques : faux positifs lexicaux, faux négatifs sémantiques, sur-ajustement
  aux réponses synthétiques. Coût fournisseur offline : 0 ; qualité réelle :
  `UNKNOWN`.
