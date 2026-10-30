
## M07c — crosswalk documented_samples ↔ passages

Référence : enquête `782ff20c-8bfb-4ac0-a1a8-356e8b7bbe03`, working_state_version 2.

Les lectures GET des sections nécessaires n'ont restitué aucun contenu (403 `operator_client_scope_denied`). Les rangs ci-dessous reprennent uniquement le nombre de candidats fourni pour la mission; faute d'accès aux sections, passages/claims et nature du lien restent non évaluables. Cela ne signifie pas qu'aucun lien n'existe.

| Rang inventaire | IDs passages / claims pertinents | Lien |
|---:|---|---|
| 1 | non accessibles | non évaluable |
| 2 | non accessibles | non évaluable |
| 3 | non accessibles | non évaluable |
| 4 | non accessibles | non évaluable |
| 5 | non accessibles | non évaluable |
| 6 | non accessibles | non évaluable |
| 7 | non accessibles | non évaluable |
| 8 | non accessibles | non évaluable |
| 9 | non accessibles | non évaluable |
| 10 | non accessibles | non évaluable |
| 11 | non accessibles | non évaluable |
| 12 | non accessibles | non évaluable |
| 13 | non accessibles | non évaluable |
| 14 | non accessibles | non évaluable |
| 15 | non accessibles | non évaluable |

Limite : aucune conclusion d'alignement source n'est possible sans une route GET autorisée donnant accès aux sections `documented_samples`, `article_long_texts`, `known_chain` et `article_claims`.

### Précision append-only

Les numéros 1–15 du tableau précédent sont des emplacements provisoires basés sur le décompte fourni, et non des rangs de lignes vérifiés dans `documented_samples`; ne pas les interpréter comme tels. Les chemins testés (`/working-state`, `/article_claims`, `/evidence`, `/openapi.json`) ont renvoyé 403 `operator_client_scope_denied`; leurs refus ne démontrent pas que les routes exactes des quatre sections ont été testées. Le seul constat vérifiable ici est qu'aucun rang, ID de passage/claim ou lien candidat-section n'a pu être établi.

### Résultat des GET ciblés

Les GET portant exactement les noms des sections (`/documented_samples`, `/article_long_texts`, `/known_chain`, `/article_claims`) ont chacun renvoyé 403 `operator_client_scope_denied`. L'inventaire et les passages/claims restent donc inaccessibles dans ce scope; aucun rang ou lien explicite/contextuel n'est vérifié.
