"""The A01-F10 acceptance matrix of ADR-0011 (M24 lot 10).

Data only. Each scenario names the real pytest node ids that prove it in this
repository, the owning lot, and the strongest proof level those nodes actually
exercise. The ADR table predates implementation: six node ids were renamed or
never written (A04, A05, A06, A07, D05, D06), so this matrix is the corrected
source of truth and ``docs/m24/acceptance-matrix.md`` records the observed run.

Proof levels, from weakest to strongest:

- ``unit``: in-memory ports and domain objects only.
- ``fake_vt`` / ``fake_bridge`` / ``fake_cli``: a real production path runs
  against a scripted provider, transport or executable; no provider is called.
- ``real_sidecar``: the real workspace-agent code (module or UDS server) runs
  on a real tmp filesystem.
- ``postgres``: the behaviour is also enforced by a real migrated PostgreSQL.
- ``real_typst``: a real Typst binary compiles the document.

A scenario lists several node ids when its one-line summary bundles several
properties; the level is the strongest level among the listed nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

ProofLevel = Literal[
    "unit",
    "postgres",
    "real_sidecar",
    "real_typst",
    "fake_cli",
    "fake_vt",
    "fake_bridge",
]

PROOF_LEVELS: Final[frozenset[str]] = frozenset(
    {
        "unit",
        "postgres",
        "real_sidecar",
        "real_typst",
        "fake_cli",
        "fake_vt",
        "fake_bridge",
    }
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """One ADR-0011 acceptance scenario and its real oracles."""

    scenario_id: str
    summary: str
    owner_lot: str
    level: ProofLevel
    tests: tuple[str, ...]


SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario(
        "A01",
        "création idempotente, manifeste jamais réécrit",
        "02",
        "real_sidecar",
        (
            "tests/workspace_agent/test_ensure.py::"
            "test_ensure_is_idempotent_and_never_rewrites_the_manifest",
        ),
    ),
    Scenario(
        "A02",
        "refresh recrée les dossiers sans perdre le contenu humain",
        "02",
        "real_sidecar",
        (
            "tests/workspace_agent/test_ensure.py::"
            "test_refresh_recreates_missing_directories_and_keeps_human_content",
        ),
    ),
    Scenario(
        "A03",
        "aucun échappement (symlink/traversal/rename concurrent)",
        "02",
        "real_sidecar",
        (
            "tests/workspace_agent/test_paths.py::"
            "test_symlink_swapped_between_stat_and_open_never_escapes",
            "tests/workspace_agent/test_paths.py::"
            "test_symlink_component_is_rejected_and_never_followed",
            "tests/workspace_agent/test_races.py::"
            "test_rename_race_neither_loses_nor_duplicates_a_revision",
        ),
    ),
    Scenario(
        "A04",
        "watcher + reconcile + restart scan, jamais de fichier mi-écrit",
        "02",
        "real_sidecar",
        (
            "tests/workspace_agent/test_scan.py::test_mid_write_file_is_not_imported_until_stable",
            "tests/workspace_agent/test_restart_scan.py::"
            "test_restart_reuses_generation_revision_ids_and_events",
            "tests/workspace_agent/test_restart_scan.py::"
            "test_modification_while_stopped_is_reported_once",
        ),
    ),
    Scenario(
        "A05",
        "révision stable par (path, sha256) ; mtime seul ne crée rien",
        "02",
        "real_sidecar",
        (
            "tests/workspace_agent/test_scan.py::test_mtime_only_change_does_not_mint_a_revision",
            "tests/workspace_agent/test_events.py::"
            "test_revision_and_event_ids_are_deterministic_pure_functions",
        ),
    ),
    Scenario(
        "A06",
        "conflit : deux versions, diff, décision conservée",
        "02",
        "real_sidecar",
        (
            "tests/workspace_agent/test_conflicts.py::"
            "test_a_cas_conflict_keeps_both_versions_and_never_touches_the_live_file",
            "tests/workspace_agent/test_conflicts.py::"
            "test_replaying_the_conflict_key_returns_the_same_conflict_and_file",
        ),
    ),
    Scenario(
        "A07",
        "agent absent : 503 fail-closed, UI bloquée, jamais de file",
        "02",
        "unit",
        (
            "tests/workspace_agent/test_workspace_agent_client.py::"
            "test_workspace_unavailable_fails_closed",
            "tests/workspace_agent/test_workspace_agent_client.py::"
            "test_missing_socket_is_unavailable_and_never_queues",
        ),
    ),
    Scenario(
        "B01",
        "synthèse et hash épinglés avant création du workspace",
        "03A",
        "fake_vt",
        ("tests/test_investigation_bootstrap.py::test_b01_synthesis_pinned_before_workspace",),
    ),
    Scenario(
        "B02",
        "inventaire IOC seeds ≠ découverts (hash, IP, domaine, URL)",
        "03A",
        "fake_vt",
        (
            "tests/test_investigation_bootstrap.py::"
            "test_b02_ioc_inventory_separates_seeds_from_discovered",
        ),
    ),
    Scenario(
        "B03",
        "échantillons en quarantaine et associations source",
        "03A",
        "fake_vt",
        (
            "tests/test_investigation_bootstrap.py::"
            "test_b03_quarantined_samples_keep_source_associations",
        ),
    ),
    Scenario(
        "B04",
        "une seule enveloppe G2 bornée soumise à l'humain",
        "03B",
        "postgres",
        (
            "tests/test_bootstrap_envelope.py::test_b04_single_envelope_requires_human_approval",
            "tests/integration/test_bootstrap_envelope_postgres.py::"
            "test_bootstrap_envelope_proposal_approve_and_batch_postgres",
        ),
    ),
    Scenario(
        "B05",
        "enveloppe approuvée : opérations prévues sans clic par hash, G2 jamais contourné",
        "03B",
        "postgres",
        (
            "tests/test_bootstrap_envelope.py::"
            "test_b05_approved_envelope_covers_planned_ops_without_per_hash",
            "tests/integration/test_bootstrap_envelope_postgres.py::"
            "test_bootstrap_envelope_proposal_approve_and_batch_postgres",
        ),
    ),
    Scenario(
        "B06",
        "rapport fichier brut+normalisé, dates, champs absents, pas de doublon",
        "03A",
        "fake_vt",
        ("tests/test_investigation_bootstrap.py::test_b06_file_report_archived_normalized_once",),
    ),
    Scenario(
        "B07",
        "comportement sandbox-scoped, PCAP seulement si route réelle",
        "03A",
        "fake_vt",
        ("tests/test_investigation_bootstrap.py::test_b07_behaviour_report_records_route_limits",),
    ),
    Scenario(
        "B08",
        "rapports IP/domaine datés, relations, curseurs, limites de couverture",
        "03A",
        "fake_vt",
        (
            "tests/test_investigation_bootstrap.py::"
            "test_b08_ip_domain_reports_keep_dates_relations_cursors",
        ),
    ),
    Scenario(
        "B09",
        "téléchargements après G2, bornés, hash vérifié, quarantaine",
        "03A",
        "fake_vt",
        (
            "tests/test_investigation_bootstrap.py::"
            "test_b09_download_is_bounded_hash_verified_and_quarantined",
        ),
    ),
    Scenario(
        "B10",
        "absence de VT ne bloque ni workspace ni snapshot",
        "03A",
        "fake_vt",
        (
            "tests/test_investigation_bootstrap.py::"
            "test_b10_no_vt_never_blocks_workspace_or_snapshot",
        ),
    ),
    Scenario(
        "C01",
        "demande de plan humaine via advisor.consult, idempotente",
        "04A2",
        "fake_bridge",
        (
            "tests/test_investigation_plan_api.py::"
            "test_human_plan_request_uses_advisor_service_and_idempotency",
        ),
    ),
    Scenario(
        "C02",
        "parseur tolérant texte/Markdown/JSON partiel, 12 champs",
        "04A2",
        "unit",
        ("tests/test_investigation_plan_parser.py::test_twelve_lead_fields_partial_parse",),
    ),
    Scenario(
        "C03",
        "sortie unparsed reste un avis, jamais une version",
        "04A2",
        "unit",
        ("tests/test_investigation_plan_parser.py::test_unparsed_plan_stays_advice",),
    ),
    Scenario(
        "C04",
        "versions de plan immuables, provenance consultation vérifiée",
        "04A1",
        "postgres",
        (
            "tests/test_investigation_plans.py::"
            "test_plan_versions_append_only_and_require_advisor_response",
            "tests/integration/test_investigation_plans_postgres.py::"
            "test_plan_versions_and_prose_authority_are_immutable_at_head",
        ),
    ),
    Scenario(
        "C05",
        "acceptation matérialise une fois ; version ultérieure ne réécrit rien",
        "04A2",
        "postgres",
        (
            "tests/test_investigation_plan_api.py::test_later_plan_version_never_rewrites_tasks",
            "tests/integration/test_investigation_plan_materialization_postgres.py::"
            "test_plan_accept_materialisation_replay_is_idempotent_postgres",
        ),
    ),
    Scenario(
        "C06",
        "registre typé, interprétation/critique consultatives sans cible",
        "04A2",
        "unit",
        (
            "tests/test_investigation_consultation_ledger.py::"
            "test_interpretation_and_critique_are_advisory_only",
        ),
    ),
    Scenario(
        "D01",
        "probe de capacités réel, aucun flag inventé",
        "05B1",
        "unit",
        (
            "tests/test_agentic_operator_port_contract.py::"
            "test_capability_probe_reports_only_probed_modes",
        ),
    ),
    Scenario(
        "D02",
        "démarrage de session lié tâche+passation, une seule active",
        "05B1",
        "postgres",
        (
            "tests/test_agent_sessions.py::test_concurrent_session_start_single_active",
            "tests/integration/test_agent_sessions_postgres.py::"
            "test_agent_session_migration_and_single_active_race",
        ),
    ),
    Scenario(
        "D03",
        "acquisition du bail : fence+1, opérateur running",
        "04B2",
        "unit",
        (
            "tests/test_investigation_tasks.py::test_transition_matrix_allows_only_declared_actors",
            "tests/test_investigation_tasks.py::"
            "test_operator_lease_lifecycle_keeps_and_closes_its_session",
        ),
    ),
    Scenario(
        "D04",
        "broker : attribution task_id/agent_session_id, fence courant",
        "05B2",
        "postgres",
        (
            "tests/test_agentic_tool_broker.py::"
            "test_broker_delegates_invocation_journal_and_attributes_task_session",
            "tests/integration/test_agentic_tool_broker_postgres.py::"
            "test_concurrent_identical_signed_calls_create_one_invocation",
        ),
    ),
    Scenario(
        "D05",
        "instruction mi-session persistée si l'agent est occupé",
        "05B1",
        "unit",
        (
            "tests/test_agent_sessions.py::"
            "test_instruction_is_queued_while_agent_busy_and_acked_signed",
        ),
    ),
    Scenario(
        "D06",
        "interruption : versions conservées, effets déjà partis réconciliés",
        "05B1",
        "fake_cli",
        (
            "tests/acceptance/test_acceptance_gaps.py::"
            "test_d06_interrupt_keeps_persisted_versions_and_reconciles_sent_effects",
            "tests/workspace_agent/test_codex_profile.py::"
            "test_codex_profile_interrupt_kills_process_group",
            "tests/test_investigation_transfers.py::test_interrupted_codex_handoff_keeps_analyses",
            "tests/test_agentic_tool_broker.py::"
            "test_broker_replays_same_client_key_without_second_dispatch",
            "tests/test_agentic_tool_broker.py::"
            "test_broker_never_resubmits_ambiguous_advisor_consult",
        ),
    ),
    Scenario(
        "D07",
        "session perdue : réconciliation typée, aucune relance auto",
        "05B1",
        "unit",
        ("tests/test_agent_sessions.py::test_reconcile_outcomes_never_auto_relaunch",),
    ),
    Scenario(
        "D08",
        "transfert Codex→Claude par passation versionnée + freshness",
        "05A",
        "unit",
        (
            "tests/test_investigation_handoffs.py::test_freshness_diff_lists_changed_note_and_evidence",
        ),
    ),
    Scenario(
        "D09",
        "agent expiré : aucune écriture tâche/résultat/contribution/broker",
        "05B2",
        "unit",
        ("tests/test_agentic_tool_broker.py::test_broker_refuses_missing_or_stale_lease",),
    ),
    Scenario(
        "D10",
        "adaptateur Codex n'utilise que les modes sondés",
        "05B3",
        "fake_cli",
        (
            "tests/test_codex_adapter.py::test_adapter_uses_probed_modes_only",
            "tests/workspace_agent/test_codex_profile.py::test_codex_argv_uses_probed_modes_only",
        ),
    ),
    Scenario(
        "E01",
        "note de travail importée non vérifiée, jamais preuve",
        "07",
        "postgres",
        (
            "tests/test_work_contributions.py::test_work_contribution_kind_mapping_closed",
            "tests/test_work_contributions.py::test_host_import_never_claims_lease_authority",
            "tests/integration/test_work_contributions_postgres.py::"
            "test_work_contribution_import_chain_persists_real_rows",
        ),
    ),
    Scenario(
        "E02",
        "observation importée par EvidenceRecorder, refs même enquête",
        "07",
        "postgres",
        (
            "tests/test_work_contributions.py::"
            "test_observation_import_uses_evidence_recorder_and_same_investigation_refs",
            "tests/integration/test_work_contributions_postgres.py::"
            "test_work_contribution_import_chain_persists_real_rows",
        ),
    ),
    Scenario(
        "E03",
        "finding importé jamais automatiquement supported",
        "07",
        "postgres",
        (
            "tests/test_work_contributions.py::test_finding_import_never_auto_supported",
            "tests/integration/test_work_contributions_postgres.py::"
            "test_work_contribution_import_chain_persists_real_rows",
        ),
    ),
    Scenario(
        "E04",
        "nouvel échantillon candidat jusqu'à G3 humain",
        "07",
        "unit",
        ("tests/test_work_contributions.py::test_new_sample_stays_candidate_until_human_g3",),
    ),
    Scenario(
        "E05",
        "replay même révision = même ligne ; refus sans refs canoniques",
        "07",
        "postgres",
        (
            "tests/test_work_contributions.py::test_rejected_import_has_no_canonical_refs",
            "tests/test_work_contributions.py::"
            "test_replay_same_workspace_revision_returns_existing_row",
            "tests/integration/test_work_contributions_postgres.py::"
            "test_work_contribution_import_chain_persists_real_rows",
        ),
    ),
    Scenario(
        "E06",
        "observation consultative jamais citable en preuve",
        "08A",
        "unit",
        (
            "tests/test_operator_opinion_authority.py::"
            "test_advisor_observations_cannot_be_cited_as_evidence",
        ),
    ),
    Scenario(
        "F01",
        "proposition de note issue d'une réponse Bridge, ancre ciblée",
        "08B",
        "postgres",
        (
            "tests/test_opinion_note_proposals.py::"
            "test_proposal_requires_advisor_response_observation",
            "tests/integration/test_opinion_note_proposals_postgres.py::"
            "test_note_proposal_chain_on_postgres",
        ),
    ),
    Scenario(
        "F02",
        "acceptation explicite version/hash, humain accepteur",
        "08B",
        "postgres",
        (
            "tests/test_opinion_note_proposals.py::"
            "test_note_acceptance_uses_stored_bridge_text_and_human_decision",
            "tests/integration/test_opinion_note_proposals_postgres.py::"
            "test_note_proposal_chain_on_postgres",
        ),
    ),
    Scenario(
        "F03",
        "proposition stale ne peut écraser une version plus récente",
        "08B",
        "postgres",
        (
            "tests/test_opinion_note_proposals.py::"
            "test_note_acceptance_idempotent_and_stale_refused",
            "tests/integration/test_opinion_note_proposals_postgres.py::"
            "test_note_proposal_chain_on_postgres",
        ),
    ),
    Scenario(
        "F04",
        "acceptation de note soumise à la porte D4",
        "08B",
        "unit",
        ("tests/test_opinion_note_proposals.py::test_note_acceptance_requires_bridge_provenance",),
    ),
    Scenario(
        "F05",
        "prose non prouvée refusée en bridge_only (revise)",
        "08A",
        "unit",
        (
            "tests/test_operator_opinion_authority.py::"
            "test_bridge_only_revise_refuses_non_bridge_prose",
        ),
    ),
    Scenario(
        "F06",
        "prose Bridge modifiée refusée",
        "08A",
        "unit",
        (
            "tests/test_operator_opinion_authority.py::"
            "test_bridge_only_rejects_modified_bridge_prose",
        ),
    ),
    Scenario(
        "F07",
        "unresolved_points non vide refusé",
        "08A",
        "unit",
        (
            "tests/test_operator_opinion_authority.py::"
            "test_bridge_only_revise_refuses_unresolved_points",
        ),
    ),
    Scenario(
        "F08",
        "approbation exige la provenance Bridge",
        "08A",
        "unit",
        (
            "tests/test_operator_opinion_authority.py::"
            "test_bridge_only_approve_requires_bridge_provenance",
        ),
    ),
    Scenario(
        "F09",
        "aperçu PDF réel, références résolvables",
        "08A",
        "real_typst",
        (
            "tests/test_operator_opinion_authority.py::"
            "test_bridge_only_preview_renders_resolvable_references",
            "tests/workflow_emulator/test_w17_opinion_draft_preview_quality.py::"
            "test_w17_real_typst_draft_preview_preserves_structure_and_limits",
        ),
    ),
    Scenario(
        "F10",
        "approbation et re-pin distincts ; jamais de publication automatique",
        "08A",
        "unit",
        (
            "tests/test_operator_opinion_authority.py::test_approval_is_distinct_from_publication_and_repin",
        ),
    ),
)

SCENARIO_IDS: Final[tuple[str, ...]] = tuple(scenario.scenario_id for scenario in SCENARIOS)
