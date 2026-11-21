# ADR-0010 — Contrats de l’atelier éditorial interactif

Statut : Accepted — 2026-11-21. Créé le 2026-11-18, précisé le 2026-11-20, implémenté en M1–M8, revu, gates verts ; l’orchestrateur a accepté l’ADR le 2026-11-21.

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

Toute persistance suit la tête Alembic courante, `0017_editorial_proposal_bar_chart_operation`. Les migrations 0011–0017 sont additives (détail dans les Conséquences) ; `0001_baseline`, les blobs historiques et leurs hashes restent intacts. Prévalider les objets préexistants ; état partiel ou non conforme : échec avant DDL. Protéger le downgrade selon ADR-0007.

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

Migrations livrées, chaîne linéaire et tête unique `0017_editorial_proposal_bar_chart_operation` : `0011_editorial_workspace_snapshots` (instantanés épinglés, registre append-only), `0012_editorial_threads` (fils, événements, jobs), `0013_bridge_generation_slots` (lease de génération avec fencing), `0014_editorial_proposals` (propositions, ModelRuns, événements), `0015_editorial_proposal_impacts` (outbox d’impacts), `0016_editorial_drafts` (brouillons serveur), `0017_editorial_proposal_bar_chart_operation` (CHECK des opérations étendu à `create_bar_chart`). `0001_baseline` et les blobs historiques restent inchangés.

Opérations ajoutées : placement `after_paragraph` avec `anchor_block_id`, et opérations `create_table`, `create_bar_chart`, `create_source_figure`.

Garde serveur unique `editorial_read_only` : `resolve_revision_target` et l’adoption refusent une écriture sur une cible en lecture seule, avec recontrôle sous verrou avant l’écriture adoptante. Les lectures, le rejet et l’historique restent permis.

Versions vérifiées à la tête (`ARCHITECTURE.md` §6) : schéma d’enrichissement V10, politique V14 (`evidence-backed-bar-charts`), prompt V23, parser V17, document de publication V10, données Typst V42–V44 (renderer v23), SVG `editorial-chart-svg-v2-bar-data` (renderer 2.0), politique de rendu V15.

La route `POST /subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}/accept` est exposée (`editorial_proposal_accept_enabled`, défaut vrai) et l’action UI « Accepter » est disponible.

## Écarts et risques résiduels connus

- Routes legacy `revise`/`restore` de `api/production.py` non gardées par `editorial_read_only` (écrivains legacy admis par D9).
- Retrait et remplacement de figure non exposés dans l’interface actuelle.
- Re-hash des blobs d’inventaire non refait : le chargeur partagé legacy reste utilisé.
- URL canonique de source exposée en lecture comme provenance.
- SVG non assaini à l’ingestion (pipeline legacy M19), assaini seulement à l’aperçu.
- GET de prévisualisation pouvant relancer un rendu `FAILED` sans plafond global (comportement legacy).
- Heuristique de kind V4–V6 : résolveur legacy ADR-0008 (D1/D5).
- Audit axe non réalisé (risque accepté).
- Curseur d’historique non signé (filtrage par sujet intact).
- `editorial_proposals.py` ~4,1 k lignes non contracté (4 183 lignes).
- mypy : erreurs préexistantes hors diff.
- Bundle JS ~771 kB (build local ; 771 451 octets).
- Réutilisation inter-run d’enrichissement non exacte (V10, appels modèle possibles).

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
