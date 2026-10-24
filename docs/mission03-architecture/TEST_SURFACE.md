# Mission 03 / Phase 1 — Surface de test réelle

**SHA étudié :** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`
**Branche :** `mission03/architecture-design`
**Sources :** matrices M2-A et M2-B (`_work/M2A_TEST_SURFACE_EXTERNAL.md`,
`_work/M2B_TEST_SURFACE_ADVISOR_AUDIT_PUB.md`), vérifications ponctuelles du
parent, tests effectivement exécutés ci-dessous.

## 1. Méthode et dénominateurs

- Inventaire réel du checkout : **305** fichiers `test_*.py` hors intégration
  et **61** sous `backend/tests/integration`, soit **366** fichiers de test
  (3 698 + 232 = 3 930 définitions de fonctions de test, paramétrages comptés
  une fois). Le chiffre 303 cité dans la mission est un décompte antérieur ou
  partiel ; le décompte utilisé ici est celui du SHA.
- Niveaux distingués : `unit`, `in-memory fake`, `fake transport`, `replay`,
  `real disposable PostgreSQL`, `real Typst/PDF`, `integration`.
- Un `httpx.MockTransport`, un `SessionSpy`, un `SimpleNamespace` ou un
  repository mémoire ne sont **jamais** comptés comme preuve PostgreSQL.
- Aucun test n'est déclaré `PASS` sans commande exécutée et résultat consigné.
- Les tests listés « présents » sont des inventaires de source ; ils ne sont
  pas des résultats d'exécution.

## 2. Tests effectivement exécutés pendant cette mission

| Commande | Résultat | Portée |
|---|---|---|
| `cd backend && uv run --python 3.12 pytest tests/test_investigation_tools_contract.py tests/test_operator_tool_invocation.py -q --tb=short` | **38 passed in 1.42 s** | contrats du dispatcher, validation de schéma, idempotence, admission opérateur, fingerprints sémantiques, CAS en mémoire |
| `cd backend && uv run --python 3.12 pytest tests/test_investigation_audit_export.py tests/test_mission01_l2_context_pagination.py tests/test_operator_tool_invocation.py::test_context_section_keyset_cursor_is_opaque_chained_and_offset_compatible tests/test_research_context_builder.py::test_context_build_pages_large_sources_with_fixed_memory_budget_and_manifest -q --tb=short` | **40 passed in 3.59 s** | projection d'audit, pagination keyset, curseur chaîné, manifeste de contexte |
| `cd backend && uv run --python 3.12 pytest tests/test_b1_admission_durability.py tests/test_bridge_investigator.py tests/test_investigation_service.py tests/test_investigation_tools_contract.py tests/test_operator_tool_invocation.py tests/test_investigation_advisor_tools.py tests/test_advisor_completeness.py tests/test_advisor_output_reparse.py tests/test_invariant_proposals.py -q --tb=short` | **335 passed in 3.56 s** | admission/durabilité, Bridge, service d'investigation, dispatcher, opérateur, advisor, complétude, reparse, P10 |
| `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_persistence_postgres.py::test_concurrent_operation_key_replay_creates_one_invocation INTEGRATION_PYTEST_ARGS="-q --tb=short"` | **1 passed in 1.01 s** sur le conteneur jetable `postgres-test` (`postgres:16-alpine`, port 55432, tmpfs) | unicité concurrente de `operation_key` et création d'une seule invocation ; ne prouve pas un dispatch unique ni le consume d'un permis |
| `make test-integration INTEGRATION_TEST_PATH="tests/integration/test_operator_context_summary_postgres.py::test_operator_context_summary_and_pages_preserve_postgres_cas tests/integration/test_analyst_opinion_publication_postgres.py::test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5" INTEGRATION_PYTEST_ARGS="-q --tb=short"` | **1 passed, 1 failed** (3.42 s) : le test de contexte passe ; le test de publication échoue parce que `/snap/bin/typst` est cassé (`snap-confine`), pas par défaut de code | contexte opérateur + première page PG ; publication V6 bloquée par l'environnement |
| `TMPDIR=/home/nill/.cache/autowork-mission03/tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst TYPST_FONTS_LOCK_FILE=/home/nill/perso/work/AutoWork/infra/typst-fonts.lock TYPST_BINARY=/snap/typst/current/bin/typst make test-integration INTEGRATION_TEST_PATH=tests/integration/test_analyst_opinion_publication_postgres.py::test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5 INTEGRATION_PYTEST_ARGS="-q --tb=short"` | **1 passed in 3.19 s** | publication V6 réelle sur PostgreSQL jetable avec Typst 0.15.1 fonctionnel ; V5 préservé, avis/version exacts |
| `cd backend && TMPDIR=/home/nill/.cache/autowork-mission03/tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst TYPST_FONTS_LOCK_FILE=/home/nill/perso/work/AutoWork/infra/typst-fonts.lock TYPST_BINARY=/snap/typst/current/bin/typst uv run --python 3.12 pytest tests/test_typst_chp_parity.py::test_real_typst_chronology_keeps_month_only_events_beside_derived_figure -q --tb=short --basetemp=/home/nill/.cache/autowork-mission03/tmp/pytest-typst` | **1 passed in 0.78 s** | régression Typst réelle de la chronologie (mois seuls) avec le template courant |
| `cd backend && TMPDIR=/home/nill/.cache/autowork-mission03/tmp FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst TYPST_FONTS_LOCK_FILE=/home/nill/perso/work/AutoWork/infra/typst-fonts.lock TYPST_BINARY=/snap/typst/current/bin/typst uv run --python 3.12 pytest tests/test_typst_rendering.py tests/test_typst_chp_parity.py tests/test_ci_typst_smoke_fixture.py -q --tb=short --basetemp=/home/nill/.cache/autowork-mission03/tmp/pytest-typst-suite` | **43 passed in 14.58 s** | suite Typst ciblée complète, dont le test de chronologie V6 et les rendus réels |

Total : **414 exécutions offline** (375 tests uniques), **3 tests PostgreSQL
réels** (operation-key concurrent, contexte opérateur, publication V6) et
**43 tests Typst ciblés**. L'échec initial de publication est un défaut
d'environnement (`/snap/bin/typst` cassé), corrigé par `TYPST_BINARY` ; il
n'est pas compté comme un échec de code. Ces exécutions ne prouvent ni le
consume d'un permis, ni le transport fournisseur, ni un PDF de run réel
complet. Aucun test d'intégration d'audit export n'existe encore ; les lots
de test proposés sont dans `IMPLEMENTATION_PLAN.md`.

## 3. Matrice A — effets externes, admission, dispatch, gouvernance

### 3.1 Synthèse

`M2-A` identifie **0/10** oracles complets au niveau d'un **consume de permis
durable** (l'abstraction de permis n'existe pas encore). Il existe en revanche
**18 cas d'intégration PostgreSQL réels dans quatre fichiers**, tous avec un
egress fournisseur/Bridge factice. Aucun test SQLite n'a été trouvé dans ce
périmètre.

### 3.2 Matrice seam × invariant × test × niveau × double × oracle manquant

| Seam | Invariant | Tests existants (file::test) | Niveau / double | Oracle manquant | Risque |
|---|---|---|---|---|---|
| `OperatorToolInvocationService.invoke → _invoke_locked` | replay idempotent, conflit de payload, CAS même version, ambiguïté sémantique | `test_operator_tool_invocation.py::test_same_key_replays_durable_result_and_changed_payload_conflicts` ; `::test_g2_and_budget_guards_refuse_external_invocation` ; `::test_replay_after_persisted_running_marks_review_without_redispatch` ; `test_b1_admission_durability.py::test_concurrent_same_version_invocations_dispatch_only_once` ; `::test_effect_equivalent_representations_block_a_fresh_key` ; `::test_advisor_semantic_normalization_blocks_equivalent_new_key` | unit/in-memory, replay, asyncio | #2, #4, #7 ; #1 au consume | élevé : concurrence mono-processus, fingerprint partiel |
| Enveloppe G2/TLP/divulgation → admission | provider, budget, TLP RED, `do_not_submit`, `external_llm_allowed`, révocation | `test_operator_tool_invocation.py::test_g2_and_budget_guards_refuse_external_invocation` ; `::test_external_llm_disclosure_is_required_for_operator_client` ; `::test_disclosure_controls_block_context_before_external_tool_dispatch` ; `test_investigation_governance.py::test_default_proposal_is_local_only_and_requires_a_separate_g2_approval` ; `::test_g2_approval_persists_actor_reason_and_is_idempotent` ; `::test_revoke_then_explicit_reextension_preserves_scope_and_consumption` ; `test_b1_admission_durability.py::test_stale_envelope_cas_cannot_overwrite_a_revocation` ; `::test_revocation_after_admission_is_rechecked_before_dispatch` ; `integration/test_investigation_vertical_postgres.py::test_unapproved_envelope_denies_external_tool_and_loop_continues_postgres` | in-memory + 1 PG avec fake provider | #1, #3, #10 | élevé : la révocation est injectée séquentiellement avant le dernier read ; le test PG couvre un refus de chemin, pas l'ordre consume |
| `InvestigationService.execute_tool_call → RegistryToolDispatcher` | invocation RUNNING + réservation avant effet ; coût journalisé ; ambiguïté conservée | `test_investigation_service.py::test_running_invocation_is_committed_before_dispatch_and_never_replayed` ; `::test_interrupt_after_running_checkpoint_prevents_tool_dispatch` ; `test_b1_admission_durability.py::test_service_and_dispatcher_reuse_the_persisted_cost_and_key` ; `::test_persisted_running_reservation_replays_to_review_without_transport` ; `::test_ambiguous_post_submission_keeps_reservation_and_replay_does_not_resubmit` ; `integration/test_investigation_persistence_postgres.py::test_concurrent_operation_key_replay_creates_one_invocation` | in-memory + 1 PG repository | #4, #5, #6 | critique : le test PG prouve une ligne, pas un seul dispatch |
| VT adapters/transport | refus externe, normalisation, réponse, replay, observation | `test_investigation_vt_tools.py::test_disclosure_bounds_deny_external_vt_without_a_call` ; `::test_dispatcher_replay_does_not_call_virustotal_twice` ; `test_virustotal_adapter.py::test_file_report_normalizes_hash_and_returns_exact_body` ; `::test_invalid_hash_is_rejected_without_http` ; `integration/test_investigation_vertical_postgres.py::test_real_postgres_tool_path_classifies_vt_download_route_404_as_unavailable` ; `::test_unapproved_envelope_denies_external_tool_and_loop_continues_postgres` | in-memory, `httpx.MockTransport`, PG + fake | #1, #7, #10 entrée brute VT | élevé : les tests de succès restent verts sans autorisation |
| Shodan adapters/transport | refus avant appel, replay, contrat de requête | `test_investigation_shodan_tools.py::test_disclosure_bounds_deny_search_before_external_call` ; `::test_registry_replay_does_not_make_a_second_shodan_call` ; `test_shodan_adapter.py::test_host_maps_partial_and_present_fields_and_preserves_capture_times` ; `::test_private_request_guard_refuses_active_or_unknown_shodan_routes` | in-memory, `httpx.MockTransport` | #1, #7, #10 entrée brute Shodan | élevé : mapping et succès ne prouvent pas l'admission |
| Retrohunt adapter/service | autorisation humaine, slot, submit idempotent, ambiguïté | `test_retrohunt_tools.py::test_submit_is_idempotent_by_exact_rule_corpus_and_parameters` ; `::test_ambiguous_submission_and_crash_replay_never_resubmit` ; `::test_revoked_detection_decision_cannot_authorize_retrohunt` ; `test_virustotal_retrohunt_client.py::test_submit_uses_only_the_v3_job_route_and_header_authentication` ; `::test_submit_rejects_bad_rule_parameters_and_unknown_corpus_without_http` ; `integration/test_retrohunt_postgres.py::test_concurrent_investigations_reserve_exactly_two_subject_slots` ; `::test_replay_revision_ambiguity_and_read_tools_keep_postgres_count` | in-memory, fake HTTP, PG + fake provider | #2, #5, #6, #7, #10 entrée brute Retrohunt | élevé : slot PG réel, mais pas de binding de permis |
| Acquisition adapter/service | G2/TLP, hash/provenance, réservation/réemploi, ambiguïté | `test_investigation_acquisition.py::test_default_g2_envelope_refuses_acquisition_as_a_review_request` ; `::test_approved_g2_acquires_unknown_hit_without_g3_and_replay_is_free` ; `::test_ambiguous_download_failures_remain_needs_review` ; `::test_tlp_and_do_not_submit_prohibit_vt_download` ; `test_investigation_acquisition_tools.py::test_download_tool_requests_permission_outside_g2_envelope` ; `::test_dispatcher_download_reuses_one_g2_reservation_on_replay` ; `integration/test_investigation_acquisition_postgres.py` (6 cas listés en §4) | in-memory, PG + fake ports/FS | #1, #5, #6, #9, #10 entrée brute acquisition | élevé : le provider réel n'est jamais atteint |
| `InvestigationAdvisorTool.execute → Bridge` | divulgation avant consultation, préparation, refus typé, ambiguïté | `test_investigation_advisor_tools.py::test_external_disclosure_denial_never_calls_bridge` ; `::test_advisor_transport_failures_are_explicit_and_never_retried` ; `::test_advisor_does_not_parse_persisted_output_for_ambiguous_rate_limit` ; `test_operator_tool_invocation.py::test_operator_dispatcher_wires_minimal_advisor_request_to_strict_bridge_transport` | in-memory, fake transport | #1, #2, #7, #9, #10 entrée brute advisor | élevé : les tests positifs restent verts sans permis |
| `BridgeInvestigator.turn → transport` | permission externe, identité déterministe, retries, corrections, ambiguïté | `test_bridge_investigator.py::test_external_permission_is_checked_before_transport` ; `::test_ambiguous_bridge_failure_is_never_retried_automatically` ; `::test_rate_limit_retry_honors_retry_after_and_stops_after_one_retry` ; `::test_invalid_compact_output_gets_a_precise_corrective_turn` ; `::test_invalid_output_stops_after_two_corrective_attempts` ; `integration/test_investigation_vertical_postgres.py::test_real_postgres_http_bridge_vertical_investigation_and_v6_publication` ; `::test_postgres_bridge_pre_submission_exhaustion_resumes_with_new_attempt` | in-memory scripted, PG + scripted | #8, #10 entrée brute Bridge | élevé : la portée du permis sur retry/correctif n'est pas testée |
| Recovery / `NEEDS_REVIEW` | crash après réservation/consume, pas de double soumission | `test_b1b_temporal_recovery.py::test_transport_timeout_is_ambiguous_and_never_retried` ; `::test_crash_replay_seals_durable_inflight_submission_without_second_dispatch` ; `::test_crash_after_durable_not_submitted_state_replays_as_review_without_dispatch` ; `test_investigation_service.py::test_external_timeout_requires_reconciliation_before_resume` ; `::test_reconciliation_uses_cas_and_commits_or_releases_ambiguous_budget` ; `::test_crash_replay_does_not_execute_succeeded_tool_twice` ; `test_b1_admission_durability.py::test_persisted_running_reservation_replays_to_review_without_transport` ; `::test_recovered_success_is_replayed_without_a_second_external_call` ; `integration/test_retrohunt_postgres.py::test_replay_revision_ambiguity_and_read_tools_keep_postgres_count` ; `integration/test_investigation_vertical_postgres.py::test_real_postgres_ambiguous_download_logs_safe_terminal_diagnostics` | in-memory/replay, PG partiel | #4, #5, #6 | critique : pas de fault injection autour du consume PostgreSQL |
| Budget / réemploi de résultat | une charge par effet, coût déterministe, filiation du réemploi | `test_investigation_service.py::test_repeated_tool_parameters_across_turns_reuse_observation_and_budget` ; `test_b1_admission_durability.py::test_service_and_dispatcher_reuse_the_persisted_cost_and_key` ; `::test_recovered_success_is_replayed_without_a_second_external_call` ; `integration/test_investigation_acquisition_postgres.py::test_same_subject_sample_reuse_through_registered_tools_in_postgres` ; `integration/test_retrohunt_postgres.py::test_replay_revision_ambiguity_and_read_tools_keep_postgres_count` | in-memory, PG + fake | #7, #9 | moyen-élevé : pas de politique inter-outils de filiation |

### 3.3 Oracles manquants A — formulation RED

| # | Oracle | Seam exact | Première assertion RED | PostgreSQL réel requis |
|---|---|---|---|---|
| 1 | Dispatch refusé sans autorisation effective | service → dispatcher → chaque adapter externe | enveloppe absente/provider interdit/TLP RED/DNT/budget zéro → consume refuse, spies provider à zéro | oui pour le CAS/consume ; non pour la matrice de refus |
| 2 | Permis forgé, copié, rejoué, mal lié | issuer/consumer du permis | altérer un binding ou réutiliser le permis → refus, transport à zéro | oui pour le replay/CAS |
| 3 | Révocation concurrente vs consume | transaction PostgreSQL | pause avant consume, révocation dans une seconde session, reprise → refus, zéro egress | **oui, indispensable** |
| 4 | `RUNNING → NEEDS_REVIEW` avant consume | même transaction que #3 | A commit RUNNING, pause ; B commit NEEDS_REVIEW ; reprise A → zéro consume, zéro transport | **oui** |
| 5 | Crash après réservation ou consume | transaction + recovery | crash avant consume → aucun egress ; crash après consume → permis non rejouable, pas de second egress | **oui** |
| 6 | Issue ambiguë sans double-submit | consume + journal d'issue | provider accepte puis perd la réponse ; replay → `NEEDS_REVIEW`, un seul accepté | oui |
| 7 | Identité d'effet normalisée | préparation partagée admission/permis/payload | deux requêtes normalisées identiques → même digest ; requête matériellement différente → digest distinct | partiel |
| 8 | Egress répétés dans un tour Bridge | `BridgeInvestigator.turn` avant chaque submit | révocation après la première soumission → deuxième submit bloqué ou couvert explicitement | oui pour la portée/revoke |
| 9 | Provenance et budget d'un résultat réutilisé | réemploi inter-invocations | B réutilise A → refs/effect digest/source version liés, budget une fois | oui |
| 10 | Entrée brute sans permis | chaque adapter/transport | appel direct sans permis → fail-closed, spy à zéro ; avec permis correct → un appel | non (fakes) + un test PG de consume |

## 4. Matrice B — advisor, audit, pagination, publication

| Seam | Invariant | Tests existants | Niveau / double | Oracle manquant | Risque |
|---|---|---|---|---|---|
| `build_advisor_dossier → preflight` | sélection pertinente, contradictions, manifeste traçable | `test_advisor_completeness.py::test_canonical_contradiction_omitted_from_dossier_requests_retrieval` ; `::test_dossier_preserves_all_large_observation_excerpts_with_manifest_hashes` | unit pur | pas de chemin repository réel → dossier/preflight soumis | les tests purs restent verts si l'adaptateur n'utilise plus le builder |
| `InvestigationAdvisorTool.execute → disclosure → transport` | preuves CTI permissives + refus explicites | `test_investigation_advisor_tools.py::test_600_byte_dns_raw_base64_reaches_fake_transport` ; `::test_large_legacy_hex_and_base64_measurements_reach_fake_transport` ; `::test_contradictory_permitted_dns_evidence_pieces_are_both_transmitted` ; `::test_generic_api_key_in_cti_evidence_is_not_an_autowork_secret` ; `::test_nested_operational_secret_is_redacted_before_transport` ; `::test_external_disclosure_denial_never_calls_bridge` ; `::test_sample_level_restriction_blocks_explicit_observation` | in-memory + fake transport | **#1 matrice croisée** (allow/refus sur le même payload) | les tests séparés peuvent rester verts sur un chemin non testé |
| F6 raw Base64/DNS | preuve de 600 octets transmise avec offset/taille/SHA | `test_investigation_advisor_tools.py::test_600_byte_dns_raw_base64_reaches_fake_transport` ; `::test_spaced_fragmented_600_byte_base64_reaches_fake_transport` ; `::test_contradictory_permitted_dns_evidence_pieces_are_both_transmitted` | in-memory + fake transport | **couvert au seam fake-transport** ; pas de preuve provider live | ne prouve pas l'adaptateur/provider externe |
| P10 `_normalize_context → _render_p10_prompt` | credential explicite retiré, CTI adjacente intacte | `test_invariant_proposals.py::test_redact_text_redacts_credentials_without_erasing_surrounding_claims` ; `::test_normalize_context_redacts_structured_credentials` ; `::test_render_p10_prompt_redacts_url_credentials_without_losing_claim_context` ; `::test_do_not_submit_never_reaches_external_p10_call` ; `::test_tlp_red_never_reaches_external_p10_call` ; `::test_external_forbidden_policy_never_reaches_external_call` | unit + in-memory fake | **#3 résidu JSON échappé** | le résidu S2 reste vert dans la suite actuelle |
| Advisor payload → raw → reparse | provenance et reparse sans consultation | `test_investigation_advisor_tools.py::test_large_invalid_advisor_output_is_archived_without_content_limit` ; `test_bridge_investigator.py::test_turn_is_structured_and_replayed_by_deterministic_identity` ; `test_advisor_output_reparse.py::test_reparse_appends_verified_parse_without_a_bridge_transport` ; `::test_reparse_refuses_minio_bytes_that_do_not_match_observation_output_sha` | in-memory, fake transport, replay | reparse couvert au seam outil ; stockage production et composition non couverts | un adapter blob différent ou une registration absente reste non détecté |
| Avis versionné / publication V6 | révision/approbation CAS, base V5 préservée | `test_analyst_opinion_service.py::test_revision_rejects_missing_citation_and_stale_version` ; `::test_approval_requires_exact_draft_hash_and_respects_tlp` ; `integration/test_investigation_persistence_postgres.py::test_investigation_repositories_have_memory_postgres_parity` ; `integration/test_analyst_opinion_publication_postgres.py::test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5` | unit + PG réel | pas de PDF final exhaustif | les tests PG restent verts si le renderer omet du contenu |
| Audit projection → reader PG → bundle | cohérence IDs/counts/hashes, omissions, reprise | `test_investigation_audit_export.py::test_778_synthetic_invocations_are_deterministic_and_resume_from_checkpoint` ; `::test_concurrent_exports_leave_one_coherent_bundle` ; `::test_advisor_blob_hash_mismatch_keeps_payload_unavailable` ; `::test_projection_correlates_only_persisted_ids_and_omits_private_text` | unit/in-memory, replay, tmp_path | **#5 export depuis PostgreSQL réel** | le reader SQL peut omettre/mal-scoper des lignes sans être détecté |
| Contexte → page keyset → omissions | pages chaînées, hash, refs, totaux | `test_research_context_builder.py::test_context_build_pages_large_sources_with_fixed_memory_budget_and_manifest` ; `::test_evidence_page_redacts_credentials_before_inclusion_and_hashing` ; `test_mission01_l2_context_pagination.py::test_page_sha256_rejects_falsification_of_every_page_field` ; `::test_context_page_constructors_assert_manifest_consistency` ; `::test_all_sql_keyset_adapters_call_the_shared_predicate_and_request_limit_plus_one` ; `::test_sql_keyset_predicate_is_a_pure_stable_tuple_seek_in_both_directions` ; `test_operator_tool_invocation.py` (curseurs/CAS) | unit + in-memory + `SessionSpy` | **#4 chaîne multi-page PostgreSQL** et lien message-index | le SQL réel peut différer de la mémoire et du spy |
| API contexte → repositories PG | summary/page même version d'état | `integration/test_operator_context_summary_postgres.py::test_operator_context_summary_and_pages_preserve_postgres_cas` | PG réel + ASGITransport | première page seulement, pas de chaîne | duplication/saut possible sur les pages suivantes |
| Document canonique → Typst → PDF | chronologie exhaustive, V6 sans usurpation | `test_publication_builder_v4.py::test_publication_v4_builder_is_exact_deterministic_and_resolves_used_sources` ; `test_analyst_publication_service.py::test_publication_is_idempotent_and_preserves_the_previous_v5_blob` ; `integration/test_analyst_opinion_publication_postgres.py::test_postgres_opinion_approval_publishes_new_v6_and_preserves_v5` ; `test_typst_chp_parity.py::test_real_typst_chronology_keeps_month_only_events_beside_derived_figure` ; `::test_real_typst_and_poppler_render_v6_opinion_without_changing_v5` | unit + PG réel + Typst réel | **#7 document publié complet → PDF final** | la chronologie n'est testée que sur deux entrées synthétiques |

### 4.1 Oracles manquants B — formulation RED

| # | Oracle | Première assertion RED | PostgreSQL requis |
|---|---|---|---|
| 1 | Matrice advisor autorisation/refus | même payload autorisé → marqueurs CTI + SHA/taille du manifeste ; chaque refus (`do_not_submit`, external=false, RED, restriction échantillon) → zéro transport + refs de preuve conservées | non (fake transport) |
| 2 | F6 Base64/DNS | déjà couvert au fake-transport ; conserver comme non-régression | non |
| 3 | S2 JSON échappé | `\{"auth\":\"FIXTURE_ESCAPED_AUTH\"}` absent du prompt ; CVE/network/api_key CTI intactes | non |
| 4 | Pagination chaînée | 3 pages avec ties temporels + insertion ; IDs concaténés = ensemble canonique une fois ; refs/manifestes recalculés | **oui** |
| 5 | Export audit PG | seed invocation/observation/finding/state/opinion ; IDs/counts identiques aux requêtes DB ; hash mismatch → unavailable | **oui** |
| 6 | Reparse | déjà couvert au seam outil ; ajouter composition/stockage réel si retenu | non |
| 7 | PDF final exhaustif | publier un V6 approuvé avec toutes les entrées de chronologie ; chaque entrée apparaît une fois ; base et avis inchangés | non (Typst réel) |

## 5. Couverture présente vs proposée

| Domaine | Présent | Proposé | Non exécuté |
|---|---|---|---|
| Admission/permis S3 | contrôles locaux, refunds, CAS partiels | 10 oracles de consume/permis/concurrence | tous les tests PG de course |
| Preuves CTI advisor | permissif au fake transport, refus séparés | matrice croisée + S2 + PDF final | provider live |
| Audit réel | projection pure + writer | export depuis PG jetable | export PG |
| Pagination | primitives + premiers pages PG | chaîne multi-page PG + message-index | chaîne PG |
| Publication | projection + V6 JSON + Typst synthétique | complétude canonique → PDF final | PDF réel courant |
| Frontend | non requis par cette phase | aucun lot | — |

## 6. Tests qui pourraient rester verts si la garde était retirée

- `test_virustotal_adapter.py` et `test_shodan_adapter.py` : contrats HTTP avec
  `httpx.MockTransport`, sans modèle d'autorisation.
- `test_virustotal_retrohunt_client.py` : client HTTP direct, sans permis.
- `integration/test_investigation_persistence_postgres.py::test_concurrent_operation_key_replay_creates_one_invocation` :
  prouve une ligne, pas un dispatch unique.
- Les tests positifs advisor/Bridge avec transport factice : ils prouvent
  qu'un payload autorisé atteint le fake, pas qu'un permis a été consommé.
- Les tests de projection d'audit avec `AuditSourceSnapshot` construit à la
  main : ils ne détectent pas un reader SQL qui omet ou mélange des lignes.
- Les tests de page avec repositories mémoire : ils ne détectent pas un
  ordre/seek PostgreSQL différent.
- Les tests advisor actuels (`test_investigation_advisor_tools.py::test_minimal_plan_assembles_evidence_and_parses_structured_output`,
  `test_advisor_wire.py::_parse_v2`, `test_advisor_output_reparse.py` directs)
  peuvent rester verts si le nouveau module `advisor_consultation` n'est jamais
  utilisé. Le premier lot advisor doit inclure un oracle d'adoption
  caller-level (spy/adapters) et une égalité octets préparés/archivés/transmis.
- `test_advisor_wire.py::test_advisor_wire_does_not_resolve_conflicting_manifest_aliases`
  couvre un alias ambigu, pas deux SHA pour un même `(kind,id)` ; un catalogue
  qui écrase silencieusement reste vert (`R7-ADVISOR` HIGH 1).

## 7. Métriques avec dénominateurs

| Métrique | Valeur | Dénominateur |
|---|---|---|
| Oracles de permis/consume complets | 0 | 10 |
| Cas d'intégration PostgreSQL réels dans le périmètre A | 18 | 18 identifiés (tous egress factice) |
| Oracles manquants majeurs B | 4 (S2, audit PG, pagination chaînée, PDF final) | 7 oracles demandés (F6 et reparse couverts, matrice advisor partielle) |
| Tests uniques de la suite backend exécutés dans cette phase | 375 | 3 930 définitions (9,5 %) |
| Tests PostgreSQL exécutés dans cette phase | 3 | 18 cas identifiés (16,7 %) |
| Tests Typst ciblés exécutés dans cette phase | 43 | 43 tests Typst historiques |

Ces métriques ne sont pas un score de qualité ; elles montrent l'écart entre
la couverture historique et les oracles nécessaires au chantier.

## 8. Oracles imposés par les revues M7

Les revues indépendantes ont ajouté des oracles qui doivent précéder tout
cutover. Ils sont `MISSING_ORACLE` tant qu'ils ne sont pas écrits et verts.

### 8.1 S3

| Oracle | Première assertion | PG réel |
|---|---|---|
| Course `RUNNING → NEEDS_REVIEW` | A commit RUNNING + réservation + prepared, pause ; B commit NEEDS_REVIEW ; A reprend → zéro consume/permis/egress | oui |
| Révocation vs consume | revoke-first → zéro egress ; consume-first → une seule requête autorisée ; page/retry suivant refusé | oui |
| Inversion de lock réconciliation/settlement | consume et `reconcile_invocation`/settlement concurrents → pas de deadlock, un seul ordre gagnant | oui |
| Binding owner/route | owner, investigation, tool/schema, réservation, famille, route ID et digest doivent correspondre aux lignes durables ; mismatch → refus | oui |
| Normalizer version | bump de version sur une sémantique inchangée → pas de nouveau claim par défaut sans occurrence explicite | oui |
| Direct entry sans permis | chaque entrée brute VT/Shodan/Retrohunt/acquisition/advisor/Bridge → zéro appel | fake + un PG consume |
| Perte d'acquittement de commit | commit CONSUMED non acquitté → aucun permis reconstruit, `NEEDS_REVIEW`, zéro envoi | oui |
| Réponse tardive/partielle | attachement par CAS au dispatch exact ; bytes partiels conservés ; pas de succès synthétique | oui |
| Handoff ModelGateway | consume et `ModelGateway` partagent le même protocole turn/run ; retry/correctif → consume distinct | oui |
| Settlement acquisition | réservation d'opération + ledger VT-read/hit + réemploi local : une seule charge, zéro download au réemploi | oui |

### 8.2 Advisor

| Oracle | Première assertion | PG réel |
|---|---|---|
| Conflit de refs | même `(kind,id)` + SHA différent via projection/manifeste/refs → `ref_reference_conflict` | non |
| Alias subset | alias absent/restreint/stale/mauvais kind → non résolu ; cible de review versionnée → `source_ref` exact | non |
| Octets exacts | prepared bytes = archived bytes = bytes après `ModelConversationService.add_turn` ; hash égal ; Unicode/CRLF | non |
| Matrice caller-level | external=false, DNT, RED, restriction échantillon → zéro appel S3 ; preuves CTI permises intactes | non |
| Contexte stocké | contexte manquant/incomplet → `legacy_context_unavailable` ; convertisseur legacy explicite ; zéro rechargement/Bridge | non |
| Corpus v2/v3 gelé | statuts/refus/partial/refs identiques entre live parse et reparse | non |
| Adoption du module | tests caller-level qui prouvent que `prepare`/`parse` sont réellement utilisés ; aucun dual-send/shadow parse | non |

### 8.3 Statut

Aucun de ces oracles n'est exécuté dans cette phase. Ils sont les gates des
lots L10–L14 (S3) et L20–L22 (advisor). Le premier oracle S3 est le premier
RED du slice test-only `vt.file_report` ; le premier oracle advisor est le
conflit de refs du lot L20.

## 9. Limites et UNKNOWN

- Aucun test PostgreSQL n'a été exécuté dans cette phase ; la disponibilité
  d'un PostgreSQL jetable via le profil compose `postgres-test` est notée,
  mais son exécution est réservée aux lots d'implémentation.
- Aucun test Typst réel n'a été exécuté ; le binaire fonctionnel est
  `/snap/typst/current/bin/typst` (0.15.1), le bundle `chpTypst/` est présent.
- Les noms de tests cités par M2-A/M2-B ont été vérifiés statiquement, pas
  exécutés. Les commandes exactes et résultats figurent en §2.
- Les tests frontend ne sont pas dans le périmètre de cette phase.
