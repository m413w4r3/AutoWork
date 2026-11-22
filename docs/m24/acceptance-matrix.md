# Matrice d’acceptation A01–F10 (M24, lot 10)

Source de vérité : le dépôt au commit `44badd4e` (worktree `m24/acc-matrix`,
22 novembre 2026) et les tests réellement collectés/exécutés ci-dessous. La
table de traçabilité d’ADR-0011 nommait six oracles avant implémentation ;
A04, A05, A06, A07, D05 et D06 ont été corrigés dans l’ADR avec les noms
réels, et la présente matrice est l’agrégat exécuté (aucun test fonctionnel
propre au lot 10 en dehors de l’intégrité de collecte).

## Légende des niveaux de preuve

| Niveau | Ce qui tourne réellement |
|---|---|
| `unit` | ports/domaines en mémoire ; aucun processus, provider ni réseau |
| `fake_vt` | chemin de production Bootstrap + transport VT scripté en mémoire |
| `fake_bridge` | service advisor scripté (aucun Bridge réel) |
| `fake_cli` | superviseur/adaptateur réels avec un exécutable factice (Codex/Claude simulé) |
| `real_sidecar` | code réel du workspace-agent (modules ou serveur UDS) sur un tmp réel |
| `postgres` | la garantie est aussi imposée par un PostgreSQL migré réel |
| `real_typst` | un binaire Typst réel compile le document |

## Commandes exactes et résultats observés

- **C1 — unit + fake + sidecar (tous les nœuds de la matrice hors PG et Typst) :**
  ```sh
  cd backend && PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest \
    $(PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -c 'from tests.acceptance.matrix import SCENARIOS; print(" ".join(n for s in SCENARIOS for n in s.tests if not n.startswith("tests/integration/") and "test_w17_real_typst" not in n))') \
    -q --tb=short -p no:cacheprovider
  ```
  Observé : **69 passed** (64 node ids, dont les 6 paramètres de D07), 6,28 s.
  Les tests `tests/workspace_agent/test_sessions_*` et profils sont lancés sans
  surcharge de `TMPDIR`.
- **C2 — PostgreSQL (7 oracles, conteneur `postgres-test`) :**
  ```sh
  flock /tmp/m24-pg.lock make test-integration \
    INTEGRATION_TEST_PATH="tests/integration/test_bootstrap_envelope_postgres.py::test_bootstrap_envelope_proposal_approve_and_batch_postgres tests/integration/test_investigation_plans_postgres.py::test_plan_versions_and_prose_authority_are_immutable_at_head tests/integration/test_investigation_plan_materialization_postgres.py::test_plan_accept_materialisation_replay_is_idempotent_postgres tests/integration/test_agent_sessions_postgres.py::test_agent_session_migration_and_single_active_race tests/integration/test_agentic_tool_broker_postgres.py::test_concurrent_identical_signed_calls_create_one_invocation tests/integration/test_work_contributions_postgres.py::test_work_contribution_import_chain_persists_real_rows tests/integration/test_opinion_note_proposals_postgres.py::test_note_proposal_chain_on_postgres" \
    INTEGRATION_PYTEST_ARGS="-q --tb=short"
  ```
  Observé : **7 passed**, 19,29 s.
- **C3 — Typst réel (aperçu F09) :**
  ```sh
  cd backend && export TYPST_BINARY=$HOME/.local/share/autowork-tools/typst \
    FONT_BUNDLE_ROOT=/home/nill/perso/work/AutoWork/chpTypst TMPDIR=$HOME/typst-tmp && \
    PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest \
    tests/workflow_emulator/test_w17_opinion_draft_preview_quality.py::test_w17_real_typst_draft_preview_preserves_structure_and_limits \
    -q --tb=short -p no:cacheprovider
  ```
  Observé : **1 passed**, 7,41 s.
- **C4 — intégrité de la matrice (collecte seule, n’exécute pas les 49 scénarios) :**
  ```sh
  cd backend && PYTHONPATH=$PWD/src /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest \
    tests/acceptance/test_acceptance_matrix.py tests/acceptance/test_acceptance_gaps.py \
    -q -p no:cacheprovider
  ```
  Observé : **2 passed**, 17,30 s.

Aucun scénario n’est NOT-RUN. Les noms complets des node ids sont dans
`backend/tests/acceptance/matrix.py` ; les chemins ci-dessous sont abrégés
(préfixe `tests/` conservé).

## Matrice

| ID | Scénario (résumé) | Test(s) réel(s) | Niveau | Résultat |
|---|---|---|---|---|
| A01 | création idempotente, manifeste jamais réécrit | `workspace_agent/test_ensure.py::test_ensure_is_idempotent_and_never_rewrites_the_manifest` | real_sidecar | PASS — C1 |
| A02 | refresh recrée les dossiers sans perdre le contenu humain | `workspace_agent/test_ensure.py::test_refresh_recreates_missing_directories_and_keeps_human_content` | real_sidecar | PASS — C1 |
| A03 | aucun échappement (symlink/traversal/rename concurrent) | `workspace_agent/test_paths.py::test_symlink_swapped_between_stat_and_open_never_escapes`, `::test_symlink_component_is_rejected_and_never_followed`, `workspace_agent/test_races.py::test_rename_race_neither_loses_nor_duplicates_a_revision` | real_sidecar | PASS — C1 |
| A04 | watcher + reconcile + restart scan, jamais de fichier mi-écrit | `workspace_agent/test_scan.py::test_mid_write_file_is_not_imported_until_stable`, `workspace_agent/test_restart_scan.py::test_restart_reuses_generation_revision_ids_and_events`, `::test_modification_while_stopped_is_reported_once` | real_sidecar | PASS — C1 |
| A05 | révision stable par (path, sha256) ; mtime seul ne crée rien | `workspace_agent/test_scan.py::test_mtime_only_change_does_not_mint_a_revision`, `workspace_agent/test_events.py::test_revision_and_event_ids_are_deterministic_pure_functions` | real_sidecar | PASS — C1 |
| A06 | conflit : deux versions, diff, décision conservée | `workspace_agent/test_conflicts.py::test_a_cas_conflict_keeps_both_versions_and_never_touches_the_live_file`, `::test_replaying_the_conflict_key_returns_the_same_conflict_and_file` | real_sidecar | PASS — C1 |
| A07 | agent absent : 503 fail-closed, UI bloquée, jamais de file | `workspace_agent/test_workspace_agent_client.py::test_workspace_unavailable_fails_closed`, `::test_missing_socket_is_unavailable_and_never_queues` | unit | PASS — C1 |
| B01 | synthèse et hash épinglés avant création du workspace | `test_investigation_bootstrap.py::test_b01_synthesis_pinned_before_workspace` | fake_vt | PASS — C1 |
| B02 | inventaire IOC seeds ≠ découverts | `test_investigation_bootstrap.py::test_b02_ioc_inventory_separates_seeds_from_discovered` | fake_vt | PASS — C1 |
| B03 | échantillons en quarantaine et associations source | `test_investigation_bootstrap.py::test_b03_quarantined_samples_keep_source_associations` | fake_vt | PASS — C1 |
| B04 | une seule enveloppe G2 bornée soumise à l’humain | `test_bootstrap_envelope.py::test_b04_single_envelope_requires_human_approval`, `integration/test_bootstrap_envelope_postgres.py::test_bootstrap_envelope_proposal_approve_and_batch_postgres` | postgres | PASS — C1 + C2 |
| B05 | enveloppe approuvée sans clic par hash, G2 jamais contourné | `test_bootstrap_envelope.py::test_b05_approved_envelope_covers_planned_ops_without_per_hash`, `integration/test_bootstrap_envelope_postgres.py::test_bootstrap_envelope_proposal_approve_and_batch_postgres` | postgres | PASS — C1 + C2 |
| B06 | rapport fichier brut+normalisé, dates, champs absents, pas de doublon | `test_investigation_bootstrap.py::test_b06_file_report_archived_normalized_once` | fake_vt | PASS — C1 |
| B07 | comportement sandbox-scoped, PCAP seulement si route réelle | `test_investigation_bootstrap.py::test_b07_behaviour_report_records_route_limits` | fake_vt | PASS — C1 |
| B08 | rapports IP/domaine datés, relations, curseurs, couverture | `test_investigation_bootstrap.py::test_b08_ip_domain_reports_keep_dates_relations_cursors` | fake_vt | PASS — C1 |
| B09 | téléchargements après G2, bornés, hash vérifié, quarantaine | `test_investigation_bootstrap.py::test_b09_download_is_bounded_hash_verified_and_quarantined` | fake_vt | PASS — C1 |
| B10 | absence de VT ne bloque ni workspace ni snapshot | `test_investigation_bootstrap.py::test_b10_no_vt_never_blocks_workspace_or_snapshot` | fake_vt | PASS — C1 |
| C01 | demande de plan humaine via advisor.consult, idempotente | `test_investigation_plan_api.py::test_human_plan_request_uses_advisor_service_and_idempotency` | fake_bridge | PASS — C1 |
| C02 | parseur tolérant texte/Markdown/JSON partiel, 12 champs | `test_investigation_plan_parser.py::test_twelve_lead_fields_partial_parse` | unit | PASS — C1 |
| C03 | sortie `unparsed` reste un avis, jamais une version | `test_investigation_plan_parser.py::test_unparsed_plan_stays_advice` | unit | PASS — C1 |
| C04 | versions de plan immuables, provenance vérifiée | `test_investigation_plans.py::test_plan_versions_append_only_and_require_advisor_response`, `integration/test_investigation_plans_postgres.py::test_plan_versions_and_prose_authority_are_immutable_at_head` | postgres | PASS — C1 + C2 |
| C05 | acceptation matérialise une fois ; version ultérieure ne réécrit rien | `test_investigation_plan_api.py::test_later_plan_version_never_rewrites_tasks`, `integration/test_investigation_plan_materialization_postgres.py::test_plan_accept_materialisation_replay_is_idempotent_postgres` | postgres | PASS — C1 + C2 |
| C06 | registre typé, interprétation/critique consultatives sans cible | `test_investigation_consultation_ledger.py::test_interpretation_and_critique_are_advisory_only` | unit | PASS — C1 |
| D01 | probe de capacités réel, aucun flag inventé | `test_agentic_operator_port_contract.py::test_capability_probe_reports_only_probed_modes` | unit | PASS — C1 |
| D02 | démarrage de session lié tâche+passation, une seule active | `test_agent_sessions.py::test_concurrent_session_start_single_active`, `integration/test_agent_sessions_postgres.py::test_agent_session_migration_and_single_active_race` | postgres | PASS — C1 + C2 |
| D03 | acquisition du bail : fence+1, opérateur `running` | `test_investigation_tasks.py::test_transition_matrix_allows_only_declared_actors`, `::test_operator_lease_lifecycle_keeps_and_closes_its_session` | unit | PASS — C1 |
| D04 | broker : attribution `task_id`/`agent_session_id`, fence courant | `test_agentic_tool_broker.py::test_broker_delegates_invocation_journal_and_attributes_task_session`, `integration/test_agentic_tool_broker_postgres.py::test_concurrent_identical_signed_calls_create_one_invocation` | postgres | PASS — C1 + C2 |
| D05 | instruction mi-session persistée si l’agent est occupé | `test_agent_sessions.py::test_instruction_is_queued_while_agent_busy_and_acked_signed` | unit | PASS — C1 |
| D06 | interruption : versions conservées, effets déjà partis réconciliés | `acceptance/test_acceptance_gaps.py::test_d06_interrupt_keeps_persisted_versions_and_reconciles_sent_effects`, `workspace_agent/test_codex_profile.py::test_codex_profile_interrupt_kills_process_group`, `test_investigation_transfers.py::test_interrupted_codex_handoff_keeps_analyses`, `test_agentic_tool_broker.py::test_broker_replays_same_client_key_without_second_dispatch`, `::test_broker_never_resubmits_ambiguous_advisor_consult` | fake_cli | PASS — C1 |
| D07 | session perdue : réconciliation typée, aucune relance auto | `test_agent_sessions.py::test_reconcile_outcomes_never_auto_relaunch` (6 paramètres) | unit | PASS — C1 |
| D08 | transfert Codex→Claude par passation versionnée + freshness | `test_investigation_handoffs.py::test_freshness_diff_lists_changed_note_and_evidence` | unit | PASS — C1 |
| D09 | agent expiré : aucune écriture tâche/résultat/contribution/broker | `test_agentic_tool_broker.py::test_broker_refuses_missing_or_stale_lease` | unit | PASS — C1 |
| D10 | adaptateur Codex n’utilise que les modes sondés | `test_codex_adapter.py::test_adapter_uses_probed_modes_only`, `workspace_agent/test_codex_profile.py::test_codex_argv_uses_probed_modes_only` | fake_cli | PASS — C1 |
| E01 | note de travail importée non vérifiée, jamais preuve | `test_work_contributions.py::test_work_contribution_kind_mapping_closed`, `::test_host_import_never_claims_lease_authority`, `integration/test_work_contributions_postgres.py::test_work_contribution_import_chain_persists_real_rows` | postgres | PASS — C1 + C2 |
| E02 | observation importée par EvidenceRecorder, refs même enquête | `test_work_contributions.py::test_observation_import_uses_evidence_recorder_and_same_investigation_refs`, `integration/test_work_contributions_postgres.py::test_work_contribution_import_chain_persists_real_rows` | postgres | PASS — C1 + C2 |
| E03 | finding importé jamais automatiquement `supported` | `test_work_contributions.py::test_finding_import_never_auto_supported`, `integration/test_work_contributions_postgres.py::test_work_contribution_import_chain_persists_real_rows` | postgres | PASS — C1 + C2 |
| E04 | nouvel échantillon candidat jusqu’à G3 humain | `test_work_contributions.py::test_new_sample_stays_candidate_until_human_g3` | unit | PASS — C1 |
| E05 | replay même révision = même ligne ; refus sans refs canoniques | `test_work_contributions.py::test_rejected_import_has_no_canonical_refs`, `::test_replay_same_workspace_revision_returns_existing_row`, `integration/test_work_contributions_postgres.py::test_work_contribution_import_chain_persists_real_rows` | postgres | PASS — C1 + C2 |
| E06 | observation consultative jamais citable en preuve | `test_operator_opinion_authority.py::test_advisor_observations_cannot_be_cited_as_evidence` | unit | PASS — C1 |
| F01 | proposition de note issue d’une réponse Bridge, ancre ciblée | `test_opinion_note_proposals.py::test_proposal_requires_advisor_response_observation`, `integration/test_opinion_note_proposals_postgres.py::test_note_proposal_chain_on_postgres` | postgres | PASS — C1 + C2 |
| F02 | acceptation explicite version/hash, humain accepteur | `test_opinion_note_proposals.py::test_note_acceptance_uses_stored_bridge_text_and_human_decision`, `integration/test_opinion_note_proposals_postgres.py::test_note_proposal_chain_on_postgres` | postgres | PASS — C1 + C2 |
| F03 | proposition stale ne peut écraser une version plus récente | `test_opinion_note_proposals.py::test_note_acceptance_idempotent_and_stale_refused`, `integration/test_opinion_note_proposals_postgres.py::test_note_proposal_chain_on_postgres` | postgres | PASS — C1 + C2 |
| F04 | acceptation de note soumise à la porte D4 | `test_opinion_note_proposals.py::test_note_acceptance_requires_bridge_provenance` | unit | PASS — C1 |
| F05 | prose non prouvée refusée en `bridge_only` (revise) | `test_operator_opinion_authority.py::test_bridge_only_revise_refuses_non_bridge_prose` | unit | PASS — C1 |
| F06 | prose Bridge modifiée refusée | `test_operator_opinion_authority.py::test_bridge_only_rejects_modified_bridge_prose` | unit | PASS — C1 |
| F07 | `unresolved_points` non vide refusé | `test_operator_opinion_authority.py::test_bridge_only_revise_refuses_unresolved_points` | unit | PASS — C1 |
| F08 | approbation exige la provenance Bridge | `test_operator_opinion_authority.py::test_bridge_only_approve_requires_bridge_provenance` | unit | PASS — C1 |
| F09 | aperçu PDF réel, références résolvables | `test_operator_opinion_authority.py::test_bridge_only_preview_renders_resolvable_references`, `workflow_emulator/test_w17_opinion_draft_preview_quality.py::test_w17_real_typst_draft_preview_preserves_structure_and_limits` | real_typst | PASS — C1 + C3 |
| F10 | approbation et re-pin distincts ; jamais de publication automatique | `test_operator_opinion_authority.py::test_approval_is_distinct_from_publication_and_repin` | unit | PASS — C1 |

## Limites

**Implémenté et prouvé sur une intégration réelle.** Les garanties PostgreSQL
des lots 03B/04A1/04A2/05B1/05B2/07/08B sont imposées par un vrai schéma migré
(C2) : enveloppe G2 unique, immutabilité et CAS des plans, index unique de
session active, concurrence du broker, lignes/CHECK/triggers des contributions,
chaîne des propositions de note. A01–A06 font tourner le vrai code du
workspace-agent sur un système de fichiers réel (tmp) ; A06 traverse en plus un
serveur UDS réel. F09 est aussi prouvé avec un binaire Typst réel (C3).

**Simulé.** B01–B10 utilisent un transport VT scripté (`fake_vt`), jamais
l’API VirusTotal. C01 et F01–F04 utilisent un advisor/Bridge scripté
(`fake_bridge` ; la chaîne PG F01–F03 a un transport Bridge scripté, pas un
appel réseau). D01–D09 utilisent le faux `AgenticOperatorPort` du lot 05B1 ;
D06 ajoute un superviseur réel avec un exécutable Codex factice, et D10 sonde
un faux binaire Codex via le profil réel (`fake_cli`) ; aucun vrai CLI
Codex/Claude n’est lancé ici. F09 côté M24
(`test_bridge_only_preview_renders_resolvable_references`) utilise un renderer
de contrat (`_RecordingDraftRenderer`, `%PDF-1.7` factice) : la porte D4 et les
références sont réelles, le PDF ne l’est pas ; le PDF réel est prouvé
séparément par W17 (document legacy, pas une enquête `bridge_only` M24).

**Attestation locale, pas transport réel.** Les tests de sessions, de broker et
d’adaptateurs prouvent le contrat local (HMAC, bail, attribution, idempotence,
argv construits depuis les modes sondés) mais ne prouvent aucun transport réel
vers un CLI Codex/Claude, ni un probe réel dans cette exécution. Les oracles
D06/D10 de `test_codex_profile.py` utilisent un faux binaire Codex dont le
`--help` est un fixture (le test de cycle de vie complet du profil passe aussi
par ce faux binaire).

**Fournisseur réel jamais prouvé ici.** Aucun appel VT, Bridge, Shodan, LLM ou
CLI opérateur réel n’est effectué par cette matrice ; aucun PCAP, aucun
téléchargement, aucune soumission. Les scénarios « provider » sont prouvés au
niveau du contrat et des gates, pas de la connectivité.

**Couverture plus étroite que le résumé pour quelques lignes.**
B06 : le test prouve l’archive brute + normalisée une seule fois et le replay,
mais n’assert pas explicitement les dates ni les champs absents. B08 : le test
prouve relations, curseurs et limites de couverture, mais les dates ne sont
pas assertées explicitement. D03 : le test de cycle de vie prouve l’acquisition
(bail non nul), la conservation et la libération (fence +1 à la sortie) ; la
valeur littérale du fence initial n’est pas assertée par ce test. D06 : aucune
ligne de test unique du lot 05B1 ne couvre les deux moitiés du résumé ; le test
d’acceptation ajouté compose la session réelle (état/version conservés) et le
broker réel (journal unique, replay idempotent, refus après changement de
fence), et les tests de composants cités couvrent l’interruption côté hôte et
la conservation des analyses.

**Intégrité de la matrice.** `test_acceptance_matrix.py::test_a01_to_f10`
vérifie par collecte pytest en sous-processus que chacun des node ids existe,
que A01–F10 apparaît exactement une fois (A01–A07, B01–B10, C01–C06, D01–D10,
E01–E06, F01–F10) et qu’aucun scénario n’a une liste vide. Il n’exécute pas les
49 scénarios ; l’exécution est consignée ci-dessus.
