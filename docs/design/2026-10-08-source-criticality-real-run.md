# Audit du run réel — criticité des pertes de sources

Date de vérification : 2026-10-08. Politique exécutée : `source-criticality-v1`, avec la fonction du worktree `production_source_criticality`.

## Méthode et périmètre

- Lecture seule de `cti-bulletin-postgres-1` : toutes les requêtes ont été exécutées dans `BEGIN TRANSACTION READ ONLY` puis `ROLLBACK`. Aucun pipeline, bridge ou écriture DB n'a été lancé.
- Lecture seule des objets MinIO pointés par les artifacts REFERENCES et EXTRACTION des runs 1 et 2. Les SHA-256 des blobs lus correspondent à `blobs.sha256` dans PostgreSQL.
- Les six labels de sujets du rapport correspondent aux titres archivés : S3 CHOSEN BRICK/HEAVYGRAM, S4 GTG-30004, S5 AA26-097A/PLC, S6 GTG-30005, S7 GTG-30006, S9 GTG-34007.
- La criticité a été évaluée avec `production_source_criticality(source, sources=coverage)`. `coverage` est la liste des URL présentes dans l'artifact EXTRACTION vérifié du même run, comme dans le code de production. Les décisions `continue_without_source` ont été reliées à leur URL par recalcul du `repair_key_for_supplemental_source` : les 11 clés DB correspondent aux 11 URL ci-dessous.
- Les décisions observées sont toutes sur le run 1. Le run 2 a des IDs d'artifacts différents, même lorsque le blob REFERENCES est réutilisé ; les décisions du run 1 ne s'y appliquent pas.

## Matrice

| Sujet | Source et échec observé | Résultat de `production_source_criticality` | Run 1 : effet de la décision enregistrée | Run 2 : effet avec la règle livrée et justification |
|---|---|---|---|---|
| S3 — CHOSEN BRICK/HEAVYGRAM | Page FBI Telegram C2, HTTP 403 | **CRITICAL — `core_tier`** | Décision reasonnée, artifact et génération `3/3` concordants : continue autorisé avec diagnostic durable. Sans cette décision, bloquant. | Échec toujours présent, aucune décision liée au run 2 : **bloquant/review-required**. La page est CORE. |
| S4 — GTG-30004 | Brief Krypt3ia, HTTP 403 | **CRITICAL — `counter_analysis`** | Décision observée génération 0, run génération 2 : périmée, donc **bloquant/review-required** malgré le motif enregistré. | Toujours non archivé au moment de l'artifact : **bloquant/review-required**. Une contre-analyse ne devient pas facultative parce que les sources core sont disponibles. |
| S5 — AA26-097A/PLC | Avis CISA AA26-097A, HTTP 403 | **CRITICAL — `primary_role`** | Décision reasonnée, génération `0/0` concordante : continue avec diagnostic durable ; sans décision, bloquant. | Pas de décision propre au run 2 : **bloquant/review-required**. Le rôle PRIMARY est déterminant même si le tier est SUPPORTING. |
| S5 — AA26-097A/PLC | STIX JSON CISA, HTTP 403 | **CRITICAL — `primary_role`** | Même décision reasonnée et fence `0/0` : continue avec diagnostic durable. | Pas de décision run 2 : **bloquant/review-required**. Le rôle enregistré est PRIMARY malgré le tier TECHNICAL. |
| S5 — AA26-097A/PLC | STIX XML CISA, HTTP 403 | **CRITICAL — `primary_role`** | Même décision reasonnée et fence `0/0` : continue avec diagnostic durable. | Pas de décision run 2 : **bloquant/review-required**. Le rôle enregistré est PRIMARY malgré le tier TECHNICAL. |
| S5 — AA26-097A/PLC | Avis FBI PLC qui donne son nom au sujet, HTTP 403 | **CRITICAL — `core_tier`** | Décision reasonnée, fence `0/0` concordant : continue avec diagnostic durable ; sans décision, bloquant. | Pas de décision run 2 : **bloquant/review-required**. Source CORE et title-defining. |
| S5 — AA26-097A/PLC | Avis FBI/EPA sur les PLC eau et eaux usées, HTTP 403 | **CRITICAL — `counter_analysis`** | Décision reasonnée, fence `0/0` concordant : continue avec diagnostic durable. | Pas de décision run 2 : **bloquant/review-required**. La source est affectée au rôle COUNTER_ANALYSIS. |
| S5 — AA26-097A/PLC | Alerte NCSC « Disruptive cyber activity… », extraction `extraction_source_output_invalid`; l'artifact porte aussi `q2_parse_error:q2_no_payload` | **CRITICAL — `counter_analysis`** | Aucune décision `source_extraction_failed` en DB : **bloquant/review-required**. L'omission identifie l'URL et le code d'échec ; le diagnostic nouveau ajoute la criticité. | Même omission et avertissement présents dans l'extraction run 2, sans décision : **bloquant/review-required**. La contre-analyse ne peut pas disparaître silencieusement. |
| S6 — GTG-30005 | Article Reuters sur l'usage de Claude, HTTP 401 | **SECONDARY — `redundant_corroboration`** | Décision reasonnée, fence `0/0` concordant : continue avec diagnostic durable. La couverture primaire Anthropic est extraite. | La décision run 1 ne se reporte pas, mais la règle autorise **continue avec diagnostic durable** : une source primaire CORE est extraite et l'article est une corroboration redondante. |
| S7 — GTG-30006 | IranWire « How the Islamic Republic and the MEK Misused Claude AI », HTTP 403 | **CRITICAL — `coverage_unknown`** | La clé vise cette URL ; fence `0/0` concordant et motif non vide : le code autorise continue avec diagnostic. **Anomalie DB : le texte du motif parle du CSV Anthropic d'un autre cas, pas de cet article IranWire.** | Source toujours omise, aucune décision du run 2 : **bloquant/review-required**. Le rôle éditorial CONTEXT ne prouve pas une corroboration redondante. |
| S9 — GTG-34007 | IranWire « How the Islamic Republic and the MEK Misused Claude AI », HTTP 403 | **SECONDARY — `redundant_corroboration`** | Décision observée génération 0, run génération 1 : périmée. La perte reste néanmoins **continuable avec diagnostic durable**, car une source primaire est extraite et la corroboration est redondante. | Décision run 1 non reportée ; l'échec reste **continuable avec diagnostic durable** pour la même raison de couverture. |
| S9 — GTG-34007 | IranWire « Alaj: The Rise of Crowdsourced Surveillance Platforms in Iran », HTTP 403 | **CRITICAL — `coverage_unknown`** | Décision observée génération 0, run génération 1 : périmée ; **bloquant/review-required** jusqu'à une décision fraîche. Le rôle CONTEXT ne fournit pas de garantie de couverture. | Aucune décision propre au run 2 : **bloquant/review-required**. |

## Lecture des résultats

- Le tier seul ne rend pas un échec secondaire. CORE, PRIMARY et COUNTER_ANALYSIS sont CRITICAL. Une CORROBORATION n'est SECONDARY que si une source primaire extractible subsiste ; un hash de contenu identique à une autre source extractible est également une preuve de redondance. Les rôles CONTEXT et les couvertures inconnues restent CRITICAL. Aucune classification par modèle n'a été ajoutée.
- Les 11 décisions de collecte du run 1 sont des waivers reasonnés par `dev-analyst`. Les décisions qui respectent le fence artifact/génération restent lisibles et applicables à ce run ; les décisions périmées ne le sont pas. Aucun waiver du run 1 ne traverse vers le run 2.
- La décision S7 est bien liée par son `repair_key` à l'URL IranWire, mais son texte mentionne le CSV Anthropic. Le code livré ne peut contrôler le sens d'un motif libre ; il voit une action analyste explicite avec motif non vide. Il faut corriger cette justification par une nouvelle décision analyste si l'on veut une justification fidèle, sans réécrire l'historique.
- L'artifact EXTRACTION de S9 contient aussi un avertissement `q2_parse_error:q2_no_payload` sans URL ni entrée `omitted_sources` correspondante. Il ne permet donc pas d'identifier une source et de calculer sa criticité ; je ne l'attribue pas aux deux IranWire. Cet avertissement reste une limite de traçabilité à investiguer.
- Les artifacts et décisions historiques n'ont pas été modifiés. L'évaluation est une projection du code livré sur les observations/artifacts archivés.

## Révision ciblée après waiver du run courant

Après cet audit, le sujet S5 (`bb2701a1-777c-405a-b2bd-0f097e648fd3`) a reçu
via l'API deux décisions analyste `continue_without_source` avec raison pour
l'échec NCSC `extraction_source_output_invalid`. Les deux décisions sont
effectives pour l'artifact et la génération courants; l'API Repairs les expose
`resolved=true`, `blocking=false` et `application_state=already_effective`.

Une révision ciblée de SYNTHESIS restait pourtant refusée. La garde consultait
uniquement les omissions d'extraction et leur criticité, sans lire les
décisions effectives. Le refus est corrigé: chaque omission critique est
comparée à sa clé de réparation et ne bloque plus si la décision effective est
un `continue_without_source` avec raison analyste et fences d'artifact et de
génération courantes. Sans waiver, avec une raison absente ou avec un waiver
périmé, le refus d'extraction reste en place.

Les révisions de SYNTHESIS et d'EDITORIAL_ENRICHMENT tracent les waivers
utilisés dans `metadata.revision.critical_source_waivers` et dans la lineage.
Le contrat et le validateur de révision ciblée sont versionnés pour ce
changement. Les prévisualisations d'extension de sources appliquent également
la garde; la promotion est revalidée contre l'artifact EXTRACTION courant.

Les autres chemins vérifiés ont une portée distincte: l'ajout de sources
utilise déjà les décisions effectives reasonnées pour lever ses blockers de
sources critiques non archivées; une extraction échouée sur la source ajoutée
reste refusée par son propre résultat d'extraction. La restauration
d'enrichissements périmés réutilise des blocs historiques vérifiés et les
revalide contre les preuves courantes; elle ne passe pas par la garde de
génération d'une révision ciblée de SYNTHESIS.

## Résolution des issues entre runs

Le même audit a révélé que la clé `31b64959…` du sujet 3 avait deux
projections `supplemental_source_unarchived` : l'issue ouverte observée sur le
run courant `c515deef` (artifact `293a9b9b`, génération 2) et l'issue résolue
du run antérieur `7c42069c` (artifact `e09741e5`, génération 3). Le lecteur
des issues parcourait tous les runs de l'édition et projetait leur artifact
courant. La clé supplemental étant stable entre les runs, son getter par clé
et `get_issue` pouvaient retourner la première observation historique avant
la courante ; `_prepare` comparait alors le mauvais artifact et la mauvaise
génération aux fences du client, puis refusait la décision courante avec
`production_repair_stale`. Les clés extraction-failure incluent l'artifact
EXTRACTION observé et distinguent donc déjà les runs, mais leurs anciennes
issues restaient exposées par les listes comme des actions disponibles.

Le read model de revue expose le run du dernier batch de l'édition, mais
`EditionReviewService` agrège les réparations par sujet. Il dépend donc du
lecteur d'issues pour ne lui transmettre que les réparations de ce run.
L'endpoint sujet sélectionne déjà son run via
`get_current_for_subject`; avant ce correctif, le lecteur appelé ensuite
réintroduisait les projections plus anciennes de la même édition.

Les trois lecteurs (`_records`, sources supplemental et échecs d'extraction)
ne projettent maintenant que le run au plus grand `run_number` par sujet dans
l'édition. Les getters par clé résolvent ainsi cette observation unique, et
les listes de l'API sujet et de la revue d'édition n'exposent plus les issues
de runs antérieurs comme actions disponibles. Pour les décisions de source, le
run, l'artifact et la génération définissent l'observation à laquelle la
réponse s'applique. `expected_effective_decision_id` reste la garde optimiste
de cette observation : une décision d'un artifact précédent ne masque pas une
première décision de source du run courant, et la révision d'une décision de
l'observation courante exige toujours l'identifiant attendu. Un
`observed_run_id` historique est refusé comme périmé. Les décisions des autres
types de réparation gardent leur résolution existante par clé de réparation.

Le journal append-only n'est pas filtré : le détail d'une issue courante
continue de renvoyer tout `decision_history` de cette clé, y compris les
décisions des runs précédents. Les tests de régression couvrent le détail
extraction, les issues supplemental et extraction-failure, l'adjudication
courante avec et sans `observed_run_id`, le refus du run précédent et la
conservation des deux décisions dans l'historique.
