# Mission 22 — atelier éditorial : tickets M1–M8

**Base de reprise:** `8e556a9857282d7d42027ef64b6b3dafe2d0ce15`. **État:** proposition locale de DAG; M1 et M2 sont intégrés à cette base, M3–M8 restent à exécuter/revoir; aucun ticket tracker publié. Les contrats restent dans MISSION-SPEC.md, DECISIONS.md et ADR-0010; les choix d’architecture ci-dessous pointent vers les sections d’ARCHITECTURE.md au lieu de les recopier.

**Repères de revue:** S = reviewer Standards; P = reviewer Spec/UX; G = reviewer sécurité-provenance. Tous les tickets requièrent S et P; G est requis dès qu’un ticket touche identité, données persistées, Bridge, preuves, fichiers, publication ou versions canoniques. Les tests restent synthétiques/offline.

**Taille:** S/M/L; L reste borné à une session, cible ≤900 lignes modifiées tests inclus. Chaque implémenteur lit les AGENTS.md de ses chemins. Les chemins listés sont l’autorité d’écriture du ticket. Les chemins partagés ne sont jamais écrits en parallèle.

**Garde-fous frontend communs:** types stricts sans any; aucun hook/composant générique sans usage démontré; composants de présentation sans logique métier backend; accès réseau via la couche API; état serveur dans TanStack Query.

## Graphe, séquencement et orchestration

    M1  T-M1-01 → (T-M1-02 ∥ T-M1-03) → T-M1-04 → T-M1-05
    M2  T-M1-05 → T-M2-06 → T-M2-07 → T-M2-08 → T-M2-09
    M3  T-M2-09 → (T-M3-10 ∥ T-M3-11) → T-M3-12 → T-M3-13 → T-M3-14 → T-M3-15 → T-M3-16 → T-M3-17
    M4  T-M3-17 → T-M4-18 → T-M4-19 → T-M4-20
    M5  T-M4-20 → T-M5-21 → T-M5-22 → T-M5-23
    M6  T-M5-23 → T-M6-24 → T-M6-25
    M7  T-M6-25 → T-M7-26 → T-M7-27 → T-M7-28 → T-M7-29
    M8  T-M7-29 → (T-M8-30 ∥ T-M8-31) → T-M8-32

**Chemin critique:** T-M1-01 → T-M1-02/T-M1-03 (les deux bloquent T-M1-04) → T-M1-05 → T-M2-06…09 → T-M3-10/T-M3-11 (les deux bloquent T-M3-12) → T-M3-13…17 → T-M4-18…20 → T-M5-21…23 → T-M6-24…25 → T-M7-26…29 → T-M8-30/T-M8-31 → T-M8-32.

**Frontières parallèles autorisées:** M1: T-M1-02 ∥ T-M1-03; M3: T-M3-10 ∥ T-M3-11; M8: T-M8-30 ∥ T-M8-31. Les lots restent ordonnés M1→M8 selon DECISIONS D6. Dans tout autre cas, suivre Depends-on; l’intersection de chemins autorisés interdit le parallélisme même si deux branches semblent indépendantes. App.tsx est réservé à T-M1-05; api/main.py à T-M1-02 puis n’est plus modifié; api/editorial_workspace.py est partagé et ses tickets sont séquencés ci-dessous; ProductionArtifactView.tsx est réservé à T-M1-03; les deux services de révision ont chacun un ticket propriétaire en M3 puis un en M8.

**Pointeurs architecture par lot (les tickets héritent de ces sections):** M1: ARCH §1–4, §6–8; M2: §2–6, §9–10; M3: §1–6, §8–9; M4: §2–6, §9–10; M5: §2–8; M6: §2–5, §9; M7: §5–8; M8: §1, §5–10. Les critères ci-dessous ajoutent des gates d’exécution et de test aux contrats de ces sections.

**Décisions de livraison additionnelles:** appliquer DECISIONS D7 en plus de D1–D6; les garde-fous, l’inventaire/cutover et les oracles ci-dessous sont obligatoires avant les décisions canoniques.

**Règle Alembic — D8/D8b, tête unique:** `0001_baseline` reste gelée; toute DDL est additive après la tête intégrée. D8 autorise tout ticket DDL à modifier exactement, en plus de ses chemins propres: `backend/migrations/env.py` (import du nouveau module de modèles seulement), `backend/tests/integration/test_migrations.py` (assertions de tête et liste des modules de modèles seulement), `backend/tests/test_alembic_chain.py` (tête et chaîne seulement), `backend/tests/test_architecture_production_lineage.py` (liste des fichiers de migration seulement). D8b précise que `env.py` peut aussi exclure la nouvelle table de la métadonnée baseline gelée (`frozenset` des tables post-baseline et condition de `_metadata_for_frozen_baseline`), et que `test_migrations.py` peut ajouter les attentes de tables, triggers et fonctions du nouveau DDL. Le test d’intégration éditorial doit pouvoir faire `alembic upgrade head` sur base vide; oracle: `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_migrations.py` et le test éditorial concerné. Chaque migration garde son préflight et garde de downgrade, avec vérification des états absent/exact/partiel.

**Tête intégrée à la base de reprise:** `0011_editorial_workspace_snapshots` (`backend/migrations/versions/0011_editorial_workspace.py`) → `0012_editorial_threads` (`0012_editorial_threads.py`) → `0013_bridge_generation_slots` (`0013_bridge_generation_slots.py`). Les tickets M1-02, M2-06 et M2-09 sont déjà intégrés. Le prochain ticket DDL se base sur cette tête réelle. Les worktrees n’intègrent jamais des branches Alembic concurrentes; à l’intégration, l’orchestrateur réattribue les revision IDs et chaque `down_revision` à la tête unique nouvellement intégrée.

## M1 — socle

### T-M1-01 — Fixtures et harnais éditorial
- **Comportement:** les parcours peuvent être rejoués localement avec un Article à paragraphes dupliqués, un Brief, un enrichissement avec figures archivées et des adaptateurs Bridge/ModelGateway factices; Playwright sait stubber l’API et proposer un mode real-backend-lite.
- **Depends-on / parallélisme:** aucun; bloque M1. Aucune donnée de sujet réelle ni appel provider.
- **Chemins autorisés:** backend/tests/editorial/fixtures/{articles.py,briefs.py,enrichment.py,model_adapters.py}; backend/tests/editorial/test_fixtures.py; backend/tests/integration/editorial/; frontend/e2e/editorial/fixtures/editorialApi.ts; frontend/e2e/editorial/workspace.spec.ts; frontend/e2e-real/editorial/workspace_real.spec.ts; frontend/playwright.editorial.config.ts.
- **Tests rouges d’abord:** test_duplicate_article_fixture_has_distinct_equal_paragraphs (deux textes égaux, deux positions); test_archived_figure_fixture_keeps_sha_and_metadata (SHA, MIME, dimensions stables); test_editorial_stubbed_app_loads (route et sélecteurs visibles sans backend).
- **Acceptation:** faux adapters enregistrent chaque appel et échouent sur sortie réseau; les deux modes Playwright partagent les fixtures; real-backend-lite garde PostgreSQL réel et provider factice. Commandes de base: make test-backend PYTEST_ARGS="tests/editorial/test_fixtures.py -q --tb=short"; cd frontend && pnpm exec playwright test --config playwright.editorial.config.ts e2e/editorial/workspace.spec.ts.
- **UX:** infra, pas un scénario produit.
- **Reviewers:** Standards + Spec; sécurité-provenance vérifie uniquement que les fixtures sont synthétiques.
- **Stop:** fixture contenant preuve CTI réelle, secret ou URL signée.
- **Taille:** M. Commande PostgreSQL préparée pour T-M1-02; l’orchestrateur lance lui-même le Make cible Docker: make test-integration INTEGRATION_TEST_PATH=tests/integration/editorial/test_workspace_snapshot_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short".

### T-M1-02 — Snapshot et sélection d’occurrence
- **Comportement:** cliquer un paragraphe affiche l’occurrence exacte, même si son texte est dupliqué; recharger reconstruit la même identité et l’état ambigu reste en lecture seule.
- **Depends-on / parallélisme:** T-M1-01; parallèle avec T-M1-03, chemins disjoints.
- **Chemins autorisés:** backend/migrations/versions/<revision>_editorial_workspace.py; backend/src/cti_app/domain/editorial_workspace.py; backend/src/cti_app/application/editorial_workspace/{__init__.py,service.py,registry.py}; backend/src/cti_app/infrastructure/database/models/editorial_workspace.py; backend/src/cti_app/infrastructure/database/repositories/editorial_workspace.py; backend/src/cti_app/api/editorial_workspace.py; backend/src/cti_app/api/main.py; backend/tests/test_editorial_workspace_snapshot.py; backend/tests/integration/editorial/test_workspace_snapshot_postgres.py; frontend/src/api/editorialWorkspace.ts; frontend/src/features/editorial-workspace/{EditorialWorkspacePage.tsx,EditorialDocument.tsx,EditorialDocument.test.tsx}.
- **Tests rouges d’abord:** test_legacy_bootstrap_is_stable_and_duplicate_ids_differ (UUIDv5 stable, deux IDs); test_registry_contains_no_text_or_evidence (registre limité à structure/pins); test_snapshot_postgres_is_unique_append_only (conflit unique et refus update/delete); test_click_selects_server_block_id (UI envoie block_id reçu).
- **Acceptation:** DTO plat reprend exactement subject_id, production_run_id, publication_kind, synthesis_artifact_id/version/canonical_sha256, enrichment_artifact_id/version, publication_artifact_id/version, pipeline_generation, blocks, enrichments, document_outline, capabilities, preview_status et snapshot_id opaque; aucun pins/outline parallèle. GET/resolve est pin-scoped; bootstrap idempotent sous concurrence; projection lit les blobs exacts; ambiguïté bloque l’édition.
- **UX:** UX-01, UX-07.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** migration partielle/non conforme, blob historique modifié ou liaison d’occurrence devinée.
- **Taille:** L.

### T-M1-03 — Extraire le rendu de lecture de ProductionArtifactView
- **Comportement:** la vue historique garde le même document publié, tandis que le nouvel atelier peut rendre ses blocs depuis le snapshot sans ajouter sélection ni édition à la vue historique.
- **Depends-on / parallélisme:** T-M1-01; parallèle avec T-M1-02. Caractérisation avant extraction.
- **Chemins autorisés:** `frontend/src/components/ProductionArtifactView.tsx`, `frontend/src/components/ProductionArtifactView.test.tsx`, et les fichiers extraits intégrés dans `frontend/src/features/publication/`: `ExtractionDocumentPreview.tsx`, `ProductionExtractionPreview.tsx`, `ProductionExtractionSourceList.tsx`, `ProductionExtractionV1Preview.tsx`, `ProductionReferenceCorpusView.tsx`, `ProductionSynthesisView.tsx`, `PublicationDiagnosticsPanel.tsx`, `PublicationDiagramView.tsx`, `PublicationDocumentSections.tsx`, `PublicationDocumentView.tsx`, `PublicationDocumentView.test.tsx`, `PublicationEnrichmentsAt.tsx`, `PublicationFigureView.tsx`, `PublicationPdfPanel.tsx`, `PublicationProvenance.tsx`, `PublicationSemanticContent.tsx`, `PublicationTableView.tsx`, `productionStageLabels.ts`, `publicationArtifactDisplay.ts`, `publicationArtifactSchemas.ts`, `publicationDocumentHelpers.ts`, `publicationValueGuards.ts`.
- **Tests rouges d’abord:** test_publication_view_characterization_preserves_document_order (DOM/canonique identique avant/après); test_publication_view_preserves_v4_v8_read_contracts (fixtures historiques restent visibles).
- **Acceptation:** ProductionArtifactView devient un adaptateur de lecture; extraction en modules focalisés, sans déplacer en bloc son corps complet; l’atelier garde son modèle snapshot.
- **UX:** UX-01, UX-07, UX-20.
- **Reviewers:** Standards + Spec; sécurité-provenance vérifie le maintien des références consultables.
- **Stop:** régression de rendu, nouvelle cible construite côté client ou dérive vers un refactor global.
- **Taille:** M.

### T-M1-04 — Corriger publication_kind sans défaut ARTICLE
- **Comportement:** Article/Brève reste correct dans /content, l’écran de publication et le snapshot; si le kind ne peut être résolu, l’atelier est en lecture seule.
- **Depends-on / parallélisme:** T-M1-02, T-M1-03; partagé avec la projection PublicationDocumentView, donc séquencé après M1-03.
- **Chemins autorisés:** backend/src/cti_app/application/subject_content.py; backend/src/cti_app/application/production_synthesis_revision.py; backend/src/cti_app/api/subject_content.py; backend/tests/test_subject_content_publication_kind.py; backend/tests/test_publication_kind_resolution.py; frontend/src/api/subjectContent.ts; frontend/src/api/production.ts; frontend/src/features/subject/ArticleTab.tsx; frontend/src/features/publication/PublicationDocumentView.tsx; frontend/src/features/subject/ArticleTab.test.tsx.
- **Tests rouges d’abord:** test_v8_brief_kind_survives_content_projection (kind BRIEF après unwrap); test_missing_legacy_kind_is_read_only (aucun fallback ARTICLE); test_article_tab_uses_explicit_kind (filtre et libellé alignés).
- **Acceptation:** V7/V8 gardent le kind explicite de routage; V4–V6 passent uniquement par le résolveur ADR-0008; absence non résolue bloque toute adoption.
- **UX:** UX-14, UX-20.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** source de routage absente/contradictoire; ne pas inférer ARTICLE depuis la forme du contenu.
- **Taille:** M.

### T-M1-05 — Route, layout et points d’entrée
- **Comportement:** l’utilisateur ouvre /subjects/:id/editor depuis le détail sujet, l’onglet Article, SubjectProduction, ReviewItemCard/ProductionConsole et les écrans de consultation article/brève; le libellé est « Ouvrir l’atelier éditorial ». Accès direct sujet inexistant/interdit est expliqué sans fuite; le bloc reste sélectionné au retour du plan/inspecteur.
- **Depends-on / parallélisme:** T-M1-04; App.tsx et les entrées de navigation appartiennent exclusivement à ce ticket.
- **Chemins autorisés:** frontend/src/App.tsx; frontend/src/features/editorial-workspace/{EditorialWorkspacePage.tsx,EditorialWorkspaceLayout.tsx,EditorialOutline.tsx,EditorialInspector.tsx,EditorialEntryPoints.test.tsx}; frontend/src/components/{SubjectWorkbench.tsx,SubjectProduction.tsx,SubjectWorkbench.test.tsx,SubjectProduction.test.tsx}; frontend/src/features/subject/ArticleTab.tsx; frontend/src/features/edition-workflow/{ProductionConsole.tsx,ReviewItemCard.tsx,EditionPreviewPanel.tsx,EditionPreviewPanel.test.tsx}; frontend/e2e/editorial/{editor-route-auth.spec.ts,editor-entry-points.spec.ts}.
- **Points d’entrée effectivement câblés à la base:** `frontend/src/components/SubjectWorkbench.tsx` (détail sujet), `frontend/src/features/subject/ArticleTab.tsx`, `frontend/src/components/SubjectProduction.tsx`, `frontend/src/features/edition-workflow/ReviewItemCard.tsx` et `ProductionConsole.tsx`, `frontend/src/features/edition-workflow/EditionPreviewPanel.tsx` (consultation article/brève); la route est dans `frontend/src/App.tsx`.
- **Tests rouges d’abord:** test_editor_route_precedes_subject_detail_route (navigation directe/back); test_all_five_entry_surfaces_use_editorial_label_and_subject_id; test_forbidden_or_missing_subject_shows_safe_route_state; test_layout_keeps_selected_block_context (plan/inspecteur ne perd pas le bloc).
- **Acceptation:** route placée avant détail sujet; responsive ≥1440/1024–1439/<1024 et feuille <768 sans scroll horizontal; pas de formulaire de cibles séparé; endpoint revalide l’autorisation sujet/run.
- **UX:** UX-01, UX-14, UX-19.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** route capturée par le détail sujet ou sélection traduite en hash/ordinal client.
- **Taille:** M.

## M2 — fils et Bridge

**Découpage effectivement intégré à la base:** `application/editorial_threads.py` reste la façade. Le contexte et les tours sont dans `editorial_discussion_context.py` et `editorial_conversation_turns.py`; les responsabilités de fin de job, réconciliation, sessions et capacité sont dans `editorial_thread_job_completion.py`, `editorial_thread_reconciliation.py`, `editorial_thread_sessions.py` et `editorial_bridge_gate.py`. Le cycle frontend est porté par `frontend/src/features/editorial-workspace/useEditorialThreadLifecycle.ts`.

### T-M2-06 — Fil append-only par occurrence
- **Comportement:** sélectionner un bloc ouvre son fil ou l’état « unopened »; l’historique suit seulement une continuité prouvée et ne mélange pas deux occurrences identiques.
- **Depends-on / parallélisme:** T-M1-05; DDL réservée à ce ticket.
- **Chemins autorisés:** backend/migrations/versions/<revision>_editorial_threads.py; backend/src/cti_app/domain/editorial_thread.py; backend/src/cti_app/infrastructure/database/models/editorial_threads.py; backend/src/cti_app/infrastructure/database/repositories/editorial_threads.py; backend/src/cti_app/application/{editorial_threads.py,editorial_thread_common.py,editorial_thread_types.py,editorial_thread_sessions.py}; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_threads.py; backend/tests/integration/editorial/test_threads_postgres.py; frontend/src/features/editorial-workspace/{EditorialConversation.tsx,EditorialConversation.test.tsx,useEditorialThreadLifecycle.ts}.
- **Tests rouges d’abord:** test_get_unopened_thread_creates_no_row; test_thread_origin_is_immutable_and_ambiguous_lineage_stales; test_thread_events_append_only_and_sequence_unique; test_thread_bodies_and_model_turns_use_subject_acl.
- **Acceptation:** événement de fil référence proposal_id sans proposition dupliquée; ModelConversation reste privé; objectif/purpose editorial est typé et non-preuve; événements ordonnés/idempotents. Les corps de fil suivent l’ACL sujet et sont gardés comme archive éditoriale (pas de purge automatique); les tours ModelConversation suivent l’ACL sujet; texte absent des logs/traces/télémétrie.
- **UX:** UX-01, UX-03, UX-07, UX-15.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** contenu du message ajouté au registre, preuve canonique ou timeline non autorisée.
- **Taille:** L.

### T-M2-07 — Envoyer un message Bridge asynchrone
- **Comportement:** « Envoyer un message » poste dans le fil, montre attente/génération/réponse et conserve le document ouvert; ce mode ne crée jamais de proposition.
- **Depends-on / parallélisme:** T-M2-06; écrit le routeur API partagé, donc séquentiel.
- **Chemins autorisés:** backend/src/cti_app/application/{editorial_threads.py,editorial_thread_messages.py,editorial_discussion_context.py,editorial_conversation_turns.py,editorial_conversation_turn_execution.py,editorial_conversation_turn_submission.py,editorial_thread_job_completion.py}; backend/src/cti_app/workers/editorial_threads.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_thread_message.py; frontend/src/features/editorial-workspace/{EditorialConversation.tsx,EditorialConversation.test.tsx,editorialWorkspaceQueries.ts,useEditorialThreadLifecycle.ts}.
- **Tests rouges d’abord:** test_message_enqueues_once_and_replay_returns_same_job; test_message_objective_never_creates_proposal_or_canonical_artifact; test_message_context_is_rebuilt_and_policy_checked_before_gateway; test_message_body_never_enters_logs_traces_or_error_telemetry.
- **Acceptation:** M2 n’expose que `message`; POST renvoie 202 avec clé d’idempotence; fake Gateway reçoit seulement le contexte permis; G2/TLP/external_llm_allowed/révocations sont réévalués; réponse reste donnée non-preuve; prose utilisateur n’apparaît que dans le fil autorisé, jamais dans la télémétrie.
- **UX:** UX-03, UX-15.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** test nécessitant Bridge réel, contexte de sujet complet par défaut ou politique d’envoi non autorisée.
- **Taille:** L.

### T-M2-08 — Réconcilier un envoi incertain sans resoumission
- **Comportement:** après timeout/session perdue, l’utilisateur voit l’état incertain et peut lire/réconcilier le ModelRun exact sans renvoyer le message.
- **Depends-on / parallélisme:** T-M2-07; mêmes routeur/API et composant conversation.
- **Chemins autorisés:** backend/src/cti_app/application/{editorial_threads.py,editorial_thread_reconciliation.py,editorial_thread_sessions.py,editorial_conversation_turns.py,editorial_conversation_turn_reconciliation.py,model_conversations.py}; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_thread_reconciliation.py; frontend/src/features/editorial-workspace/{EditorialConversation.tsx,EditorialConversation.test.tsx,useEditorialThreadLifecycle.ts}.
- **Tests rouges d’abord:** test_reconcile_reads_exact_turn_without_second_gateway_call (compteur reste 1); test_lost_session_requires_new_thread (session numéro suivant, même occurrence); test_propose_is_hidden_and_refused_until_unique_adoption_is_ready; test_thread_binding_stale_blocks_propose.
- **Acceptation:** GET/poll n’a pas d’écriture; résultat reconcilié est rejouable; aucune URL Bridge exposée; erreur française avec action sûre; M2 n’expose que le mode message, jamais `propose` (UI masquée et API refusée avec le code `propose_not_enabled`).
- **UX:** UX-03, UX-15.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** issue externe indéterminée déclenchant un second submit ou fil réattaché par similarité.
- **Taille:** M.

### T-M2-09 — Sérialiser les générations Bridge de l’atelier éditorial
- **Comportement:** deux workers/processus de l’atelier ne soumettent pas simultanément; l’interface montre « waiting_for_capacity » ou une réponse disponible sans doublon. Les autres consommateurs Bridge ne sont pas limités par ce slot.
- **Depends-on / parallélisme:** T-M2-08; la migration 0013, intégrée, précède toute DDL M3.
- **Chemins autorisés:** backend/migrations/versions/0013_bridge_generation_slots.py; backend/src/cti_app/application/{editorial_threads.py,editorial_bridge_gate.py}; backend/src/cti_app/infrastructure/database/models/bridge_generation_slots.py; backend/src/cti_app/infrastructure/database/repositories/bridge_generation_slots.py; backend/tests/test_bridge_global_generation_gate.py; backend/tests/integration/editorial/test_bridge_generation_gate_postgres.py; frontend/src/features/editorial-workspace/{EditorialConversation.tsx,useEditorialThreadLifecycle.ts}.
- **Tests rouges d’abord:** test_two_processes_never_submit_bridge_concurrently (faux transport partagé); test_unknown_external_outcome_retains_fencing_slot (slot non libéré).
- **Acceptation:** le slot singleton Postgres à token de fencing sérialise les générations Bridge éditoriales entre processus; il est retenu jusqu’à échec pré-submit prouvé ou réconciliation exacte. Il n’étend pas la limitation aux autres utilisateurs de Bridge. L’oracle de concurrence reste synthétique/offline.
- **UX:** UX-15.
- **Reviewers:** Standards + Spec + sécurité-provenance/concurrence.
- **Stop:** oracle à deux processus absent ou slot libéré alors que le résultat externe reste incertain; dans ce cas suspendre la génération et garder la réconciliation explicite.
- **Taille:** M.

## M3 — propositions et adoption

### T-M3-10 — Expand synthesis: façade vers résolution/validation
- **Comportement:** les révisions synthesis historiques gardent les mêmes résultats pendant qu’un chemin partagé expose résolution et validation testables par l’atelier.
- **Depends-on / parallélisme:** T-M2-09; parallèle avec T-M3-11 sur fichiers disjoints.
- **Chemins autorisés:** backend/src/cti_app/application/production_synthesis_revision.py; backend/src/cti_app/application/editorial_synthesis/{targets.py,patch_validation.py}; backend/tests/test_production_synthesis_revision_facade.py.
- **Tests rouges d’abord:** test_synthesis_facade_characterization_keeps_legacy_target_and_cas; test_synthesis_target_resolution_uses_registry_occurrence.
- **Acceptation:** déplacer des responsabilités ciblées vers modules profonds; façade publique conservée; tests de caractérisation sur replay/hash/erreurs passent à l’intégration.
- **UX:** UX-02, UX-04, UX-07, UX-08 via contrat partagé.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** ancien endpoint change de sémantique ou nouvel ID remplace le legacy_target_id.
- **Taille:** L.

### T-M3-11 — Expand enrichment: façade vers patch/placement
- **Comportement:** les révisions d’enrichissement historiques gardent leurs fences et leur rendu pendant que l’atelier utilise un validateur dédié.
- **Depends-on / parallélisme:** T-M2-09; parallèle avec T-M3-10, sans fichier/test partagé.
- **Chemins autorisés:** backend/src/cti_app/application/production_enrichment_revision.py; backend/src/cti_app/application/editorial_enrichment_revision.py; backend/tests/test_production_enrichment_revision_facade.py.
- **Tests rouges d’abord:** test_enrichment_facade_characterization_keeps_replay_and_fences; test_enrichment_validation_rejects_unsupported_patch.
- **Acceptation:** façade garde les API existantes; validation/target/lineage sortis sans déplacement monolithique; lecteurs V8 inchangés.
- **UX:** UX-09, UX-10, UX-12 via contrats partagés.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** placement legacy silencieusement converti ou blob historique réécrit.
- **Taille:** L.

### T-M3-12 — Préparer et consulter une proposition persistante
- **Comportement:** le résultat Bridge validé devient une carte persistante consultable; le document canonique ne change pas et l’action Bridge « Proposer » reste masquée jusqu’à T-M3-16.
- **Depends-on / parallélisme:** T-M3-10 et T-M3-11; DDL: proposals/model_runs/events.
- **Chemins autorisés:** backend/migrations/versions/<revision>_editorial_proposals.py; backend/src/cti_app/domain/editorial_proposal.py; backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/infrastructure/database/models/editorial_proposals.py; backend/src/cti_app/infrastructure/database/repositories/editorial_proposals.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_proposal_preparation.py; backend/tests/integration/editorial/test_proposal_persistence_postgres.py; frontend/src/features/editorial-workspace/{EditorialProposalCard.tsx,EditorialProposalCard.test.tsx,EditorialConversation.tsx}.
- **Tests rouges d’abord:** test_bridge_patch_becomes_immutable_proposal_with_exact_sha; test_refusal_and_needs_review_do_not_write_canonical_artifact; test_proposal_get_projects_persisted_state_and_base_version; test_proposal_blob_is_verified_before_relational_uow_and_orphan_cleanup_is_idempotent.
- **Acceptation:** patch fermé/typé stocké en blob immuable re-hashé; upload-et-relecture SHA précèdent tout UOW; run/turn exacts liés; `variant_group_id` = (pin de base, occurrence cible, opération, thread_id); préparation/replay et réconciliation d’orphelins idempotents; aucun endpoint propose actif ni acceptation automatique.
- **UX:** UX-02, UX-03, UX-11, UX-15.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** patch libre HTML/SVG/Typst, sortie modèle devient preuve, ou Bridge rappelé pour afficher la carte.
- **Taille:** L.

### T-M3-13 — Comparer et rejeter une proposition
- **Comportement:** l’utilisateur lit comparaison/diff à l’échelle du paragraphe et peut rejeter; le canonique et sa version ne bougent pas.
- **Depends-on / parallélisme:** T-M3-12; API et composants proposition partagés.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_proposal_rejection.py; frontend/src/features/editorial-workspace/{EditorialDiffView.tsx,EditorialProposalCard.tsx,EditorialDiffView.test.tsx}.
- **Tests rouges d’abord:** test_reject_is_terminal_and_keeps_artifact_pins; test_diff_shows_text_changes_not_json; test_variant_reference_resolves_to_proposal_uuid; test_changed_pins_project_proposal_stale_and_persist_before_any_decision.
- **Acceptation:** comparaison côte à côte/empilée et mode différence accessible; patch hash/base affichés en détails; pins changés projettent `stale` dès la lecture et sont persistés stale avant toute décision; rejet versionné/idempotent; accept est exposé seulement après T-M3-15.
- **UX:** UX-02, UX-03, UX-04.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** diff confondant variants, action accepter présente sans adoption prête, ou rejet déclenche révision.
- **Taille:** M.

### T-M3-14 — Implémenter l’adoption unique par CAS
- **Comportement:** le service d’adoption prépare une nouvelle version du bloc; replay retourne le même résultat, sans appel modèle. La route de décision reste désactivée jusqu’au cutover T-M3-15.
- **Depends-on / parallélisme:** T-M3-13; DDL editorial_proposal_impacts réservée ici.
- **Chemins autorisés:** backend/migrations/versions/<revision>_editorial_proposal_impacts.py; backend/src/cti_app/application/editorial_proposal_adoption.py; backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/infrastructure/database/models/editorial_proposal_impacts.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_proposal_adoption.py; backend/tests/integration/editorial/test_proposal_adoption_postgres.py; frontend/src/features/editorial-workspace/{EditorialProposalCard.tsx,EditorialWorkspacePage.tsx}.
- **Tests rouges d’abord:** test_accept_rehashes_patch_revalidates_fences_and_never_calls_gateway; test_accept_cas_appends_artifact_lineage_registry_and_outbox_atomically; test_accepting_variant_stales_siblings_atomically; test_adoption_crash_before_upload; test_adoption_crash_after_upload_before_uow; test_adoption_crash_after_commit_before_impact.
- **Acceptation:** patch et candidat canonique uploadés puis relus par SHA avant UOW; UOW relationnel seulement: CAS, références, lineage, registre, événement et outbox; blob orphelin inoffensif/content-addressed et réconciliation/nettoyage idempotent; verrouille proposal puis run; l’acceptation rend atomiquement les variantes sœurs stale et tout autre proposal aux pins changés est persisté stale avant décision. Aucune adoption partielle, aucun rappel modèle ni repin de composition. Ce ticket fournit l’écrivain unique mais n’active pas encore sa route.
- **UX:** UX-02, UX-03, UX-04, UX-08.
- **Reviewers:** Standards + Spec + sécurité-provenance/concurrence.
- **Stop:** transaction partielle, patch SHA différent, policy/révocation modifiée ou CAS manqué.
- **Taille:** L.

### T-M3-15 — Inventorier et couper tous les écrivains legacy
- **Comportement:** les révisions historiques passent par prepare+accept; aucune route synthesis/enrichment ne contourne adoption, registre, lineage ou outbox avant activation des décisions.
- **Depends-on / parallélisme:** T-M3-14; ticket de cutover exclusif, aucun parallèle sur les écrivains/API concernés.
- **Chemins autorisés:** backend/src/cti_app/application/production_synthesis_revision.py; backend/src/cti_app/application/production_enrichment_revision.py; backend/src/cti_app/application/publication_text_correction.py; backend/src/cti_app/application/editorial_proposal_adoption.py; backend/src/cti_app/application/editorial_revision_impact.py; backend/src/cti_app/api/production.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_writer_inventory.py; backend/tests/test_production_synthesis_revision_contract.py; backend/tests/test_production_enrichment_revision_contract.py; backend/tests/integration/editorial/test_editorial_writer_cutover_postgres.py; frontend/src/features/editorial-workspace/{EditorialProposalCard.tsx,EditorialWorkspacePage.tsx}; frontend/e2e/editorial/editorial-decision-cutover.spec.ts.
- **Tests rouges d’abord:** test_writer_inventory_covers_revise_restore_rebase_and_publication_correction; test_accept_races_legacy_revise_exactly_one_winner_and_registry_outbox_never_bypass; test_legacy_revise_is_prepare_accept_facade; test_impact_rebase_advances_registry_under_run_guard.
- **Acceptation:** tracer et classer chaque chemin de révision synthesis, revise enrichment, restore stale blocks, rebase d’impact et publication_text_correction (si elle écrit synthesis/enrichment); chaque écriture concernée passe par adoption, ou pour l’impact dérivé par le même garde run + registre/lineage/outbox. L’accept contre revise legacy concurrent a exactement un gagnant. La route accept devient accessible seulement après ces oracles; toute correction publication hors scope est justifiée par oracle de non-écriture.
- **UX:** UX-02, UX-04, UX-08, UX-20.
- **Reviewers:** Standards + Spec + sécurité-provenance/concurrence.
- **Stop:** writer non inventorié, revise restant writer privé, course à deux gagnants, ou rebase n’avançant pas le registre.
- **Taille:** L.

### T-M3-16 — Activer « Proposer » après adoption unique
- **Comportement:** « Proposer » transforme le dernier tour Bridge vérifié en proposition persistante uniquement après que T-M3-14/15 livrent agrégat et écrivain unique; message seul reste disponible.
- **Depends-on / parallélisme:** T-M3-12, T-M3-14 et T-M3-15; D2 et le cutover sont requis avant activation frontend/API.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_propose_activation.py; backend/tests/integration/editorial/test_proposal_activation_postgres.py; frontend/src/features/editorial-workspace/{EditorialConversation.tsx,EditorialProposalCard.tsx,EditorialWorkspacePage.tsx}; frontend/e2e/editorial/proposal-activation.spec.ts.
- **Tests rouges d’abord:** test_propose_refused_without_unique_adoption_and_enabled_after_cutover; test_message_objective_still_never_creates_proposal; test_propose_uses_exact_thread_turn_without_second_gateway_call.
- **Acceptation:** la bascule active les routes et contrôle UI dans le même déploiement; endpoint refuse fail-closed si l’adoption unique est indisponible; proposal references turn/run exacts; aucun second appel Bridge pour construire la carte.
- **UX:** UX-02, UX-03, UX-11, UX-15.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** propose visible avant gate, mode message créant une proposition, ou Bridge rappelé pour projeter le dernier tour.
- **Taille:** M.

### T-M3-17 — Sérialiser les impacts après commit
- **Comportement:** après adoption, l’utilisateur voit si enrichissement/publication/PDF sont en attente, périmés ou à jour; un impact rejouable ne défait pas l’édition.
- **Depends-on / parallélisme:** T-M3-16; aucun ticket parallèle sur projection/query partagée.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_revision_impact.py; backend/src/cti_app/workers/editorial_proposal_impacts.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_proposal_impact.py; backend/tests/integration/editorial/test_editorial_proposal_impact_order_postgres.py; frontend/src/features/editorial-workspace/{EditorialWorkspaceStatus.tsx,EditorialWorkspacePage.tsx}.
- **Tests rouges d’abord:** test_impact_sequence_is_monotonic_per_run; test_out_of_order_impact_a_b_marks_old_pins_superseded_noop; test_worker_locks_run_and_rereads_current_pins; test_pending_impact_never_blocks_adoption; test_impact_failure_never_rolls_back_accepted_artifact.
- **Acceptation:** séquence monotone par run; worker verrouille le run et relit les pins courants; un impact dépassé devient `superseded` no-op car le plus récent repart de l’état courant; adoption reste possible avec impact pending; outbox et reprises sont idempotents; composition n’est jamais repinnée automatiquement.
- **UX:** UX-02, UX-16, UX-17.
- **Reviewers:** Standards + Spec + sécurité-provenance/concurrence.
- **Stop:** résultat A/B hors séquence appliqué au mauvais état, impact bloquant une adoption, ou worker publiant/repinnant.
- **Taille:** L.

## M4 — édition manuelle et brouillons

### T-M4-18 — Brouillons locaux par bloc
- **Comportement:** changer de bloc conserve le brouillon local du premier, le plan le signale et l’utilisateur peut le reprendre ou l’annuler.
- **Depends-on / parallélisme:** T-M3-17; frontière frontend réservée au ticket.
- **Chemins autorisés:** frontend/src/features/editorial-workspace/{InlineTextEditor.tsx,EditorialDocument.tsx,EditorialOutline.tsx,EditorialDrafts.ts,InlineTextEditor.test.tsx}.
- **Tests rouges d’abord:** test_switching_blocks_keeps_draft_by_block_id; test_cancel_clears_only_active_draft; test_close_warns_when_unsaved; test_browser_drafts_are_memory_only.
- **Acceptation:** état par occurrence; prose en mémoire React seulement, sans localStorage ni sessionStorage; focus/copie natifs; compteur seulement si limite du contrat; clavier Ctrl/Cmd+S et Échap.
- **UX:** UX-06, UX-18.
- **Reviewers:** Standards + Spec + sécurité-provenance (stockage navigateur).
- **Stop:** persistance longue durée de texte CTI dans le navigateur.
- **Taille:** M.

### T-M4-19 — Configuration rétention/PII et brouillon serveur
- **Comportement:** l’utilisateur peut retrouver un brouillon non canonique sur un autre écran autorisé; ACL, expiry et état indisponible sont explicites.
- **Depends-on / parallélisme:** T-M4-18; DDL editorial_drafts réservée ici.
- **Chemins autorisés:** backend/migrations/versions/<revision>_editorial_drafts.py; backend/src/cti_app/domain/editorial_draft.py; backend/src/cti_app/application/editorial_drafts.py; backend/src/cti_app/infrastructure/database/models/editorial_drafts.py; backend/src/cti_app/infrastructure/database/repositories/editorial_drafts.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_drafts.py; backend/tests/integration/editorial/test_drafts_postgres.py; frontend/src/features/editorial-workspace/{EditorialDrafts.ts,EditorialWorkspacePage.tsx}.
- **Tests rouges d’abord:** test_draft_subject_acl_and_version_fence; test_draft_ttl_defaults_to_30_days_after_last_change_and_is_configurable; test_expired_draft_is_unavailable_and_purged; test_adopt_reject_cancel_purge_server_draft; test_editorial_prose_and_block_text_never_enter_logs_traces_or_error_telemetry; test_unconfigured_retention_fails_closed.
- **Acceptation:** brouillons, corps de fil et tours suivent l’ACL du sujet; blob SHA référencé, version If-Match; TTL serveur configurable, défaut 30 jours après dernière modification; expires_at et purge des brouillons à l’adoption/rejet/annulation/expiration; aucune persistance sans configuration de rétention. Les blobs de fil restent le dossier éditorial, sans purge automatique; les tours ModelConversation suivent l’ACL du sujet sans nouvelle règle de rétention ici. Prose utilisateur et texte de bloc absents des logs/traces/télémétrie; l’UI montre expiry et distingue brouillon serveur/local.
- **UX:** UX-06, UX-08.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** configuration absente/invalide; garder l’écriture serveur désactivée tant que le TTL et les déclencheurs de purge ne sont pas configurés.
- **Taille:** M.

### T-M4-20 — Édition manuelle sans appel modèle
- **Comportement:** modifier/enregistrer un texte crée une nouvelle version canonique après validation humaine, avec le même historique de contexte et zéro appel Bridge.
- **Depends-on / parallélisme:** T-M4-19; réutilise l’unique chemin d’adoption.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/application/editorial_proposal_adoption.py; backend/tests/test_editorial_manual_patch.py; frontend/src/features/editorial-workspace/{InlineTextEditor.tsx,EditorialProposalCard.tsx,EditorialWorkspacePage.tsx,InlineTextEditor.test.tsx}.
- **Tests rouges d’abord:** test_manual_patch_uses_same_validator_and_adoption; test_manual_edit_makes_zero_gateway_calls; test_unadmitted_new_fact_shows_actionable_error.
- **Acceptation:** typed ManualPatch uses D2 validation/CAS/lineage/impact; base-version check prevents overwrite; saved block stays selected and focus returns.
- **UX:** UX-05, UX-06, UX-08.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** direct artifact write outside adoption or correction stylistique qui appelle un modèle.
- **Taille:** M.

## M5 — tableaux, graphiques et after_paragraph

### T-M5-21 — Contrat versionné d’ancre after_paragraph
- **Comportement:** chaque nouvel enrichissement peut être résolu après une occurrence exacte; ancre absente/ambiguë rend l’enrichissement et l’aperçu STALE, jamais déplacés en fin de section.
- **Depends-on / parallélisme:** T-M4-20; contrat backend et types de lecture avant les composeurs UI.
- **Chemins autorisés:** backend/src/cti_app/domain/production_editorial_enrichment.py; backend/src/cti_app/application/production_editorial_enrichment.py; backend/src/cti_app/application/production_enrichment_revision.py; backend/src/cti_app/domain/publication_document.py; backend/src/cti_app/application/publication_builder.py; backend/src/cti_app/application/typst_rendering.py; backend/tests/test_production_editorial_enrichment.py; backend/tests/test_publication_builder_v4.py; backend/tests/test_typst_rendering.py; frontend/src/api/production.ts; frontend/src/components/ProductionArtifactView.test.tsx.
- **Tests rouges d’abord:** test_after_paragraph_requires_registered_anchor; test_deleted_anchor_marks_enrichment_and_preview_stale_without_fallback; test_v8_legacy_placement_still_reads.
- **Acceptation:** enrichment/policy/prompt/parser/publication/render versions bumpés; old readers V4–V8 restent exacts; ordre même ancre = position canonique puis element_key.
- **UX:** UX-09, UX-10, UX-16, UX-20.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** ancre reconstruite depuis ordinal/texte ou lecteur legacy silencieusement resérialisé.
- **Taille:** L.

### T-M5-22 — Insérer un tableau après un paragraphe, jusque dans le PDF
- **Comportement:** depuis « Ajouter après », l’utilisateur crée/ajuste/compare/adopte un tableau typé; l’atelier et le PDF le montrent au même ancrage.
- **Depends-on / parallélisme:** T-M5-21; composants insertion, assembly et chemins Typst partagés, donc séquentiel.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/application/publication_builder.py; backend/src/cti_app/application/typst_rendering.py; backend/tests/test_editorial_table_insertion.py; backend/tests/test_typst_chp_parity.py; frontend/src/features/editorial-workspace/{VisualInsertionComposer.tsx,EditorialDocument.tsx,EditorialProposalCard.tsx,VisualInsertionComposer.test.tsx}; frontend/e2e/editorial/table-placement.spec.ts.
- **Tests rouges d’abord:** test_table_patch_keeps_anchor_through_adoption; test_real_typst_table_follows_anchor_paragraph_in_pdf; Playwright asserts same anchor order in document and preview.
- **Acceptation:** cellules/colonnes structurées, preuves consultables, aucun markup libre; compiler Typst réel épinglé avec FONT_BUNDLE_ROOT et TMPDIR; inspecter visuellement le PDF (ordre, coupe, légende) avant approbation.
- **UX:** UX-09.
- **Reviewers:** Standards + Spec + sécurité-provenance + review visuelle PDF.
- **Stop:** placement React temporaire, fallback d’ancre, compile non réel ou PDF non inspecté.
- **Taille:** L.

### T-M5-23 — Graphiques à barres étayés et rendus
- **Comportement:** l’utilisateur peut proposer un graphique à barres seulement depuis valeurs documentées; prévisualise preuves/labels et retrouve le même graphique dans le PDF.
- **Depends-on / parallélisme:** T-M5-22; domaine/compilateur/renderer partagés, donc séquentiel.
- **Chemins autorisés:** backend/src/cti_app/domain/production_editorial_enrichment.py; backend/src/cti_app/application/production_editorial_enrichment.py; backend/src/cti_app/application/analytic_chart_compilation.py; backend/src/cti_app/infrastructure/analytic_chart_renderer.py; backend/src/cti_app/application/publication_builder.py; backend/src/cti_app/application/typst_rendering.py; backend/tests/test_editorial_bar_chart.py; backend/tests/test_typst_chp_parity.py; frontend/src/features/editorial-workspace/{VisualInsertionComposer.tsx,EditorialDocument.tsx,VisualInsertionComposer.test.tsx}; frontend/e2e/editorial/bar-chart.spec.ts.
- **Tests rouges d’abord:** test_bar_data_requires_finite_evidence_backed_values; test_insufficient_data_refuses_without_invented_value; test_real_typst_bar_chart_pdf_has_labels_and_anchor.
- **Acceptation:** BarChart typé, compiler local déterministe/SHA/versionnés; aucun SVG arbitraire; compile Typst réel (TYPST_BINARY épinglé, FONT_BUNDLE_ROOT, TMPDIR) et inspection visuelle du PDF.
- **UX:** UX-10, UX-11.
- **Reviewers:** Standards + Spec + sécurité-provenance + review visuelle PDF.
- **Stop:** valeur non étayée, SVG fourni par modèle, test sans vrai Typst ou inspection PDF manquante.
- **Taille:** L.

## M6 — galerie de figures archivées

### T-M6-24 — Catalogue autorisé et miniatures bornées
- **Comportement:** l’utilisateur filtre les figures candidates et lit aperçu, provenance et exclusion; les vignettes ne révèlent ni clé MinIO ni URL distante.
- **Depends-on / parallélisme:** T-M5-23; API gallery/router est partagé et séquencé.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_figure_catalog.py; backend/src/cti_app/api/editorial_workspace.py; backend/src/cti_app/infrastructure/figure_thumbnails.py; backend/tests/test_editorial_figure_catalog.py; backend/tests/integration/editorial/test_figure_authorization_postgres.py; frontend/src/features/editorial-workspace/{SourceFigureGallery.tsx,SourceFigureGallery.test.tsx}.
- **Tests rouges d’abord:** test_figure_catalog_scopes_subject_run_and_actor; test_thumbnail_is_bounded_and_never_accepts_blob_key; test_ineligible_figure_exposes_reason_and_is_not_selectable.
- **Acceptation:** revalide sujet/run/figure à chaque lecture; source inventory archivée uniquement; SVG rasterisé/sanitisé localement; pagination/miniatures bornées; cache privé et nosniff.
- **UX:** UX-12, UX-13.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** blob ID choisi par client, source distante fetchée, figure pending acceptée ou autorisation implicite.
- **Taille:** L.

### T-M6-25 — Intégrer plusieurs figures avec provenance
- **Comportement:** cocher plusieurs figures éligibles, régler légendes/ordre/ancre et les intégrer via proposition; aucune génération Bridge.
- **Depends-on / parallélisme:** T-M6-24; même composer/adoption/render files.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_proposals.py; backend/src/cti_app/application/editorial_proposal_adoption.py; backend/tests/test_editorial_figure_adoption.py; frontend/src/features/editorial-workspace/{SourceFigureGallery.tsx,VisualInsertionComposer.tsx,EditorialDocument.tsx}; frontend/e2e/editorial/figure-insertion.spec.ts.
- **Tests rouges d’abord:** test_multi_figure_patch_rechecks_archive_sha_mime_size_dimensions; test_brief_blocks_more_than_three_integrated_figures; test_figure_adoption_makes_zero_gateway_calls.
- **Acceptation:** patch conserve SHA/MIME/dimensions/provenance; ARTICLE garde borne inventory; BRIEF borne 3 intégrations; retrait/remplacement restent versionnés; PDF sert asset local.
- **UX:** UX-12, UX-13, UX-14.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** asset distant, adoption sans nouvelle validation inventaire ou provenance inaccessible.
- **Taille:** M.

## M7 — intégration Article/Brève, PDF et expérience

### T-M7-26 — PDF courant/périmé et composition épinglée
- **Comportement:** après adoption, l’utilisateur voit le PDF obsolète, le régénère et consulte la nouvelle version; composition signale ses anciennes pins sans les repointer.
- **Depends-on / parallélisme:** T-M6-25; partage preview/composition avec l’intégration et reste séquentiel.
- **Chemins autorisés:** backend/src/cti_app/application/editorial_revision_impact.py; backend/src/cti_app/application/edition_composition.py; backend/src/cti_app/api/subject_content.py; backend/tests/test_editorial_pdf_composition_impact.py; backend/tests/integration/editorial/test_editorial_pdf_postgres.py; frontend/src/features/editorial-workspace/{EditorialWorkspaceStatus.tsx,EditorialHistory.tsx}; frontend/e2e/editorial/pdf-composition.spec.ts.
- **Tests rouges d’abord:** test_acceptance_marks_pdf_stale_then_rendered_pdf_matches_current_pins; test_edition_composition_detects_stale_without_auto_repin; test_article_and_brief_pdf_use_explicit_kind.
- **Acceptation:** preview/pdf identity reste exacte; READY/STALE/FAILED visible; real render requiert Typst configuré; pins changent seulement après action explicite opérateur.
- **UX:** UX-02, UX-14, UX-16, UX-17.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** preview d’une autre version servie comme actuelle ou composition repinnée automatiquement.
- **Taille:** L.

### T-M7-27 — Versions V4–V8 en lecture historique
- **Comportement:** l’utilisateur ouvre une ancienne publication explicitement en lecture seule; l’octet canonique et son SHA restent inchangés.
- **Depends-on / parallélisme:** T-M7-26; composants historiques partagés avec workspace, donc séquentiel.
- **Chemins autorisés:** backend/src/cti_app/domain/publication_document.py; backend/src/cti_app/api/editorial_workspace.py; backend/tests/test_editorial_legacy_read_only.py; frontend/src/api/production.ts; frontend/src/features/publication/PublicationDocumentView.tsx; frontend/src/features/editorial-workspace/{EditorialWorkspacePage.tsx,EditorialWorkspacePage.test.tsx}.
- **Tests rouges d’abord:** test_v4_to_v8_exact_readers_have_no_write_capabilities; test_historical_view_does_not_rewrite_canonical_sha; test_kind_unknown_keeps_history_read_only.
- **Acceptation:** anciens lecteurs inchangés; format non éditable/ambigu explique l’état; pas de migration implicite, backfill destructif ou réécriture blob.
- **UX:** UX-20.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** hash historique diffère après simple ouverture ou old schema converti au chargement.
- **Taille:** M.

### T-M7-28 — Clavier, mobile et focus
- **Comportement:** actions usuelles sont utilisables au clavier/sans hover et sur mobile, sans perdre cible, brouillon ni position.
- **Depends-on / parallélisme:** T-M7-27; styles/composants éditoriaux partagés.
- **Chemins autorisés:** frontend/src/features/editorial-workspace/{EditorialWorkspaceLayout.tsx,EditorialInspector.tsx,EditorialDocument.tsx,EditorialConversation.tsx,EditorialProposalCard.tsx,InlineTextEditor.tsx,SourceFigureGallery.tsx}; frontend/e2e/editorial/keyboard-mobile.spec.ts.
- **Tests rouges d’abord:** test_keyboard_select_edit_compare_decide_and_restore_focus; test_mobile_inspector_sheet_restores_block_and_has_no_page_horizontal_scroll (390px); test_async_reply_does_not_steal_focus.
- **Acceptation:** commandes Tab/flèches/Échap/Ctrl-S/Cmd-Entrée selon contexte; aria-live sans déplacer focus; cibles tactiles ≥44px; reduced-motion; axe WCAG 2.2 AA.
- **UX:** UX-18, UX-19.
- **Reviewers:** Standards/accessibilité + Spec + sécurité-provenance (état/texte accessible).
- **Stop:** raccourci natif intercepté, focus perdu ou scroll horizontal de page.
- **Taille:** M.

### T-M7-29 — Captures Playwright pour revue visuelle
- **Comportement:** reviewers peuvent inspecter captures cohérentes de l’application réelle aux tailles et états éditoriaux requis.
- **Depends-on / parallélisme:** T-M7-28; dernier ticket visuel avant M8.
- **Chemins autorisés:** frontend/e2e/editorial/visual-review.spec.ts; frontend/playwright.editorial.config.ts; frontend/test-results/editorial-workspace/ (artefacts générés, non suivis).
- **Tests rouges d’abord:** test_visual_review_captures_desktop_intermediate_mobile_and_states (capture présente pour chaque viewport/état).
- **Acceptation:** commande: `cd frontend && pnpm exec playwright test --config playwright.editorial.config.ts e2e/editorial/visual-review.spec.ts`; Playwright capture desktop large, intermédiaire, mobile, paragraphe sélectionné, édition, conversation en cours, proposition+diff, tableau inséré, galerie, erreur/conflit; reviewer inspecte lisibilité, hiérarchie, alignements, espacements, débordements, position des boutons, densité, visibilité de l’action principale, place du document et contrôles répétés; captures ne contiennent que les fixtures synthétiques.
- **UX:** UX-01..UX-20 comme états visibles; matrice finale reste T-M8-32.
- **Reviewers:** Standards + Spec + revue visuelle; sécurité-provenance vérifie les fixtures sans données réelles.
- **Stop:** capture absente, overflow bloquant, action principale introuvable ou interface encore confuse.
- **Taille:** M.

## M8 — revue architecture et finition

### T-M8-30 — Contract synthesis façade après migration callers
- **Comportement:** routes synthesis historiques restent compatibles mais exécutent désormais prepare→accept explicite avec clé legacy déterministe; aucun second writer privé ne subsiste.
- **Depends-on / parallélisme:** T-M7-29; parallèle avec T-M8-31, service distinct.
- **Chemins autorisés:** backend/src/cti_app/application/production_synthesis_revision.py; backend/tests/test_production_synthesis_revision_contract.py; backend/tests/test_production_api.py.
- **Tests rouges d’abord:** test_legacy_revise_maps_to_prepare_accept_with_deterministic_key; test_legacy_replay_returns_same_adopted_artifact; test_only_editorial_adoption_writes_synthesis_version.
- **Acceptation:** tous callers migrés; ancien revise demeure façade sur action explicite; previews restent lecture seule; historique/version/IDs legacy conservés.
- **UX:** UX-02, UX-04, UX-08, UX-20.
- **Reviewers:** Standards + Spec + sécurité-provenance/concurrence.
- **Stop:** caller privé restant, appel modèle pendant accept, ou suppression anticipée d’un endpoint client.
- **Taille:** L.

### T-M8-31 — Contract enrichment façade après migration callers
- **Comportement:** routes enrichment historiques restent compatibles et n’écrivent qu’au travers du validateur/adoption partagé, avec placements versionnés.
- **Depends-on / parallélisme:** T-M7-29; parallèle avec T-M8-30, service et tests distincts.
- **Chemins autorisés:** backend/src/cti_app/application/production_enrichment_revision.py; backend/tests/test_production_enrichment_revision_contract.py; backend/tests/test_production_editorial_enrichment_application.py.
- **Tests rouges d’abord:** test_legacy_enrichment_revise_uses_shared_adoption; test_only_editorial_adoption_writes_enrichment_version; test_v8_and_v9_readers_preserve_placement_contracts.
- **Acceptation:** vieux readers/wires restent valides; aucune écriture canonique privée; diagnostics lineage/anchor restent observables; nouvelles versions ne modifient pas les anciennes.
- **UX:** UX-09, UX-10, UX-12, UX-14, UX-20.
- **Reviewers:** Standards + Spec + sécurité-provenance.
- **Stop:** nouvelle placement interprétée silencieusement par ancien reader ou caller non migré.
- **Taille:** L.

### T-M8-32 — Gate final et couverture complète UX-01…UX-20
- **Comportement:** une livraison peut être déclarée complète seulement quand chaque scénario UX est relié à un test e2e ou integration, les lots sont revus et les rendus visuels/PDF acceptés.
- **Depends-on / parallélisme:** T-M8-30 et T-M8-31; gate terminal.
- **Chemins autorisés:** backend/tests/integration/editorial/; backend/tests/test_editorial_*; frontend/e2e/editorial/; docs/design/atelier-editorial/TICKETS.md (matrice et noms de test uniquement).
- **Tests rouges d’abord:** test_editorial_ux_matrix_has_all_twenty_scenarios (aucun UX-xx sans test existant); test_article_brief_editorial_replay_matrix (oracles fake, versions et pins stables).
- **Acceptation:** chaque scénario ci-dessous possède un test exécuté et un résultat; orchestrateur exécute lui-même `make test-backend PYTEST_ARGS=…`, `make lint-backend`, `make typecheck-backend` et `make test-integration` via Docker; lancer `cd frontend && pnpm test --run`, `cd frontend && pnpm lint`, `cd frontend && pnpm typecheck`, `cd frontend && pnpm build`, Playwright et tests real-Typst selon les tickets; vérifier la cohérence D7 dans ADR-0010/ARCHITECTURE. ADR-0010 reste Proposé jusqu’à la décision d’acceptation de l’orchestrateur en M8; DECISIONS D1–D7 reste autoritatif jusque-là. Revue séparée standards/spec/sécurité-provenance; aucune preuve de replay présentée comme test transport live.
- **UX:** UX-01..UX-20, matrice complète ci-dessous.
- **Reviewers:** Standards + Spec + sécurité-provenance + revue visuelle finale.
- **Stop:** scénario sans test e2e/integration, provider live requis, review PDF/capture manquante, ou toute migration en tête concurrente.
- **Taille:** L.

## Matrice de couverture UX

| Scénario | Ticket(s) | Test d’acceptation |
|---|---|---|
| UX-01 sélection, section, actions, preuves | T-M1-02, T-M1-05, T-M8-32 | Playwright e2e + integration snapshot PostgreSQL |
| UX-02 titre proposé, accepté, version/PDF | T-M3-12, T-M3-14..17, T-M7-26, T-M8-32 | Playwright e2e + integration adoption/PDF |
| UX-03 trois tours, variante explicite, fil conservé | T-M2-06..08, T-M3-12..13, T-M3-16, T-M8-32 | Playwright e2e + integration thread/proposal |
| UX-04 rejet sans canonique modifié | T-M3-13, T-M3-15, T-M8-32 | Playwright e2e + unit/integration rejection |
| UX-05 édition sans modèle | T-M4-20, T-M8-32 | Playwright e2e + fake Gateway call-count |
| UX-06 brouillon par bloc | T-M4-18..19, T-M8-32 | Playwright e2e + integration draft ACL/TTL |
| UX-07 doublons indépendants | T-M1-02..03, T-M8-32 | Playwright e2e + integration bootstrap Postgres |
| UX-08 conflit entre sessions | T-M3-14..15, T-M4-19..20, T-M8-32 | Playwright two-session e2e + integration CAS |
| UX-09 tableau après l’ancre identique en PDF | T-M5-21..22, T-M8-32 | Playwright e2e + real-Typst/PDF structure and visual review |
| UX-10 graphique étayé et rendu | T-M5-23, T-M8-32 | Playwright e2e + evidence test + real Typst/PDF visual |
| UX-11 données insuffisantes refusées | T-M3-12, T-M5-23, T-M8-32 | Playwright e2e + domain refusal test |
| UX-12 galerie multi-figure/provenance | T-M6-24..25, T-M8-32 | Playwright e2e + integration asset/provenance |
| UX-13 non-éligible expliqué/bloqué | T-M6-24..25, T-M8-32 | Playwright e2e + API authorization test |
| UX-14 ARTICLE/BRIEF et plafond de trois | T-M1-04, T-M6-25, T-M7-26, T-M8-32 | Playwright Article/Brief e2e + integration |
| UX-15 Bridge indisponible/incertain sans doublon | T-M2-07..09, T-M3-12, T-M3-16, T-M8-32 | Playwright e2e + two-process/reconciliation integration |
| UX-16 PDF obsolète puis actuel | T-M3-17, T-M7-26, T-M8-32 | Playwright e2e + render identity integration |
| UX-17 composition stale sans repin | T-M3-17, T-M7-26, T-M8-32 | Playwright e2e + composition integration |
| UX-18 clavier/focus | T-M1-05, T-M4-18, T-M7-28..29, T-M8-32 | Playwright keyboard e2e + a11y assertions |
| UX-19 mobile sans scroll horizontal | T-M1-05, T-M7-28..29, T-M8-32 | Playwright mobile viewport e2e + screenshot review |
| UX-20 ancienne version en lecture seule | T-M1-04, T-M7-27, T-M8-30..32 | Playwright e2e + canonical SHA/read-only integration |

