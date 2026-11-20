# ADR-0010 — Contrats de l’atelier éditorial interactif

Statut : Proposed — créé le 2026-11-18, précisé le 2026-11-20. `DECISIONS.md` D1–D7 fait autorité pour la mission jusqu’à acceptation explicite de cet ADR par l’orchestrateur en M8, après implémentation et revues. Il reste proposé à la base intégrée `887445f0`; cette mise à jour documentaire ne l’accepte pas. En cas de divergence, s’arrêter et signaler.

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

Toute persistance suit la tête Alembic courante. À la base intégrée `887445f0`, la chaîne éditoriale réelle est `0011_editorial_workspace_snapshots` (`backend/migrations/versions/0011_editorial_workspace.py`) → `0012_editorial_threads` (`backend/migrations/versions/0012_editorial_threads.py`) → `0013_bridge_generation_slots` (`backend/migrations/versions/0013_bridge_generation_slots.py`) → `0014_editorial_proposals` (`backend/migrations/versions/0014_editorial_proposals.py`) → `0015_editorial_proposal_impacts` (`backend/migrations/versions/0015_editorial_proposal_impacts.py`). Les migrations 0014 et 0015 ajoutent respectivement propositions/ModelRuns/événements et outbox d’impacts. Le prochain DDL part de 0015 (0016+ inclut notamment `editorial_drafts`). `0001_baseline`, les blobs historiques et leurs hashes restent intacts. Prévalider les objets préexistants ; état partiel ou non conforme : échec avant DDL. Protéger le downgrade selon ADR-0007.

### D7 — Résolutions M0

- **B1, écrivains — écart formalisé au libellé initial :** inventoriés avant d’exposer les décisions, les écrivains legacy synthesis/enrichment `revise` restent des écritures directes et conservent leurs endpoints. Dans leur UOW, ils appellent `EditorialRevisionEffects`, également utilisé par l’adoption, pour le verrou du run, l’avance du registre et la persistance stale des propositions aux pins changés. Ils ne deviennent pas des façades prepare+accept : fabriquer une proposition ou un ModelRun ferait croire à une origine Bridge inexistante. L’adoption reste l’unique écrivain d’une proposition acceptée et écrit son outbox d’impact; le rebase d’impact utilise le garde et les effets de registre correspondants. Les oracles PostgreSQL accept‖revise/restore imposent un seul gagnant. `publication_text_correction` et publication augmentation sont vérifiées comme n’écrivant aucun artefact synthesis/enrichment.
- **M1, blobs :** upload puis relecture/SHA avant UOW ; celle-ci ne contient que CAS, références et écritures relationnelles. Réconcilier/nettoyer les blobs orphelins de façon idempotente. Éprouver les crashs avant upload, après upload/avant UOW et après commit/avant impact.
- **M2, impacts :** séquence monotone par run ; le worker verrouille le run, relit les pins et ne produit que pour l’état courant. Pins dépassés ⇒ `superseded`, no-op ; l’impact récent reconstruit depuis l’état courant. Une adoption n’attend pas un impact antérieur.
- **M3, variantes :** `variant_group_id = (base pin, occurrence cible, opération, thread_id)`. L’acceptation rend atomiquement les sœurs `stale`. Toute autre proposition aux pins modifiés est projetée puis persistée `stale` avant décision.
- **M4, prose/PII :** brouillons, fils et tours ont les ACL du sujet. Brouillon serveur : TTL configurable, défaut 30 jours après dernière modification, purgé à adoption/rejet/annulation. Blobs de fil conservés comme dossier éditorial, sans purge automatique. Logs, traces et télémétrie d’erreur excluent prose et texte de bloc ; navigateur : mémoire React seulement, sans stockage persistant.
- **M5, accès :** « Ouvrir l’atelier éditorial » depuis détail sujet, onglet Article, `SubjectProduction`, `ReviewItemCard`/`ProductionConsole`, et consultation article/brève. La route réautorise sujet/run ; interdit ou absent montre un état sûr sans fuite.
- **M6, autorité :** le présent statut Proposé est subordonné à `DECISIONS.md` D1–D7 jusqu’à acceptation M8 par l’orchestrateur après implémentation et revues ; toute divergence arrête le travail et est signalée.
- **m1, frontend/visuel :** types stricts sans `any`, pas de hook/composant générique sans usage démontré, composants de présentation sans logique métier backend, réseau via la couche API. Décision utilisateur : desktop-only. Captures Playwright couvrent les tailles desktop large/intermédiaire et états éditoriaux actifs, avec revue visuelle et commandes pnpm/Make; aucun scroll horizontal de page à partir de 1024 px.
- **m2, snapshot :** DTO plat avec `snapshot_id` opaque et champs `subject_id`, `production_run_id`, `publication_kind`, `synthesis_artifact_id/version/canonical_sha256`, `enrichment_artifact_id/version`, `publication_artifact_id/version`, `pipeline_generation`, `blocks`, `enrichments`, `document_outline`, `capabilities`, `preview_status`. Pas de `pins` ni `outline` parallèles.
- **m3, activation :** M2 expose seulement `message`; T-M3-16 active `Proposer` après le cutover partagé. La route accept reste désactivée; seulement après T-M3-17, l’impact worker est disponible et `EDITORIAL_PROPOSAL_ACCEPT_ENABLED=true` avec l’action UI `Accepter` expose l’acceptation aux utilisateurs.

## Conséquences

Le registre latéral, l’agrégat, le fil et les outboxes portent identité, décision et reprise. Les reassemblages suivent les nouvelles versions ; les compositions gardent leurs pins jusqu’à action explicite. Le lease singleton de `bridge_generation_slots` sérialise les générations Bridge lancées par l’atelier éditorial avec fencing; il ne limite pas les autres consommateurs de Bridge et ne prouve pas une règle globale côté Bridge. Les écritures legacy directes et l’adoption partagent le verrou de run et les effets registre/stale; seule l’adoption écrit l’outbox d’impact d’une proposition.

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
