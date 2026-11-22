"""Truthful editorial capability resolution (F1b, M23).

The snapshot capabilities are the UI contract: a true value must mean the
mounted application can really execute the action. These tests pin the pure
resolver and the block predicates before the API-level wiring tests.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cti_app.application.editorial_workspace.capabilities import (
    INSERTION_OPERATIONS,
    EditorialCapabilities,
    EditorialCapabilityFacts,
    insertion_anchor_ready,
    manual_edit_anchor_ready,
    resolve_editorial_capabilities,
)


def _wired_facts() -> EditorialCapabilityFacts:
    return EditorialCapabilityFacts(
        publication_kind_known=True,
        production_run_known=True,
        read_only=False,
        discussion_routes_wired=True,
        discussion_runtime_wired=True,
        revision_preview_wired=True,
        proposal_path_wired=True,
        manual_edit_anchor_ready=True,
        insertion_anchor_ready=True,
    )


def test_all_capabilities_are_true_only_when_every_dependency_is_available() -> None:
    assert resolve_editorial_capabilities(_wired_facts()) == EditorialCapabilities(
        can_discuss=True,
        can_preview_revision=True,
        manual_edit=True,
        can_add_enrichment=True,
    )


@pytest.mark.parametrize(
    ("missing_fact", "expected"),
    (
        ("discussion_routes_wired", "can_discuss"),
        ("discussion_runtime_wired", "can_discuss"),
        ("discussion_routes_wired", "can_preview_revision"),
        ("discussion_runtime_wired", "can_preview_revision"),
        ("revision_preview_wired", "can_preview_revision"),
        ("proposal_path_wired", "manual_edit"),
        ("manual_edit_anchor_ready", "manual_edit"),
        ("proposal_path_wired", "can_add_enrichment"),
        ("insertion_anchor_ready", "can_add_enrichment"),
    ),
)
def test_a_missing_dependency_disables_exactly_the_actions_that_need_it(
    missing_fact: str,
    expected: str,
) -> None:
    resolved = resolve_editorial_capabilities(replace(_wired_facts(), **{missing_fact: False}))

    assert getattr(resolved, expected) is False


def test_read_only_disables_every_write_capability() -> None:
    resolved = resolve_editorial_capabilities(replace(_wired_facts(), read_only=True))

    assert resolved == EditorialCapabilities(
        can_discuss=False,
        can_preview_revision=False,
        manual_edit=False,
        can_add_enrichment=False,
    )


@pytest.mark.parametrize(
    "unwritten_fact",
    ("publication_kind_known", "production_run_known"),
)
def test_an_undetermined_document_disables_every_write_capability(
    unwritten_fact: str,
) -> None:
    resolved = resolve_editorial_capabilities(replace(_wired_facts(), **{unwritten_fact: False}))

    assert not any(
        (
            resolved.can_discuss,
            resolved.can_preview_revision,
            resolved.manual_edit,
            resolved.can_add_enrichment,
        )
    )


def _block(
    kind: str,
    operations: list[str],
    *,
    text: str | None = "Synthetic paragraph.",
    before_hash: str | None = "a" * 64,
    revision_status: str = "ready",
) -> dict[str, object]:
    return {
        "kind": kind,
        "text": text,
        "before_hash": before_hash,
        "revision_status": revision_status,
        "allowed_operations": operations,
    }


def test_manual_edit_anchor_requires_an_actionable_editable_block() -> None:
    assert manual_edit_anchor_ready([_block("title", ["rewrite_title"])]) is True
    assert manual_edit_anchor_ready([_block("lead_paragraph", ["rewrite_lead"])]) is True
    assert manual_edit_anchor_ready([_block("paragraph", ["rewrite_paragraph"])]) is True
    assert manual_edit_anchor_ready([_block("paragraph", ["rewrite_section"])]) is False
    no_text = [_block("paragraph", ["rewrite_paragraph"], text=None)]
    no_before_hash = [_block("paragraph", ["rewrite_paragraph"], before_hash=None)]
    stale_revision = [_block("paragraph", ["rewrite_paragraph"], revision_status="stale")]
    assert manual_edit_anchor_ready(no_text) is False
    assert manual_edit_anchor_ready(no_before_hash) is False
    assert manual_edit_anchor_ready(stale_revision) is False


def test_insertion_anchor_requires_a_real_create_operation() -> None:
    for operation in sorted(INSERTION_OPERATIONS):
        assert insertion_anchor_ready([_block("paragraph", [operation])]) is True
    assert insertion_anchor_ready([_block("lead_paragraph", ["create_table"])]) is True
    # `insert_after` is only a legacy placement marker: the real backend never
    # emits it, and it cannot prepare any of the three insertion routes.
    assert insertion_anchor_ready([_block("paragraph", ["insert_after"])]) is False
    needs_review = [_block("paragraph", ["create_table"], revision_status="needs_review")]
    no_before_hash = [_block("paragraph", ["create_table"], before_hash=None)]
    assert insertion_anchor_ready([_block("title", ["create_table"])]) is False
    assert insertion_anchor_ready(needs_review) is False
    assert insertion_anchor_ready(no_before_hash) is False
