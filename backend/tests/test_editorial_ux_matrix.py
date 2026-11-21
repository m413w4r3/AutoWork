"""Executable UX coverage matrix for T-M8-32.

Every active scenario UX-01..UX-18 and UX-20 must name at least one real,
collectable test. UX-19 is abandoned by DECISIONS.md D9 (desktop-only UI) and
carries no fake test. The same `path::name` references must appear in the
"Matrice de couverture UX" table of TICKETS.md, and no referenced test may be
disabled by `.only`, `.skip`, `.fixme`, `.failing`, `describe.skip`, `skip` or
`xfail`.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_TICKETS_DOCUMENT = "docs/design/atelier-editorial/TICKETS.md"
_MATRIX_HEADING = "## Matrice de couverture UX"


@dataclass(frozen=True)
class CoverageReference:
    path: str
    name: str
    kind: str

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

_PYTHON_DISABLED_MARKERS = frozenset({"skip", "skipif", "xfail"})
_TS_DISABLED_MODIFIERS = frozenset({"only", "skip", "fixme", "failing", "todo"})
_TS_TEST_TITLE = re.compile(
    r"(?<![\w.])(?:test|it)"
    r"(?P<modifiers>(?:\.(?:only|concurrent|sequential|failing|todo|skip|each))*)"
    r"\s*\(\s*(?P<quote>['\"`])(?P<title>.*?)(?P=quote)",
    re.DOTALL,
)
_TS_DESCRIBE = re.compile(
    r"(?<![\w.])(?:test\s*\.\s*)?describe\s*(?:\.\s*(?P<modifier>only|skip|fixme))?\s*\("
)
_UX_ROW = re.compile(r"^\|\s*(?P<scenario>UX-\d{2})\s*\|\s*(?P<body>.*?)\s*\|\s*$")
_MARKDOWN_REF = re.compile(r"`([^`]+::[^`]+)`")


def _marked_disabled(node: ast.expr) -> bool:
    return any(
        isinstance(child, ast.Attribute) and child.attr in _PYTHON_DISABLED_MARKERS
        for child in ast.walk(node)
    )


def _python_module_is_disabled(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            return node.value is not None and _marked_disabled(node.value)
    return False


def _python_test_sources(source: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(source)
    declared: set[str] = set()
    disabled: set[str] = set()
    module_disabled = _python_module_is_disabled(tree)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        declared.add(node.name)
        if module_disabled or any(_marked_disabled(item) for item in node.decorator_list):
            disabled.add(node.name)
    return declared, disabled


def _matching_paren(source: str, opening: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    index = opening
    while index < len(source):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(source)


def _typescript_describe_spans(source: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _TS_DESCRIBE.finditer(source):
        spans.append(
            (
                match.start(),
                _matching_paren(source, match.end() - 1),
                match.group("modifier") or "",
            )
        )
    return spans


def _typescript_test_sources(source: str) -> tuple[set[str], set[str]]:
    matches = list(_TS_TEST_TITLE.finditer(source))
    declared = {match.group("title") for match in matches}
    disabled: set[str] = set()
    focused: set[str] = set()
    for match in matches:
        modifiers = {part for part in match.group("modifiers").split(".") if part}
        if modifiers & _TS_DISABLED_MODIFIERS:
            disabled.add(match.group("title"))
        if "only" in modifiers:
            focused.add(match.group("title"))
    for start, end, modifier in _typescript_describe_spans(source):
        if modifier not in {"only", "skip", "fixme"}:
            continue
        for match in matches:
            if start <= match.start() <= end:
                if modifier == "only":
                    focused.add(match.group("title"))
                else:
                    disabled.add(match.group("title"))
    if focused:
        # A focused test silently skips every sibling declared in the same file.
        disabled.update(declared - focused)
    return declared, disabled


def _test_sources(reference: CoverageReference, source: str) -> tuple[set[str], set[str]]:
    if reference.path.endswith(".py"):
        return _python_test_sources(source)
    return _typescript_test_sources(source)


def _ux_matrix_section(repository_root: Path) -> str:
    document = (repository_root / _TICKETS_DOCUMENT).read_text(encoding="utf-8")
    try:
        start = document.index(_MATRIX_HEADING)
    except ValueError:
        raise AssertionError(f"{_TICKETS_DOCUMENT} has no {_MATRIX_HEADING!r} section") from None
    end = document.find("\n## ", start + len(_MATRIX_HEADING))
    return document[start:] if end < 0 else document[start:end]


def _documented_matrix_refs(section: str) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    for line in section.splitlines():
        row = _UX_ROW.match(line)
        if row is None:
            continue
        refs[row.group("scenario")] = set(_MARKDOWN_REF.findall(row.group("body")))
    return refs


def test_editorial_ux_matrix_has_all_twenty_scenarios() -> None:
    expected = {f"UX-{number:02d}" for number in range(1, 21)}
    assert set(UX_COVERAGE) == expected
    repository_root = Path(__file__).resolve().parents[2]
    section = _ux_matrix_section(repository_root)
    documented = _documented_matrix_refs(section)
    assert set(documented) == expected, (
        "TICKETS.md UX matrix rows differ from the executable matrix: "
        f"missing rows={sorted(expected - set(documented))}, "
        f"extra rows={sorted(set(documented) - expected)}"
    )
    assert VISUAL_REVIEW.label in section, (
        f"TICKETS.md must document the visual review capture {VISUAL_REVIEW.label}"
    )

    for scenario_id, coverage in UX_COVERAGE.items():
        if coverage.abandoned_reason is not None:
            assert scenario_id == "UX-19", f"{scenario_id} cannot be abandoned"
            assert coverage.abandoned_reason == UX_19_ABANDONED
            assert "D9" in coverage.abandoned_reason
            assert "desktop" in coverage.abandoned_reason
            assert coverage.tests == ()
            assert coverage.visual_review is None
            expected_refs: set[str] = set()
        else:
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
            expected_refs = {reference.label for reference in coverage.tests}

        assert documented[scenario_id] == expected_refs, (
            f"{scenario_id} references differ between TICKETS.md and the executable matrix: "
            f"missing from TICKETS.md={sorted(expected_refs - documented[scenario_id])}, "
            f"extra in TICKETS.md={sorted(documented[scenario_id] - expected_refs)}"
        )

        for reference in (*coverage.tests, coverage.visual_review):
            if reference is None:
                continue
            source_path = repository_root / reference.path
            assert source_path.is_file(), f"{scenario_id} references missing {reference.path}"
            source = source_path.read_text(encoding="utf-8")
            declared, disabled = _test_sources(reference, source)
            assert reference.name in declared, (
                f"{scenario_id} references missing test {reference.label}; "
                f"declared tests in {reference.path}: {sorted(declared)}"
            )
            assert reference.name not in disabled, (
                f"{scenario_id} references a disabled test {reference.label} "
                "(only/skip/fixme/failing/todo/xfail); "
                f"disabled tests in {reference.path}: {sorted(disabled)}"
            )
