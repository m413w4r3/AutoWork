"""Executable UX coverage matrix for T-M8-32.

Every active scenario UX-01..UX-18 and UX-20 must name at least one real,
collectable test. UX-19 is abandoned by DECISIONS.md D9 (desktop-only UI) and
carries no fake test. The visual review capture is written in a parallel
worktree (T-M7-29): it is an explicit external reference that this integration
gate does not resolve yet.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoverageReference:
    path: str
    name: str
    kind: str
    external_ticket: str | None = None

    @property
    def label(self) -> str:
        return f"{self.path}::{self.name}"


@dataclass(frozen=True)
class ScenarioCoverage:
    tests: tuple[CoverageReference, ...]
    requires_backend: bool
    visual_review: CoverageReference | None
    abandoned_reason: str | None = None


VISUAL_REVIEW = CoverageReference(
    path="frontend/e2e/editorial/visual-review.spec.ts",
    name="test_visual_review_captures_desktop_and_states",
    kind="e2e",
    # Written by another agent in a parallel worktree. This exact reference is
    # the only one allowed to stay external to the current integration.
    external_ticket="T-M7-29",
)

UX_19_ABANDONED = (
    "D9 — UI desktop-only : UX-19 (mobile) est abandonné ; aucun test, critère ni capture mobile."
)


def _ref(path: str, name: str, kind: str) -> CoverageReference:
    return CoverageReference(path=path, name=name, kind=kind)


UX_COVERAGE: dict[str, ScenarioCoverage] = {
    "UX-01": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/editor-entry-points.spec.ts",
                "un paragraphe garde son contexte exact aux trois largeurs attendues",
                "e2e",
            ),
            _ref(
                "frontend/src/features/editorial-workspace/EditorialDocument.test.tsx",
                "test_click_selects_server_block_id",
                "ui",
            ),
            _ref(
                "backend/tests/test_editorial_workspace_snapshot.py",
                "test_service_resolves_revision_target_from_current_occurrence",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-02": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/proposal-accept.spec.ts",
                "compares, accepts, and refreshes the selected desktop document and impact state",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_editorial_pdf_postgres.py",
                "test_editorial_pdf_postgres_adoption_regeneration_history_and_composition_pin",
                "integration",
            ),
            _ref(
                "backend/tests/test_editorial_article_brief_replay_matrix.py",
                "test_article_brief_editorial_replay_matrix",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-03": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/ux-flow-gaps.spec.ts",
                "three Bridge turns retain the selected second variant after acceptance",
                "e2e",
            ),
            _ref(
                "frontend/src/features/editorial-workspace/EditorialConversation.test.tsx",
                "resolves the second variant reference to its proposal UUID",
                "ui",
            ),
            _ref(
                "backend/tests/test_editorial_thread_message.py",
                "test_reconciled_proposal_is_prepared_once_from_the_exact_turn_before_job_completion",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-04": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/proposal-card.spec.ts",
                "compares and rejects a persisted proposal with stubbed API data",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_proposal_preparation.py",
                "test_reject_is_terminal_and_keeps_artifact_pins",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-05": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/manual-edit.spec.ts",
                "manual edit saves canonically without Bridge and keeps failed text",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_manual_patch.py",
                "test_manual_edit_makes_zero_gateway_calls",
                "backend",
            ),
            _ref(
                "backend/tests/integration/editorial/test_manual_edit_postgres.py",
                "test_manual_edit_real_route_persists_replays_and_refuses_unsupported_values",
                "integration",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-06": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/local-drafts.spec.ts",
                "local drafts stay with their block and remain in browser memory",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_drafts_postgres.py",
                "test_draft_subject_acl_and_version_fence",
                "integration",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-07": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/ux-flow-gaps.spec.ts",
                "two identical paragraphs stay distinct selectable occurrences",
                "e2e",
            ),
            _ref(
                "frontend/src/features/editorial-workspace/EditorialDocument.test.tsx",
                "test_click_selects_server_block_id",
                "ui",
            ),
            _ref(
                "backend/tests/test_editorial_workspace_snapshot.py",
                "test_duplicate_paragraphs_resolve_to_distinct_canonical_paths",
                "backend",
            ),
            _ref(
                "backend/tests/test_editorial_workspace_snapshot.py",
                "test_legacy_bootstrap_is_stable_and_duplicate_ids_differ",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-08": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/proposal-accept.spec.ts",
                "shows a recoverable French conflict without leaving the selected thread",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_proposal_preparation.py",
                "test_changed_pins_project_proposal_stale_and_persist_before_any_decision",
                "backend",
            ),
            _ref(
                "backend/tests/integration/editorial/test_manual_edit_postgres.py",
                "test_manual_edit_concurrent_base_fence_has_one_winner",
                "integration",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-09": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/table-placement.spec.ts",
                "a table proposal is accepted after paragraph three in snapshot order",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_table_insertion_postgres.py",
                "test_manual_table_patch_accept_publishes_table_at_registered_anchor",
                "integration",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-10": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/bar-chart.spec.ts",
                "accepts a supported bar chart and renders it after its anchor in snapshot order",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_table_insertion_postgres.py",
                "test_manual_bar_chart_patch_accepts_with_registered_evidence_and_anchor",
                "integration",
            ),
            _ref(
                "backend/tests/test_editorial_bar_chart.py",
                "test_bar_point_requires_value_and_unit_in_the_same_admitted_evidence",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-11": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/bar-chart.spec.ts",
                "offers a descriptive table when the chart endpoint refuses insufficient data",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_bar_chart.py",
                "test_insufficient_data_refuses_without_invented_value",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-12": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/figure-insertion.spec.ts",
                "intègre deux figures après le paragraphe, les compare puis les accepte dans "
                "l’ordre du snapshot",  # noqa: RUF001
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_figure_insertion_postgres.py",
                "test_figure_adoption_makes_zero_gateway_calls",
                "integration",
            ),
            _ref(
                "backend/tests/test_editorial_figure_adoption.py",
                "test_multi_figure_patch_rechecks_archive_sha_mime_size_dimensions",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-13": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/figure-insertion.spec.ts",
                "explains a non-eligible figure and keeps its checkbox disabled",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_figure_authorization_postgres.py",
                "test_figure_catalog_scopes_subject_run_and_actor",
                "integration",
            ),
            _ref(
                "backend/tests/test_editorial_figure_adoption.py",
                "test_ineligible_or_pending_figure_refused",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-14": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/figure-insertion.spec.ts",
                "shows the server refusal when a stale BRIEF snapshot would add a fourth figure",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_pdf_composition_impact.py",
                "test_article_and_brief_pdf_use_explicit_kind",
                "backend",
            ),
            _ref(
                "backend/tests/test_editorial_figure_adoption.py",
                "test_brief_blocks_more_than_three_integrated_figures",
                "backend",
            ),
            _ref(
                "backend/tests/test_editorial_article_brief_replay_matrix.py",
                "test_article_brief_editorial_replay_matrix",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-15": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/message-composer.spec.ts",
                "message-only composer renders safe generating, reply and error states",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_thread_message.py",
                "test_message_uncertain_submission_requires_reconciliation_without_resubmit",
                "backend",
            ),
            _ref(
                "backend/tests/test_editorial_thread_message.py",
                "test_reconcile_reads_exact_turn_without_second_gateway_call",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-16": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/pdf-composition.spec.ts",
                "canonical adoption refreshes the PDF and keeps history and composition "
                "pins explicit",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_editorial_pdf_postgres.py",
                "test_editorial_pdf_postgres_adoption_regeneration_history_and_composition_pin",
                "integration",
            ),
            _ref(
                "backend/tests/test_editorial_pdf_composition_impact.py",
                "test_acceptance_marks_pdf_stale_then_rendered_pdf_matches_current_pins",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-17": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/pdf-composition.spec.ts",
                "canonical adoption refreshes the PDF and keeps history and composition "
                "pins explicit",
                "e2e",
            ),
            _ref(
                "backend/tests/test_editorial_pdf_composition_impact.py",
                "test_edition_composition_detects_stale_without_auto_repin",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-18": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/keyboard.spec.ts",
                "test_keyboard_select_edit_compare_decide_and_restore_focus",
                "e2e",
            ),
            _ref(
                "frontend/e2e/editorial/keyboard.spec.ts",
                "test_async_reply_does_not_steal_focus",
                "e2e",
            ),
        ),
        requires_backend=False,
        visual_review=VISUAL_REVIEW,
    ),
    "UX-19": ScenarioCoverage(
        tests=(),
        requires_backend=False,
        visual_review=None,
        abandoned_reason=UX_19_ABANDONED,
    ),
    "UX-20": ScenarioCoverage(
        tests=(
            _ref(
                "frontend/e2e/editorial/legacy-read-only.spec.ts",
                "opens a V4 publication as desktop read-only history",
                "e2e",
            ),
            _ref(
                "backend/tests/integration/editorial/test_legacy_read_only_postgres.py",
                "test_legacy_read_only_postgres_keeps_artifact_bytes_and_versions",
                "integration",
            ),
            _ref(
                "backend/tests/test_editorial_legacy_read_only.py",
                "test_explicitly_historical_v8_has_a_stable_read_only_reason",
                "backend",
            ),
            _ref(
                "backend/tests/test_editorial_legacy_read_only.py",
                "test_v4_to_v8_exact_readers_have_no_write_capabilities",
                "backend",
            ),
            _ref(
                "backend/tests/test_editorial_legacy_read_only.py",
                "test_current_v8_snapshot_does_not_rewrite_canonical_sha",
                "backend",
            ),
        ),
        requires_backend=True,
        visual_review=VISUAL_REVIEW,
    ),
}

_SKIPPED_MARKERS = frozenset({"skip", "skipif", "xfail"})
_TS_TEST_TITLE = re.compile(
    r"(?<![\w.])(?:test|it)"
    r"(?P<modifiers>(?:\.(?:only|concurrent|sequential|failing|todo|skip|each))*)"
    r"\s*\(\s*(?P<quote>['\"`])(?P<title>.*?)(?P=quote)",
    re.DOTALL,
)


def _decorator_tail(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_tail(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _python_test_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        if any(_decorator_tail(item) in _SKIPPED_MARKERS for item in node.decorator_list):
            continue
        names.add(node.name)
    return names


def _typescript_test_titles(source: str) -> set[str]:
    titles: set[str] = set()
    for match in _TS_TEST_TITLE.finditer(source):
        modifiers = match.group("modifiers")
        if any(marker in modifiers for marker in (".skip", ".todo", ".each")):
            continue
        titles.add(match.group("title"))
    return titles


def _declared_tests(reference: CoverageReference, source: str) -> set[str]:
    if reference.path.endswith(".py"):
        return _python_test_names(source)
    return _typescript_test_titles(source)


def test_editorial_ux_matrix_has_all_twenty_scenarios() -> None:
    expected = {f"UX-{number:02d}" for number in range(1, 21)}
    assert set(UX_COVERAGE) == expected
    assert VISUAL_REVIEW.external_ticket == "T-M7-29"
    repository_root = Path(__file__).resolve().parents[2]

    for scenario_id, coverage in UX_COVERAGE.items():
        if coverage.abandoned_reason is not None:
            assert scenario_id == "UX-19", f"{scenario_id} cannot be abandoned"
            assert coverage.abandoned_reason == UX_19_ABANDONED
            assert "D9" in coverage.abandoned_reason
            assert "desktop" in coverage.abandoned_reason
            assert coverage.tests == ()
            assert coverage.visual_review is None
            continue

        assert coverage.tests, f"{scenario_id} has no test references"
        assert coverage.visual_review is VISUAL_REVIEW
        assert len(set(coverage.tests)) == len(coverage.tests), (
            f"{scenario_id} repeats a test reference"
        )
        assert any(test.kind in {"e2e", "ui"} for test in coverage.tests), (
            f"{scenario_id} needs a UI or end-to-end test"
        )
        assert any(test.kind in {"e2e", "integration"} for test in coverage.tests), (
            f"{scenario_id} needs an end-to-end or integration test"
        )
        if coverage.requires_backend:
            assert any(test.kind in {"backend", "integration"} for test in coverage.tests), (
                f"{scenario_id} needs a backend or integration test"
            )

        for reference in (*coverage.tests, coverage.visual_review):
            if reference.external_ticket is not None:
                assert reference is VISUAL_REVIEW
                continue
            source_path = repository_root / reference.path
            assert source_path.is_file(), f"{scenario_id} references missing {reference.path}"
            source = source_path.read_text(encoding="utf-8")
            assert reference.name in _declared_tests(reference, source), (
                f"{scenario_id} references missing test {reference.label}"
            )
