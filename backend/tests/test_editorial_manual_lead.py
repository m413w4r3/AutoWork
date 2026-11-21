"""The manual text editor exposes exactly the canonical operations it saves."""

from __future__ import annotations

import pytest

from cti_app.api.editorial_workspace import EditorialManualEditRequest
from cti_app.domain.editorial_proposal import EditorialProposalError, ManualTextPatch
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation


def test_manual_text_patch_accepts_rewrite_lead() -> None:
    patch = ManualTextPatch(
        operation=TargetedEditorialRevisionOperation.REWRITE_LEAD,
        text="Synthetic edited lead.",
    )
    assert patch.operation is TargetedEditorialRevisionOperation.REWRITE_LEAD


def test_manual_text_patch_still_refuses_non_text_section_operations() -> None:
    with pytest.raises(EditorialProposalError, match="proposal_operation_unsupported"):
        ManualTextPatch(
            operation=TargetedEditorialRevisionOperation.REWRITE_SECTION,
            text="Synthetic edited section.",
        )


def test_manual_edit_request_accepts_the_three_text_operations() -> None:
    for operation in ("rewrite_title", "rewrite_lead", "rewrite_paragraph"):
        request = EditorialManualEditRequest(
            snapshot_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            operation=operation,
            text="Synthetic text.",
        )
        assert request.operation == operation


def test_manual_edit_request_refuses_a_section_rename() -> None:
    with pytest.raises(ValueError):
        EditorialManualEditRequest(
            snapshot_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
            operation="rename_section",
            text="Synthetic section title.",
        )
