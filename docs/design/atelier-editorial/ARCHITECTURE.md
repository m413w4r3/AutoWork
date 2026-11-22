# Architecture de l’atelier éditorial

**État vérifié (M23, 2026-11-22) :** branche `m23/integration`, migration Alembic en tête `0018_editorial_enrichment_element_operations`, une seule tête. Les tickets M1–M8 sont livrés ou partiels (T-M8-32), y compris T-M7-27, T-M7-29 et la matrice UX ; les lots M23 F0–F6 et la remédiation R1 complètent les contrats. ADR-0010 est `Accepted` (2026-11-21, amendé le 2026-11-22). Les décisions D1–D9, l’amendement M23 et le handoff de l’orchestrateur complètent les contrats décrits ici.

## 1. Modules en place

| Couche | Modules et responsabilité constatée |
|---|---|
| API | `api/editorial_workspace.py` expose instantané, résolution, fils, propositions, brouillons, insertions manuelles, catalogue de figures, historique, PDF et état de composition. `api/publication_runtime.py` construit les dépendances de requête sans exiger le lifespan. |
| Application | `editorial_proposals.py` prépare et projette les propositions ; `editorial_visual_revision/` porte le planificateur unique des révisions visuelles (`plan_visual_revision`) ; `editorial_proposal_adoption.py` est l’unique écrivain d’adoption ; `editorial_proposal_adoption_inputs.py` revalide les entrées canoniques ; `editorial_revision_effects.py` applique les effets communs de révision ; `editorial_drafts.py`, `editorial_figure_catalog.py`, `editorial_pdf_preview.py`, `editorial_history.py` et `brief_publication_builder.py` portent les parcours correspondants. |
| Domaine et rendu | `domain/editorial_proposal.py` définit les origines et patchs typés ; `domain/editorial_enrichment_patch.py` définit l’union fermée `VisualRevisionPatch` ; `domain/editorial_figure_keys.py` fournit les clés canoniques de figures ; `domain/production_editorial_enrichment.py` porte tableaux, graphiques, placements, le schéma V11 `group_order` et l’ordre canonique partagé ; `infrastructure/analytic_chart_renderer.py` compile localement les graphiques ; `infrastructure/d2_diagram_compiler.py` compile les diagrammes ; `infrastructure/figure_thumbnails.py` prépare des miniatures bornées. |
| Frontend | `api/editorialWorkspace.ts` expose les DTO typés ; `features/editorial-workspace/` contient l’atelier, les éditeurs de texte/tableau/graphique, la galerie, l’historique et les états PDF/composition. L’interface livrée est desktop-only. |

## 2. Modèle et contrats

L’instantané du workspace épingle les versions exactes du sujet et des artefacts. Son registre append-only identifie des occurrences de bloc distinctes, même lorsque leur texte est identique. Il ne contient ni texte ni preuve. Le snapshot structuré projette les blocs du document et leurs contenus typés `table`, `chart`, `diagram` et `figure` ; chaque bloc d’enrichissement expose son `element_kind`, son `element_key`, son rang d’ordre, sa légende et ses opérations autorisées. Une figure expose `catalog_figure_id` comme identifiant opaque de consultation de miniature. L’occurrence éditoriale (`occurrence_id`) est scopée à une version d’artefact ; la clé d’élément (`element_key`) est stable à travers les versions ; l’asset (asset compilé d’un graphique/diagramme ou blob de figure) est une identité média immuable.

Les propositions sont persistées avec leur origine (`manual_patch` ou `bridge_draft`), leur cible, leurs gardes, leurs références de preuves, leur patch immuable et leurs transitions. Les insertions manuelles restent `ManualTextPatch`, `ManualTablePatch`, `ManualBarChartPatch` et `ManualSourceFigurePatch`. Les révisions d’éléments existants passent par l’union fermée `VisualRevisionPatch` à sept variantes (graphique, diagramme, légende, déplacement, réorganisation, remplacement de figure, retrait) ; une seule fonction, `plan_visual_revision`, valide et applique le patch à la préparation comme à l’adoption. Le client fournit les champs éditoriaux et les identifiants opaques de catalogue ; le serveur résout les blobs et revalide la cible, les preuves et les versions.

L’ordre des éléments est une donnée canonique unique : `enrichment_element_order` et `enrichment_order_rank` alimentent le snapshot, les `resolved_placements` de publication et le rendu Typst. Le schéma d’enrichissement V11 ajoute un `group_order` explicite optionnel, écrit seulement en cas de réorganisation ou d’insertion/déplacement dans un groupe déjà ordonné ; sans ordre explicite, l’ordre PDF historique est reproduit. Seuls les groupes `after_paragraph` sont réorganisables ; un déplacement efface l’ordre explicite de l’élément déplacé.

`ChartSpecV1` porte les deux familles existantes : chronologie et barres. Un point de barre exige une valeur `Decimal` finie, une unité et des preuves admissibles ; la valeur et l’unité doivent être justifiées ensemble. Les SVG de graphiques sont compilés localement et de façon déterministe. Une donnée insuffisante donne un refus explicite, sans valeur inventée.

Une figure archivée est résolue à partir de l’inventaire autorisé du sujet/run. La sélection manuelle peut inclure plusieurs figures, leur légende, leur ordre et une ancre de paragraphe ; la préparation et l’adoption revalident les entrées d’inventaire. Le client ne transmet ni octets, ni URL, ni clé de blob. La limite d’intégration de BRIEF est de trois figures. Le remplacement est appliqué en place et résolu à nouveau depuis le catalogue à la préparation et à l’adoption : même clé, placement et ordre, une seule version, l’ancienne version conservant l’image précédente. Le retrait supprime l’élément de sa famille sans toucher aux archives ni à l’inventaire ; la trace de décision de la figure retirée est supprimée du nouvel artefact, les versions antérieures gardant l’historique.

Les brouillons locaux restent en mémoire par bloc. Les brouillons serveur sont non canoniques, soumis aux ACL du sujet et expirent par défaut 30 jours après leur dernière modification, avec purge aux transitions prévues. L’historique (`EditorialHistory`) est paginé et indique l’origine manuelle ou Bridge des propositions.

## 3. Écritures, effets communs et D9

L’adoption d’une proposition est l’unique chemin qui accepte un patch et écrit l’artefact correspondant. Elle vérifie l’état, le SHA du patch, les gardes de version, la politique et les preuves ; elle ajoute la nouvelle version, la lignée, l’avance du registre et l’outbox d’impact dans une transaction relationnelle, sans appel modèle.

**D9 amende D7.B1 :** les endpoints legacy `revise` de synthèse et d’enrichissement restent des écrivains canoniques directs. Dans leur UOW, ils appellent `EditorialRevisionEffects`, comme l’adoption : garde du run, avance du registre et passage à stale des propositions dont les pins ont changé. Les chemins de restore et de rebase d’impact utilisent également les effets communs. Ces routes ne sont pas des façades `prepare + accept` et ne créent pas de proposition ou de ModelRun artificiels. Les tests PostgreSQL de course acceptation/révision/restauration vérifient le gagnant unique.

T-M8-30 et T-M8-31 sont des tests de contrat sur ces chemins ; ils n’ont pas remplacé les écrivains de production par une façade. Écart documenté de T-M8-31 : la lecture de prévisualisation n’ajoute plus de ligne append-only au snapshot du workspace ; le chemin d’écriture canonique reste inchangé et couvert par les tests de contrat.

Les brouillons ne deviennent canoniques qu’après validation explicite. Les impacts après adoption sont idempotents ; ils peuvent invalider ou reconstruire des artefacts dérivés, mais ne reviennent pas sur le contenu accepté et ne repinnent pas une composition d’édition.

## 4. API constatée

Dans `api/editorial_workspace.py`, les routes couvrent :

- `GET .../editor/snapshot` et `POST .../editor/resolve` pour la projection et la résolution serveur ;
- fils, entrées Bridge et réconciliation d’un envoi incertain, avec propositions et décisions accept/reject ;
- brouillons par occurrence, édition manuelle, propositions de tableau, de graphique et de figures ;
- révisions visuelles d’éléments existants par la route générique `POST /subjects/{subject_id}/editor/blocks/{occurrence_id}/enrichment-revisions` (sept opérations, union discriminée sur `operation`, `Idempotency-Key` obligatoire) ;
- catalogue, aperçu et miniatures des figures archivées ;
- historique paginé, état du PDF, régénération explicite et état de la composition épinglée.

Les routes PDF, composition et historique construisent leurs services sans dépendre du lifespan grâce aux fabriques de requête partagées et à `api/publication_runtime.py`. Les erreurs éditoriales sont typées et les messages sont en français. La résolution des cibles, les autorisations, les preuves et les blobs restent sous l’autorité serveur.

## 5. Persistance et migrations

La chaîne intégrée, depuis la tête historique `0010`, est linéaire :

| Migration | Rôle |
|---|---|
| `0011_editorial_workspace` | Instantanés épinglés et registre append-only des occurrences. |
| `0012_editorial_threads` | Fils, événements, jobs et extension du purpose/objective des conversations éditoriales. |
| `0013_bridge_generation_slots` | Slot clôturé par jeton de fencing pour les générations Bridge éditoriales. |
| `0014_editorial_proposals` | Propositions typées, références ModelRun et événements. |
| `0015_editorial_proposal_impacts` | Outbox transactionnelle des impacts après adoption. |
| `0016_editorial_drafts` | Brouillons serveur mutables, versionnés, non canoniques et expirables. |
| `0017_editorial_proposal_bar_chart_operation` | Extension du CHECK des opérations de proposition pour `create_bar_chart`. |
| `0018_editorial_enrichment_element_operations` | Extension du même CHECK pour `update_visual_caption`, `move_enrichment_element` et `reorder_enrichment_elements` ; préflight et garde de downgrade. |

La tête constatée est `0018_editorial_enrichment_element_operations`. Les migrations restent additives ; `0001_baseline` demeure gelée. Les versions de schémas et blobs historiques ne sont pas réécrites.

## 6. Versions de contrats et de rendu

| Contrat | Version vérifiée à HEAD |
|---|---|
| Schéma d’enrichissement | V10 ; V11 lorsqu’un `group_order` explicite existe |
| Politique d’enrichissement | V14 (`evidence-backed-bar-charts`) ; V15 (`editorial-enrichment-v15-element-order`) pour un artefact V11 |
| Prompt d’enrichissement | V23 |
| Parser du wire d’enrichissement | V17 |
| Document de publication | V10 |
| Données de rendu Typst | V42+ pour les tableaux/graphiques, V43 pour les notes de source des figures, V44 pour leur provenance ; le renderer de publication est v23. |
| SVG de graphique | politique `editorial-chart-svg-v2-bar-data`, renderer 2.0 |
| Politique de rendu publication | V15 |

Les lecteurs historiques restent présents. Les versions V4–V8 sont lues par les lecteurs existants ; l’expérience dédiée d’ouverture historique en lecture seule (T-M7-27) est livrée (route d’artefact de publication, `read_only_reason`, spec `legacy-read-only.spec.ts`, oracle PostgreSQL). Aucune ouverture ne doit réécrire les octets ni le SHA canonique.

## 7. Parcours de données

1. Le serveur résout les épingles et les occurrences, puis projette le snapshot structuré et les capacités autorisées.
2. Un message Bridge est associé à un fil et à un job durable. Après une soumission incertaine, le système réconcilie le ModelRun exact ; il ne soumet pas un second appel à l’aveugle.
3. Une proposition Bridge ou manuelle est validée et persistée sans écriture canonique. Les révisions visuelles passent par la route générique et le planificateur unique, puis l’adoption réexécute le même planificateur sous verrou. La décision humaine passe ensuite par l’écrivain d’adoption unique.
4. L’assemblage résout `after_paragraph` contre le registre épinglé et transmet au rendu Typst le même ordre canonique, explicite (`group_order` V11) ou historique. Pour un BRIEF, la carte de fusion overview de `merge_brief_narrative_sections` fournit les ancres publiées. Une ancre absente rend l’enrichissement et l’aperçu périmés ; aucun repli de placement n’est appliqué.
5. Le statut de prévisualisation PDF est `READY`, `STALE`, `GENERATING` ou `FAILED`. La régénération est explicite. Une composition signale ses pins périmées ; seul un changement explicite de composition les repinne.

## 8. Compatibilité, sécurité et limites connues

- Les blobs canoniques sont append-only et référencés par SHA-256 ; PostgreSQL porte les pins, versions, décisions, gardes et lignées. Les patchs et pièces jointes sont relus et vérifiés avant l’UOW relationnelle.
- Chaque lecture/écriture vérifie le sujet, le run et l’acteur. Les générations Bridge revalident politique, TLP, preuves et révocations. Les textes utilisateur et sorties modèle ne deviennent pas des preuves.
- Les routes de galerie réautorisent chaque accès et résolvent les figures de l’inventaire archivé ; les miniatures sont locales et bornées. Les URLs canoniques des sources restent visibles comme provenance. Le comportement de nettoyage SVG à l’ingestion relève encore du pipeline historique M19.
- Le remplacement et le retrait de figure sont exposés par la route générique de révision visuelle. Le remplacement est en place et revalidé depuis le catalogue ; le retrait ne touche ni aux archives ni à l’inventaire. Le rehash des blobs d’inventaire repose sur le chargeur partagé existant. L’interface de figure ne permet pas de choisir une clé blob.
- Les capacités du snapshot sont calculées fail-closed depuis le câblage réel ; les contrôles visuels restent bornés par `read_only` et `allowed_operations`, et la route de réorganisation ne résout qu’un groupe `after_paragraph`. La correction de graphique et de diagramme passe par Bridge uniquement, sans éditeur manuel ; le diagramme n’est pas rendu dans l’atelier, et la route manuelle `revise_diagram` force `relation_type = FACTUAL`. L’adoption refuse une dérive de version du compilateur D2.
- La revue visuelle desktop de T-M7-29 et l’expérience dédiée de lecture historique T-M7-27 sont livrées (captures desktop multi-états, spec historique en lecture seule et oracles associés). L’audit axe n’a pas été réalisé ; le risque a été accepté dans le handoff. L’UI de l’atelier est desktop-only (D9) : UX-19 (mobile) est abandonné, sans critère ni capture mobile.
- Le transport Bridge réel n’est pas prouvé (doubles fournisseur) ; les e2e UI tournent sur une API stubbée et le smoke real-backend-lite reste conditionnel. Un BRIEF V9 historique dont les placements résolus portent des coordonnées synthèse refuse le rendu (`editorial_anchor_stale`) jusqu’à réassemblage. La réutilisation inter-run d’enrichissement n’est pas exacte. Dette et limites détaillées : ADR-0010, Amendement M23 §A7.
- Le GET de prévisualisation peut relancer un rendu `FAILED` selon le comportement historique ; le handoff le note comme risque préexistant non traité.
