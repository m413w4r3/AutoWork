# Architecture de l’atelier éditorial

**État vérifié :** HEAD `ac436333` (`m22/integration`), migration Alembic en tête `0017_editorial_proposal_bar_chart_operation`, une seule tête. Les lots M1 à M7 sont intégrés sauf T-M7-27 et T-M7-29. T-M8-30 et T-M8-31 apportent des tests de contrat. ADR-0010 reste `Proposed` et n’est pas accepté par cette mise à jour documentaire. Les décisions D1–D9 et le handoff de l’orchestrateur complètent les contrats décrits ici.

## 1. Modules en place

| Couche | Modules et responsabilité constatée |
|---|---|
| API | `api/editorial_workspace.py` expose instantané, résolution, fils, propositions, brouillons, insertions manuelles, catalogue de figures, historique, PDF et état de composition. `api/publication_runtime.py` construit les dépendances de requête sans exiger le lifespan. |
| Application | `editorial_proposals.py` prépare et projette les propositions ; `editorial_proposal_adoption.py` est l’unique écrivain d’adoption ; `editorial_proposal_adoption_inputs.py` revalide les entrées canoniques ; `editorial_revision_effects.py` applique les effets communs de révision ; `editorial_drafts.py`, `editorial_figure_catalog.py`, `editorial_pdf_preview.py` et `editorial_history.py` portent les parcours correspondants. |
| Domaine et rendu | `domain/editorial_proposal.py` définit les origines et patchs typés ; `domain/editorial_figure_keys.py` fournit les clés canoniques de figures ; `domain/production_editorial_enrichment.py` porte tableaux, graphiques et placements ; `infrastructure/analytic_chart_renderer.py` compile localement les graphiques ; `infrastructure/figure_thumbnails.py` prépare des miniatures bornées. |
| Frontend | `api/editorialWorkspace.ts` expose les DTO typés ; `features/editorial-workspace/` contient l’atelier, les éditeurs de texte/tableau/graphique, la galerie, l’historique et les états PDF/composition. L’interface livrée est desktop-only. |

## 2. Modèle et contrats

L’instantané du workspace épingle les versions exactes du sujet et des artefacts. Son registre append-only identifie des occurrences de bloc distinctes, même lorsque leur texte est identique. Il ne contient ni texte ni preuve. Le snapshot structuré projette les blocs du document et leurs contenus typés `table`, `chart` et `figure` dans l’ordre canonique. Une figure expose `catalog_figure_id` comme identifiant opaque de consultation de miniature.

Les propositions sont persistées avec leur origine (`manual_patch` ou `bridge_draft`), leur cible, leurs gardes, leurs références de preuves, leur patch immuable et leurs transitions. Les patchs manuels présents sont `ManualTextPatch`, `ManualTablePatch`, `ManualBarChartPatch` et `ManualSourceFigurePatch`. Le client fournit les champs éditoriaux et les identifiants opaques de catalogue ; le serveur résout les blobs et revalide la cible, les preuves et les versions.

`ChartSpecV1` porte les deux familles existantes : chronologie et barres. Un point de barre exige une valeur `Decimal` finie, une unité et des preuves admissibles ; la valeur et l’unité doivent être justifiées ensemble. Les SVG de graphiques sont compilés localement et de façon déterministe. Une donnée insuffisante donne un refus explicite, sans valeur inventée.

Une figure archivée est résolue à partir de l’inventaire autorisé du sujet/run. La sélection manuelle peut inclure plusieurs figures, leur légende, leur ordre et une ancre de paragraphe ; la préparation et l’adoption revalident les entrées d’inventaire. Le client ne transmet ni octets, ni URL, ni clé de blob. La limite d’intégration de BRIEF est de trois figures. Le retrait et le remplacement de figure ne sont pas exposés dans l’interface actuelle.

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

La tête constatée est `0017_editorial_proposal_bar_chart_operation`. Les migrations restent additives ; `0001_baseline` demeure gelée. Les versions de schémas et blobs historiques ne sont pas réécrites.

## 6. Versions de contrats et de rendu

| Contrat | Version vérifiée à HEAD |
|---|---|
| Schéma d’enrichissement | V10 |
| Politique d’enrichissement | V14 (`evidence-backed-bar-charts`) |
| Prompt d’enrichissement | V23 |
| Parser du wire d’enrichissement | V17 |
| Document de publication | V10 |
| Données de rendu Typst | V42+ pour les tableaux/graphiques, V43 pour les notes de source des figures, V44 pour leur provenance ; le renderer de publication est v23. |
| SVG de graphique | politique `editorial-chart-svg-v2-bar-data`, renderer 2.0 |
| Politique de rendu publication | V15 |

Les lecteurs historiques restent présents. Les versions V4–V8 sont lues par les lecteurs existants ; l’expérience dédiée d’ouverture historique en lecture seule (T-M7-27) n’est pas livrée à cette base. Aucune ouverture ne doit réécrire les octets ni le SHA canonique.

## 7. Parcours de données

1. Le serveur résout les épingles et les occurrences, puis projette le snapshot structuré et les capacités autorisées.
2. Un message Bridge est associé à un fil et à un job durable. Après une soumission incertaine, le système réconcilie le ModelRun exact ; il ne soumet pas un second appel à l’aveugle.
3. Une proposition Bridge ou manuelle est validée et persistée sans écriture canonique. La décision humaine passe ensuite par l’écrivain d’adoption unique.
4. L’assemblage résout `after_paragraph` contre le registre épinglé et transmet le même ordre au rendu Typst. Une ancre absente rend l’enrichissement et l’aperçu périmés ; aucun repli de placement n’est appliqué.
5. Le statut de prévisualisation PDF est `READY`, `STALE`, `GENERATING` ou `FAILED`. La régénération est explicite. Une composition signale ses pins périmées ; seul un changement explicite de composition les repinne.

## 8. Compatibilité, sécurité et limites connues

- Les blobs canoniques sont append-only et référencés par SHA-256 ; PostgreSQL porte les pins, versions, décisions, gardes et lignées. Les patchs et pièces jointes sont relus et vérifiés avant l’UOW relationnelle.
- Chaque lecture/écriture vérifie le sujet, le run et l’acteur. Les générations Bridge revalident politique, TLP, preuves et révocations. Les textes utilisateur et sorties modèle ne deviennent pas des preuves.
- Les routes de galerie réautorisent chaque accès et résolvent les figures de l’inventaire archivé ; les miniatures sont locales et bornées. Les URLs canoniques des sources restent visibles comme provenance. Le comportement de nettoyage SVG à l’ingestion relève encore du pipeline historique M19.
- T-M6-25 n’expose pas le retrait ni le remplacement des figures. Le rehash des blobs d’inventaire repose sur le chargeur partagé existant. L’interface de figure ne permet pas de choisir une clé blob.
- La revue visuelle desktop de T-M7-29 et l’expérience dédiée de lecture historique T-M7-27 restent à faire. L’audit axe n’a pas été réalisé ; le risque a été accepté dans le handoff. L’UI de l’atelier est desktop-only (D9) : UX-19 (mobile) est abandonné, sans critère ni capture mobile.
- Le GET de prévisualisation peut relancer un rendu `FAILED` selon le comportement historique ; le handoff le note comme risque préexistant non traité.
