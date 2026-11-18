# ADR-0010 — Contrats de l’atelier éditorial interactif

Statut : Proposed — 2026-11-18. `DECISIONS.md` D1–D7 fait autorité pour la mission jusqu’à acceptation de cet ADR par l’orchestrateur en M8, après implémentation et revues. Cet ADR est son miroir proposé ; en cas de divergence, s’arrêter et signaler.

## Contexte

La mission 22 relie sélection de bloc, discussions Bridge, propositions humaines, brouillons et rendu de publication sans changer la provenance des artifacts historiques. Les révisions actuelles dérivent les cibles de hashes de contenu et écrivent directement un nouvel artifact ; leur placement éditorial ne connaît pas les paragraphes.

## Décisions

### D1 — Identité latérale

`EditorialWorkspace` résout les blocs et tient leur identité dans des snapshots append-only scopés à un `production_run`. Les UUID distinguent les occurrences ; `before_hash` reste une fence de contenu. IDs historiques et refus d’ambiguïté restent compatibles. Le registre garde IDs, structure, pins, hashes et lineage, jamais texte ni preuves.

### D2 — Proposition et adoption

`EditorialProposal` persiste les patchs Bridge et manuels : `preparing → proposed | refused | needs_review`, puis `proposed → accepted | rejected | stale`. Une adoption unique vérifie les octets, fences, politique et CAS ; elle ajoute artifact, lineage, registre et outbox d’impact dans une UOW relationnelle, sans rappel modèle. Les blobs sont uploadés et relus par SHA avant cette UOW.

### D3 — Fil

`EditorialThread` est distinct de `ModelConversation` ; son origine (sujet, occurrence, artifact, version, SHA canonique) est immuable et sa continuité exige un lineage prouvé. Les événements append-only référencent les `proposal_id` de D2. Les messages et sorties modèle ne créent jamais de proposition ni de preuve. M2 est message seul ; `propose` reste refusé jusqu’à l’activation après D2 et le cutover unique.

### D4 — Placement

Le placement ajouté est `after_paragraph` avec `anchor_block_id` obligatoire, résolu sur les pins exacts au montage. Ancre supprimée ou ambiguë : enrichissement et aperçu obsolètes, sans repli. Les anciens placements restent lisibles ; même ancre : ordre canonique puis `element_key`.

### D5 — Type de publication

`publication_kind` est explicite et typé. Il vient de la décision de routage persistée ou d’un envelope V7+ ; V4–V6 utilisent seulement le résolveur legacy ADR-0008. Si indéterminé, l’atelier est en lecture seule ; aucun défaut ARTICLE.

### D6 — Persistance additive

Toute persistance suit la tête Alembic courante. `0001_baseline`, les blobs historiques et leurs hashes restent intacts. Prévalider les objets préexistants ; état partiel ou non conforme : échec avant DDL. Protéger le downgrade selon ADR-0007.

### D7 — Résolutions M0

- **B1, écrivains :** inventorier et couper tous les chemins synthesis/enrichment avant d’activer décision ou édition manuelle. Les `revise` historiques deviennent des façades prepare+accept ; l’impact dérivé utilise le même garde de run et les mêmes registre, lineage et outbox. `advance` s’exécute aussi au rebase d’impact. L’oracle acceptation ‖ revise legacy doit produire un seul gagnant sans contournement.
- **M1, blobs :** upload puis relecture/SHA avant UOW ; celle-ci ne contient que CAS, références et écritures relationnelles. Réconcilier/nettoyer les blobs orphelins de façon idempotente. Éprouver les crashs avant upload, après upload/avant UOW et après commit/avant impact.
- **M2, impacts :** séquence monotone par run ; le worker verrouille le run, relit les pins et ne produit que pour l’état courant. Pins dépassés ⇒ `superseded`, no-op ; l’impact récent reconstruit depuis l’état courant. Une adoption n’attend pas un impact antérieur.
- **M3, variantes :** `variant_group_id = (base pin, occurrence cible, opération, thread_id)`. L’acceptation rend atomiquement les sœurs `stale`. Toute autre proposition aux pins modifiés est projetée puis persistée `stale` avant décision.
- **M4, prose/PII :** brouillons, fils et tours ont les ACL du sujet. Brouillon serveur : TTL configurable, défaut 30 jours après dernière modification, purgé à adoption/rejet/annulation. Blobs de fil conservés comme dossier éditorial, sans purge automatique. Logs, traces et télémétrie d’erreur excluent prose et texte de bloc ; navigateur : mémoire React seulement, sans stockage persistant.
- **M5, accès :** « Ouvrir l’atelier éditorial » depuis détail sujet, onglet Article, `SubjectProduction`, `ReviewItemCard`/`ProductionConsole`, et consultation article/brève. La route réautorise sujet/run ; interdit ou absent montre un état sûr sans fuite.
- **M6, autorité :** le présent statut Proposé est subordonné à `DECISIONS.md` D1–D7 jusqu’à acceptation M8 par l’orchestrateur après implémentation et revues ; toute divergence arrête le travail et est signalée.
- **m1, frontend/visuel :** types stricts sans `any`, pas de hook/composant générique sans usage démontré, composants de présentation sans logique métier backend, réseau via la couche API. Captures Playwright couvrent tailles desktop large/intermédiaire/mobile et états éditoriaux de la mission, avec revue visuelle et commandes pnpm/Make.
- **m2, snapshot :** DTO plat avec `snapshot_id` opaque et champs `subject_id`, `production_run_id`, `publication_kind`, `synthesis_artifact_id/version/canonical_sha256`, `enrichment_artifact_id/version`, `publication_artifact_id/version`, `pipeline_generation`, `blocks`, `enrichments`, `document_outline`, `capabilities`, `preview_status`. Pas de `pins` ni `outline` parallèles.
- **m3, activation :** M2 expose seulement `message` ; API et UI refusent/masquent `propose` jusqu’à D2 et à l’adoption unique, après cutover.

## Conséquences

Le registre latéral, l’agrégat, le fil et les outboxes portent identité, décision et reprise. Les reassemblages suivent les nouvelles versions ; les compositions gardent leurs pins jusqu’à action explicite. La génération Bridge exige un lease partagé avec fencing si l’application n’en garantit pas déjà l’unicité.

## Options rejetées

- IDs dans les blobs canoniques : hashes et schémas synthesis/enrichment/publication changeraient.
- Liaison du fil à chaque version : elle casserait l’historique lors d’une réécriture de la même occurrence.
- Table de propositions propre au fil : elle dupliquerait D2.
- Repli silencieux d’une ancre absente en fin de section : il déplacerait le contenu sans décision éditoriale.
