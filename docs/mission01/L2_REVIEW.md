# Mission 01 L2 — revue critique indépendante

**Objet :** commit `ef1bc61a1b7697094b7b60244ca135d378f4c741` (branche
`feat/mission01-l2-context-pagination`), base `6b195122`.
**Revue :** agent Paseo `fd227a85-b1d0-4d61-b5b5-6efc37f2cafa`, GPT-6 Luna
xhigh, lecture seule, worktree `m01-l2-review`.
**Oracle rejoué :** 322 passed. Aucun PostgreSQL réel ni `EXPLAIN`.

## Verdict : FAIL

### HIGH — curseur forgeable

Le curseur est du Base64 JSON sans MAC ni signature
(`context_pagination.py:65`, parseur `90`). Section, investigation et
`context_sha256` sont contrôlés, mais pas l'intégrité. Un pivot, un UUID et
un SHA précédent modifiés puis réencodés sont acceptés. Un client peut sauter
des lignes et présenter une continuation qui ne dérive pas de la page
précédente.

### HIGH — CAS incomplet

`canonical_page_sha256` (`context_pagination.py:164`) ne couvre que section,
investigation, SHA précédent et items. Il ignore le curseur/pivot, `limit`,
`total`, `has_more`, `context_sha256` et le manifeste d'omissions
(`operator_tool_invocation.py:456`). Les comptes `selected/omitted` et les
raisons peuvent être modifiés sans invalider `page_sha256`. Le test de
falsification ne couvre que l'ajout d'un item.

### HIGH — hash avant rédaction

`_source_manifest_entry` (`research_context_builder.py:1599`) réutilise
`page.page_sha256`, calculé sur la page source brute, alors que `selected`
est la projection rédigée (`1308`). Le hash canonique sérialise les items
reçus (`context_pagination.py:234`). Le SHA ne révèle pas directement la
valeur brute mais dépend de champs ensuite rédigés : l'ordre « rédaction
avant hash/CAS » n'est pas respecté. Le test de rédaction vérifie la page
projetée, pas le manifeste du `build()`.

### MEDIUM — omissions trompeuses

Chaque page annonce `total - len(page.items)` omissions en raison `BUDGET`
(`operator_tool_invocation.py:456`), y compris les lignes récupérables par
`next_cursor`. Le compte est répété à chaque page et ne distingue pas une
vraie omission d'un report de pagination.

### MEDIUM — preuve mémoire non probante

Les dépôts mémoire chargent puis trient toute la collection
(`investigation_persistence.py:214`, `271`). Le test instrumenté ne mesure que
la taille de page retournée (`test_research_context_builder.py:3641`). Les
adaptateurs SQL utilisent bien `limit + 1`, mais ce chemin n'est pas exercé
sur PostgreSQL. Un dépôt sans `page_for_investigation` échoue en
`AttributeError`, sans fallback.

### MEDIUM — contrat strict v1 modifié sans version

Nouvelles clés de page dans `...section.v1` et `section_cursors` passe de
`dict[str, int]` à `dict[str, str]` (`investigation_contracts.py:360`,
`operator_context_summary.py:117`). Les champs à défaut permettent au nouveau
parseur de lire d'anciens payloads ; un ancien consommateur strict
`extra=forbid` peut rejeter les nouvelles clés et le nouveau type.

## Vérifications

- Les trois adaptateurs SQL utilisent un ordre total `(sort_key, id)`, un
  prédicat de seek partagé, `limit + 1`, sans `OFFSET`. Colonnes non nulles
  et index composés déclarés. Le prédicat partagé est bien celui appelé par
  les adaptateurs ; paramètres liés par SQLAlchemy.
- Le test de prédicat ne compile qu'un cas ascendant ; aucun test ne
  traverse les trois adaptateurs SQL.
- `list_for_investigation` reste intact ; mode opérateur, CAS d'état et
  compatibilité offset présents.
- TLP / `do_not_submit` / `external_llm_allowed` non affaiblis dans le diff ;
  le test ajouté vérifie le helper de politique, pas une soumission de bout
  en bout.
- `source_ref` contient l'UUID d'investigation et, après continuation, un
  curseur décodable en UUID de ligne et horodatage.

## Résiduels

- PostgreSQL réel, concurrence et performance : **UNKNOWN**. Aucune migration
  requise.
- Coût : **UNKNOWN**.

## Recommandation

Ne pas approuver avant d'authentifier les curseurs, d'inclure le manifeste et
les métadonnées pertinentes dans le CAS après rédaction, et de traiter la
compatibilité du contrat versionné. Réparation `M01-L2-R1` lancée.

## Seconde revue après R1 — PASS_WITH_CHANGES

**Objet :** `4f2a941bfe938dcc117388de9554d4d399f4fb39` (9 fichiers).
**Revue :** agent `d676bd6a-46bd-4795-bb9c-7f61abe5e3a3`, Luna xhigh.
**Oracle rejoué :** 325 passed.

Les 3 HIGH et 3 MEDIUM R1 sont fermés : CAS lié à la portée, au curseur, au
pivot, aux bornes, aux items post-rédaction et au manifeste ; omissions
`PAGINATION` ; `section_cursors` int restauré et `section_keyset_cursors`
additif ; ancien résumé v1 parsé ; curseur explicitement
`unauthenticated_position`, lié à la section/investigation/contexte, ne
permettant pas d'élargir la portée ; fallback sans pagination signalé
`source_bounded=false` sans `AttributeError` ; tests SQL asc/desc et
`limit + 1` via session espionne.

### MEDIUM résiduel — manifeste non lié à `source_ref`/`page_sha256`

`_canonical_omissions` (`context_pagination.py:542`) exclut `source_ref` et
`page_sha256`; `verify_page_sha256` ne vérifie pas leur cohérence avec la
page. Falsifier ces deux champs du manifeste renvoie encore `True`. Ajouter
une vérification de cohérence et des assertions de falsification.

### MEDIUM résiduel — `source_bounded=True` à tort en mémoire

`_memory_keyset_page` (`investigation_persistence.py:212-240`) matérialise
toute la collection via `list_for_investigation` mais conserve
`source_bounded=True` par défaut. Le spy du test masque ces appels et attend
`True` (`test_research_context_builder.py:3668/3813`). Passer explicitement
`source_bounded=False` + raison pour les dépôts mémoire, et couvrir un faux
dépôt réellement borné.

### Résiduels

PostgreSQL réel et performance : **UNKNOWN**. Coût : **UNKNOWN**.
Réparation `M01-L2-R2` lancée.

## Troisième revue après R2 — PASS_WITH_CHANGES

**Objet :** `6143099b8193fd01d56e59185cd2080aa944c897` (delta
`4f2a941..6143099`).
**Revue :** agent `45b350c4-d1fe-41d2-af31-03df58f3e1f1`, Luna xhigh.
**Oracle rejoué :** 326 passed.

Les deux MEDIUM R2 sont fermés : le vérificateur recalcule `source_ref` et
lie `page_sha256`; les dépôts mémoire déclarent `source_bounded=False` avec
raison, le test compte leurs appels à `list_for_investigation` et un faux
dépôt réellement borné propage `True`; l'ancien payload page v1 est parsé.
Cinq fichiers, aucun interdit, CAS et adaptateurs SQL intacts, protections
inchangées.

### MEDIUM résiduel — vérification absente aux constructeurs

`make_keyset_page` (`context_pagination.py:578`) et
`_source_manifest_entry` (`research_context_builder.py:1732`) construisent
page et manifeste sans appeler le vérificateur. Le chemin opérateur
(`operator_tool_invocation.py:242`) passe `source_ref` comme valeur attendue
: il compare deux copies au lieu de recalculer la référence. Une divergence
future entre champs construits pourrait être retournée sans refus. Les tests
de falsification exercent le vérificateur isolément, pas son application aux
constructeurs.

### Résiduels

PostgreSQL réel et performance : **UNKNOWN**. Coût : **UNKNOWN**.
Réparation `M01-L2-R3` lancée (dernière tentative autorisée).

## Quatrième revue après R3 — PASS_WITH_CHANGES (clôture)

**Objet :** `0c54ed9857ad6b4d82d42912ffcb256efc924f02` (delta
`6143099..0c54ed9`).
**Revue :** agent `24b810f2-e2d0-4486-b22f-0c05887bdb1d`, Luna xhigh.
**Oracle rejoué :** 342 passed.

Le vérificateur recalcule page SHA et `source_ref` dans les quatre chemins
(`make_keyset_page`, builder, opérateur offset, opérateur keyset) et lève
des `ValueError` non avalées. Les tests couvrent manifeste cohérent accepté
et falsifications de référence, SHA, comptes et raison. Le CAS, le chaînage
des curseurs, `source_bounded=False` mémoire, les contrôles de divulgation et
la compatibilité offset restent intacts.

### MEDIUM résiduel documenté — catégorie d'omission reprise comme attendue

`_assert_keyset_page_consistency` (`operator_tool_invocation.py:273`) passe
`entry.omission_reasons` au vérificateur. Un constructeur qui émettrait une
catégorie erronée mais cohérente avec les comptes, le SHA et `source_ref`
serait accepté. Les chemins keyset produisent `PAGINATION` et
`NOT_SELECTED` ; l'attendu devrait être dérivé du chemin, pas du manifeste.
Le test ajouté injecte une raison vide mais pas une substitution de catégorie
à comptes et SHA recalculés.

**Décision :** la limite de trois réparations est atteinte ; ce MEDIUM est
accepté comme résidu documenté, sans quatrième patch. Il ne permet pas
d'obtenir une page ou une référence falsifiée, seulement un libellé
d'omission erroné.

### Résiduels

PostgreSQL réel et performance : **UNKNOWN**. Coût : **UNKNOWN**.
Intégration : `feat/mission01-l2-context-pagination` fusionnée dans
`chore/mission01-integration` au commit `36eb5326`. Oracle combiné B1B+L2 sur
l'intégration : **556 passed**.



