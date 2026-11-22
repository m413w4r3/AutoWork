# ADR-0010 — Contrats de l’atelier éditorial interactif

Statut : Accepted — 2026-11-21 ; amendé le 2026-11-22 (M23, voir « Amendement M23 »). Créé le 2026-11-18, précisé le 2026-11-20, implémenté en M1–M8, revu, gates verts ; l’orchestrateur a accepté l’ADR le 2026-11-21.

## Contexte

La mission 22 relie sélection de bloc, discussions Bridge, propositions humaines, brouillons et rendu de publication sans changer la provenance des artifacts historiques. Les révisions actuelles dérivent les cibles de hashes de contenu et écrivent directement un nouvel artifact ; leur placement éditorial ne connaît pas les paragraphes.

## Décisions

### D1 — Identité latérale

`EditorialWorkspace` résout les blocs et tient leur identité dans des snapshots append-only scopés à un `production_run`. Les UUID distinguent les occurrences ; `before_hash` reste une fence de contenu. IDs historiques et refus d’ambiguïté restent compatibles. Le registre garde IDs, structure, pins, hashes et lineage, jamais texte ni preuves.

### D2 — Proposition et adoption

`EditorialProposal` persiste les patchs Bridge et manuels : `preparing → proposed | refused | needs_review`, puis `proposed → accepted | rejected | stale`. Une adoption unique vérifie les octets, fences, politique et CAS ; elle ajoute artifact, lineage, registre et outbox d’impact dans une UOW relationnelle, sans rappel modèle. Les blobs sont uploadés et relus par SHA avant cette UOW.

### D3 — Fil

`EditorialThread` est distinct de `ModelConversation`. Une session est identifiée par `(subject_id, occurrence_id, session_number)` ; `session_number` distingue les sessions successives d’une occurrence. Son origine (sujet, run, occurrence, numéro de session, artifact, version, SHA canonique) est immuable et sa continuité exige un lineage prouvé. GET, entries et reconcile sont adressés par `occurrence_id`. Les événements append-only référencent les `proposal_id` de D2. Les messages et sorties modèle ne créent jamais de proposition ni de preuve. `session_lost` est un résultat typé de réconciliation enregistré dans un événement `reconciliation`, sans table ni colonne dédiée. M2 est message seul ; `propose` reste refusé jusqu’à l’activation après D2 et le cutover unique.

### D4 — Placement

Le placement ajouté est `after_paragraph` avec `anchor_block_id` obligatoire, résolu sur les pins exacts au montage. Ancre supprimée ou ambiguë : enrichissement et aperçu obsolètes, sans repli. Les anciens placements restent lisibles ; même ancre : ordre canonique puis `element_key`.

### D5 — Type de publication

`publication_kind` est explicite et typé. Il vient de la décision de routage persistée ou d’un envelope V7+ ; V4–V6 utilisent seulement le résolveur legacy ADR-0008. Si indéterminé, l’atelier est en lecture seule ; aucun défaut ARTICLE.

### D6 — Persistance additive

Toute persistance suit la tête Alembic courante, `0018_editorial_enrichment_element_operations` (D6 amendée par M23 ; voir A2 et les Conséquences). Les migrations 0011–0018 sont additives ; `0001_baseline`, les blobs historiques et leurs hashes restent intacts. Prévalider les objets préexistants ; état partiel ou non conforme : échec avant DDL. Protéger le downgrade selon ADR-0007.

### D7 — Résolutions M0

- **B1, écrivains — écart formalisé au libellé initial :** inventoriés avant d’exposer les décisions, les écrivains legacy synthesis/enrichment `revise` restent des écritures directes et conservent leurs endpoints. Dans leur UOW, ils appellent `EditorialRevisionEffects`, également utilisé par l’adoption, pour le verrou du run, l’avance du registre et la persistance stale des propositions aux pins changés. Ils ne deviennent pas des façades prepare+accept : fabriquer une proposition ou un ModelRun ferait croire à une origine Bridge inexistante. L’adoption reste l’unique écrivain d’une proposition acceptée et écrit son outbox d’impact; le rebase d’impact utilise le garde et les effets de registre correspondants. Les oracles PostgreSQL accept‖revise/restore imposent un seul gagnant. `publication_text_correction` et publication augmentation sont vérifiées comme n’écrivant aucun artefact synthesis/enrichment.
- **M1, blobs :** upload puis relecture/SHA avant UOW ; celle-ci ne contient que CAS, références et écritures relationnelles. Réconcilier/nettoyer les blobs orphelins de façon idempotente. Éprouver les crashs avant upload, après upload/avant UOW et après commit/avant impact.
- **M2, impacts :** séquence monotone par run ; le worker verrouille le run, relit les pins et ne produit que pour l’état courant. Pins dépassés ⇒ `superseded`, no-op ; l’impact récent reconstruit depuis l’état courant. Une adoption n’attend pas un impact antérieur.
- **M3, variantes :** `variant_group_id = (base pin, occurrence cible, opération, thread_id)`. L’acceptation rend atomiquement les sœurs `stale`. Toute autre proposition aux pins modifiés est projetée puis persistée `stale` avant décision.
- **M4, prose/PII :** brouillons, fils et tours ont les ACL du sujet. Brouillon serveur : TTL configurable, défaut 30 jours après dernière modification, purgé à adoption/rejet/annulation. Blobs de fil conservés comme dossier éditorial, sans purge automatique. Logs, traces et télémétrie d’erreur excluent prose et texte de bloc ; navigateur : mémoire React seulement, sans stockage persistant.
- **M5, accès :** « Ouvrir l’atelier éditorial » depuis détail sujet, onglet Article, `SubjectProduction`, `ReviewItemCard`/`ProductionConsole`, et consultation article/brève. La route réautorise sujet/run ; interdit ou absent montre un état sûr sans fuite.
- **M6, autorité :** cet ADR accepté et `DECISIONS.md` D1–D9 font autorité ; toute divergence arrête le travail et est signalée.
- **m1, frontend/visuel :** types stricts sans `any`, pas de hook/composant générique sans usage démontré, composants de présentation sans logique métier backend, réseau via la couche API. Décision utilisateur : desktop-only. Captures Playwright couvrent les tailles desktop large/intermédiaire et états éditoriaux actifs, avec revue visuelle et commandes pnpm/Make; aucun scroll horizontal de page à partir de 1024 px.
- **m2, snapshot :** DTO plat avec `snapshot_id` opaque et champs `subject_id`, `production_run_id`, `publication_kind`, `synthesis_artifact_id/version/canonical_sha256`, `enrichment_artifact_id/version`, `publication_artifact_id/version`, `pipeline_generation`, `blocks`, `enrichments`, `document_outline`, `capabilities`, `preview_status`. Pas de `pins` ni `outline` parallèles.
- **m3, activation :** M2 expose seulement `message`; T-M3-16 active `Proposer` après le cutover partagé. La route accept reste désactivée; seulement après T-M3-17, l’impact worker est disponible et `EDITORIAL_PROPOSAL_ACCEPT_ENABLED=true` avec l’action UI `Accepter` expose l’acceptation aux utilisateurs.

### D8 — Périmètre des tickets DDL

Un ticket qui ajoute une migration Alembic ne peut modifier, en plus de celle-ci, que `backend/migrations/env.py` (import du module de modèles, exclusion de la métadonnée de baseline gelée), `backend/tests/integration/test_migrations.py` (tête, modules, triggers/fonctions/tables attendus), `backend/tests/test_alembic_chain.py` (tête et chaîne) et `backend/tests/test_architecture_production_lineage.py` (liste des migrations) ; règle complète et oracle d’une base vide : `DECISIONS.md` D8/D8b.

### D9 — Écrivains legacy et périmètre desktop

D9 amende D7.B1 et formalise la décision T-M3-15 (cutover simplifié). Les écrivains legacy `revise` de synthèse et d’enrichissement, `restore` et le rebase d’impact restent des écritures canoniques directes, endpoints inchangés. Dans leur UOW, ils appellent l’étape commune `application/editorial_revision_effects.py` (verrou du run, avance du registre, propositions aux pins changés persistées `stale`), partagée avec l’adoption. Ils ne deviennent pas des façades `prepare + accept` et ne créent ni proposition ni ModelRun factices. L’oracle PostgreSQL accept‖revise/restore impose un seul gagnant. L’UI de l’atelier est desktop-only : UX-19 (« mobile ») est abandonné, sans critère ni capture mobile. La route accept n’est exposée aux utilisateurs qu’après T-M3-17.

## Conséquences

Le registre latéral, l’agrégat, le fil et les outboxes portent identité, décision et reprise. Les reassemblages suivent les nouvelles versions ; les compositions gardent leurs pins jusqu’à action explicite. Le lease singleton de `bridge_generation_slots` sérialise les générations Bridge lancées par l’atelier éditorial avec fencing; il ne limite pas les autres consommateurs de Bridge et ne prouve pas une règle globale côté Bridge. Les écritures legacy directes et l’adoption partagent le verrou de run et les effets registre/stale; seule l’adoption écrit l’outbox d’impact d’une proposition.

Migrations livrées à la décision initiale, en chaîne linéaire : `0011_editorial_workspace_snapshots` (instantanés épinglés, registre append-only), `0012_editorial_threads` (fils, événements, jobs), `0013_bridge_generation_slots` (lease de génération avec fencing), `0014_editorial_proposals` (propositions, ModelRuns, événements), `0015_editorial_proposal_impacts` (outbox d’impacts), `0016_editorial_drafts` (brouillons serveur), `0017_editorial_proposal_bar_chart_operation` (CHECK des opérations étendu à `create_bar_chart`). L’amendement M23 porte la tête à `0018_editorial_enrichment_element_operations` (voir A2) ; `0001_baseline` et les blobs historiques restent inchangés.

Opérations ajoutées à la décision initiale : placement `after_paragraph` avec `anchor_block_id`, et opérations `create_table`, `create_bar_chart`, `create_source_figure`. L’amendement M23 ajoute les sept révisions visuelles typées décrites en A1.

Garde serveur unique `editorial_read_only` : `resolve_revision_target` et l’adoption refusent une écriture sur une cible en lecture seule, avec recontrôle sous verrou avant l’écriture adoptante. Les lectures, le rejet et l’historique restent permis.

Versions vérifiées à la tête (`ARCHITECTURE.md` §6) : schéma d’enrichissement V10, ou V11 lorsqu’un `group_order` explicite existe (politique `editorial-enrichment-v15-element-order`), politique V14 (`evidence-backed-bar-charts`), prompt V23, parser V17, document de publication V10, données Typst V42–V44 (renderer v23), SVG `editorial-chart-svg-v2-bar-data` (renderer 2.0), politique de rendu V15.

La route `POST /subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/accept` est exposée (`editorial_proposal_accept_enabled`, défaut vrai) et l’action UI « Accepter » est disponible.

## Amendement M23 — 2026-11-22

Cet amendement enregistre les décisions livrées par la mission 23. Il précise D6 et les conséquences ; D1–D5 et D7–D9 restent inchangées.

### A1 — Révisions visuelles typées

Sept variantes forment l’union fermée `VisualRevisionPatch` : `ReviseChartPatch` (`rewrite_chart`), `ReviseDiagramPatch` (`rewrite_diagram`), `UpdateVisualCaptionPatch` (`update_visual_caption`), `MoveEnrichmentElementPatch` (`move_enrichment_element`), `ReorderEnrichmentElementsPatch` (`reorder_enrichment_elements`), `ReplaceSourceFigurePatch` (`replace_source_figure`) et `RemoveEnrichmentElementPatch` (`delete_table`, `delete_diagram`, `delete_chart` ou `delete_source_figure` selon le kind). Chaque variante nomme l’élément exact qu’elle modifie ; un kind de wire inconnu est refusé, jamais ignoré.

La validation et l’application passent par une seule fonction, `plan_visual_revision` (`application/editorial_visual_revision/`), utilisée par la préparation manuelle, la préparation Bridge et l’adoption. Une seule route générique les expose : `POST /api/subjects/{subject_id}/editor/blocks/{occurrence_id}/enrichment-revisions`, avec `Idempotency-Key` obligatoire et une union discriminée sur `operation`. Le patch persisté est non canonique ; l’adoption le relit, le réinterprète dans la même union et réexécute le même planificateur sous le verrou du run. L’écrivain d’adoption de D2 reste l’unique écrivain d’une proposition acceptée.

### A2 — Ordre canonique des éléments, schéma V11 et migration 0018

`enrichment_element_order` et `enrichment_order_rank` (`domain/production_editorial_enrichment.py`) sont l’unique autorité d’ordre, consommée par la projection du snapshot, l’assemblage de publication (`resolved_placements`) et le rendu Typst. Pour un artefact antérieur au schéma V11, ou pour un groupe sans `group_order`, l’ordre PDF historique est reproduit : `after_paragraph` par index de famille puis clé ; autres placements par rang de famille puis index puis clé. Le champ `order` des figures n’est consulté à aucun schéma : l’atelier, le snapshot et le PDF partagent donc le même ordre.

Le schéma d’enrichissement V11 ajoute un `group_order` entier optionnel par élément (politique `editorial-enrichment-v15-element-order`). Il n’est écrit que si un ordre explicite existe : une réorganisation, ou une création/déplacement dans un groupe déjà ordonné. Seuls les groupes `after_paragraph` sont réorganisables ; une réorganisation normalise le groupe en `0..n-1`, un déplacement efface le `group_order` de l’élément déplacé et un retrait conserve les positions et ordres des autres membres.

La migration `0018_editorial_enrichment_element_operations` est la tête Alembic courante. Elle ne fait qu’étendre le CHECK `ck_editorial_proposals_operation` avec `update_visual_caption`, `move_enrichment_element` et `reorder_enrichment_elements` ; elle prévalide l’état ancien/nouveau, refuse le downgrade tant que des lignes utilisent les nouvelles opérations et laisse `0001_baseline` intacte.

### A3 — Sémantique de remplacement et de retrait

Un remplacement de figure est appliqué en place : même clé, même placement, même ordre explicite, une seule nouvelle version d’artefact. La figure de remplacement est résolue à nouveau depuis le catalogue autorisé à la préparation et à l’adoption ; l’ancienne version d’artefact conserve la référence à l’ancienne image, les blobs et l’inventaire archivés restent intacts, et l’entrée d’archive remplacée redevient sélectionnable dans le candidat courant. Les `figure_decisions` sont mises à jour et le plafond BRIEF de trois figures reste appliqué.

Un retrait supprime l’élément de sa famille uniquement : les autres éléments gardent leur position de liste et leur `group_order`, les annotations de l’élément sont retirées, et les médias, l’inventaire et les preuves sources ne sont pas modifiés. La trace de décision de la figure retirée est supprimée du nouvel artefact ; les versions antérieures conservent l’historique. L’adoption produit une seule nouvelle version et l’outbox d’impact invalide les rendus dérivés.

### A4 — Ancres BRIEF et fusion overview

`merge_brief_narrative_sections` (`application/brief_publication_builder.py`) est l’unique autorité de fusion des sections overview dans le corps BRIEF : elle produit la carte ancre synthèse → corps publié, consommée à la fois par le corps de publication et par le résolveur de placement. La projection de l’atelier applique la même définition de section overview pour placer les éléments `after_lead` après le bloc fusionné. Une ancre non mappable ou un placement résolu jamais émis refuse l’assemblage ou le rendu (`editorial_anchor_stale`) au lieu de déplacer ou de supprimer silencieusement l’élément.

### A5 — Capacités sincères

Les capacités du snapshot (`can_discuss`, `can_preview_revision`, `manual_edit`, `can_add_enrichment`) sont calculées fail-closed à partir du câblage réel : routes, runtime de discussion, aperçu de révision, écrivain d’adoption et consommateur d’impact disponibles, et au moins un bloc actionnable. Une dépendance absente éteint l’action au lieu d’afficher un contrôle mort.

Les opérations d’élément sont annoncées par élément dans `allowed_operations` : tableau `rewrite_table`/`delete_table`/`update_visual_caption`/`move_enrichment_element` ; graphique `rewrite_chart`/`delete_chart`/`update_visual_caption`/`move_enrichment_element` ; diagramme `rewrite_diagram`/`delete_diagram`/`update_visual_caption`/`move_enrichment_element` ; figure `replace_source_figure`/`delete_source_figure`/`update_visual_caption`/`move_enrichment_element`. Les contrôles visuels restent en outre bornés par `read_only` et par ces opérations ; aucune capacité distincte de révision visuelle n’est exposée. La route de réorganisation ne résout qu’un groupe `after_paragraph` ancré sur un paragraphe ou le chapeau.

### A6 — Portée Bridge et limites de `revise_diagram`

Un fil ouvert sur un élément d’enrichissement peut proposer uniquement `rewrite_chart`, `rewrite_diagram` et `update_visual_caption`. Les déplacements, réorganisations, remplacements et retraits sont manuels uniquement. Aucun éditeur manuel de graphique ou de diagramme n’est livré : leur correction passe par Bridge ; l’atelier ne rend pas l’image du diagramme et l’opérateur travaille depuis le titre, le diff structurel de la proposition et le PDF.

`revise_diagram` conserve la clé, le placement et l’ordre explicite, refuse un patch sans changement et recompile le diagramme avec le compilateur D2 épinglé. Un nœud ou une arête ne peut être ajouté que si ses références de preuve étaient déjà admises pour ce diagramme (`diagram_evidence_not_previously_admitted` sinon) ; les preuves du purpose analytique sont préservées et doivent rester utilisées par le graphe. La route manuelle ne transporte pas `relation_type` : le traducteur force `FACTUAL` et une direction par défaut, tandis que le parseur Bridge préserve `relation_type` et `direction`. L’adoption recompile sous verrou ; si le binaire D2 installé ne correspond pas à la version épinglée, la compilation refuse (`diagram_compiler_version_mismatch`) au lieu de modifier l’asset silencieusement. L’ancien asset compilé reste référencé par la version antérieure de l’artefact.

### A7 — Risques résiduels acceptés en M23

- `application/editorial_proposals.py` reste un monolithe d’environ 5 300 lignes (5 267 au 2026-11-22), non contracté.
- Dette de seam `PinnedEnrichmentInputs` (S-03/S-04) : les nouveaux modules atteignent des attributs privés de service et des chaînes `getattr`, et la séquence de chargement épinglé comme la persistance de préparation visuelle restent dupliquées. Un seam public typé reste à extraire.
- Aucune preuve de transport Bridge réel : tous les chemins Bridge sont exercés avec des doubles fournisseur.
- Les e2e UI tournent sur une API stubbée ; le smoke real-backend-lite (`frontend/e2e-real/editorial/workspace_real.spec.ts`) existe mais exige une attestation d’environnement explicite et n’est pas exécuté par la configuration Playwright éditoriale standard.
- Un BRIEF V9 historique dont les `resolved_placements` portent des coordonnées synthèse refuse le rendu (`editorial_anchor_stale`) jusqu’à réassemblage.
- La réutilisation inter-run d’enrichissement n’est pas exacte : les ancres sont scopées au run, les quatre étapes amont coûteuses sont réutilisées sans appel modèle, et chaque run paie un appel d’enrichissement.
- La correction graphique/diagramme est réservée à Bridge et l’audit axe n’a pas été réalisé.

## Écarts et risques résiduels connus

- Routes legacy `revise`/`restore` de `api/production.py` non gardées par `editorial_read_only` (écrivains legacy admis par D9).
- Re-hash des blobs d’inventaire non refait : le chargeur partagé legacy reste utilisé.
- URL canonique de source exposée en lecture comme provenance.
- SVG non assaini à l’ingestion (pipeline legacy M19), assaini seulement à l’aperçu.
- GET de prévisualisation pouvant relancer un rendu `FAILED` sans plafond global (comportement legacy).
- Heuristique de kind V4–V6 : résolveur legacy ADR-0008 (D1/D5).
- Audit axe non réalisé (risque accepté).
- Curseur d’historique non signé (filtrage par sujet intact).
- Dette et limites introduites par M23 (monolithe, seam épinglé, transport Bridge non prouvé, e2e stubbée, BRIEF V9, réutilisation non exacte, limites graphique/diagramme) : voir Amendement M23 §A7.

## Options rejetées

- IDs dans les blobs canoniques : hashes et schémas synthesis/enrichment/publication changeraient.
- Liaison du fil à chaque version : elle casserait l’historique lors d’une réécriture de la même occurrence.
- Table de propositions propre au fil : elle dupliquerait D2.
- Repli silencieux d’une ancre absente en fin de section : il déplacerait le contenu sans décision éditoriale.
- ACL par acteur propre à l’atelier : le produit n’en a pas; la frontière reste l’identité API globale utilisée par `/content`, les snapshots et les fils.
- Porte d’approbation G2/G3 dédiée au chemin éditorial : la révision ciblée existante n’en a pas. Avant chaque soumission, l’atelier réévalue TLP, `external_llm_allowed` et les révocations de source; un refus ne déclenche aucun appel gateway. `do_not_submit` n’est pas un veto à Bridge.
- Rétention spécifique d’un blob de message orphelin après enqueue échoué : le blob adressé par contenu suit la rétention du fil (D7).
- Validation profonde de V8 côté frontend : le backend reste l’autorité de validation.
- Lease partagé avec tous les utilisateurs Bridge : le slot ne couvre que les générations éditoriales de l’application.

**Risque résiduel à arbitrer :** le fil envoie à Bridge du contexte dérivé des sources sous les contrôles TLP, `external_llm_allowed` et révocation décrits ci-dessus. Faut-il ajouter une approbation dédiée à cette divulgation ? Cette question reste ouverte et n’est pas tranchée par le présent ADR.
