"""Fail-closed behaviour of the pinned visual-revision loader.

The loader maps expected read failures to ``proposal_base_unavailable`` and
must keep programming errors visible instead of disguising them as a 503.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from cti_app.application.editorial_visual_revision import (
    VisualRevisionContextLoader,
    VisualRevisionRejected,
)
from cti_app.application.production_artifact_store import (
    ProductionReuseStorageUnavailableError,
)

_SNAPSHOT_ID = UUID("33333333-3333-4333-8333-333333333333")
_OCCURRENCE_ID = UUID("44444444-4444-4444-8444-444444444444")


def _loader() -> VisualRevisionContextLoader:
    record = SimpleNamespace(
        snapshot_id=_SNAPSHOT_ID,
        enrichment_artifact_id=UUID("55555555-5555-4555-8555-555555555555"),
        enrichment_version=2,
        enrichment_input_hash="1" * 64,
        enrichment_canonical_sha256="2" * 64,
        subject_id=UUID("66666666-6666-4666-8666-666666666666"),
        production_run_id=UUID("77777777-7777-4777-8777-777777777777"),
    )
    resolution = SimpleNamespace(
        record=record,
        entry=SimpleNamespace(block_id=_OCCURRENCE_ID),
        binding_state="valid",
        read_only_reason=None,
    )

    async def resolve_occurrence(_subject_id: UUID, _occurrence_id: UUID) -> Any:
        return resolution

    loader = VisualRevisionContextLoader(
        uow_factory=cast(Any, object()),
        artifact_store=cast(Any, object()),
        workspace_service=SimpleNamespace(resolve_occurrence=resolve_occurrence),
        enrichment_loader=cast(Any, object()),
        enrichment_revision_service=cast(Any, object()),
    )
    return loader


@pytest.mark.asyncio
async def test_expected_load_failure_is_a_closed_refusal_and_logs_no_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader = _loader()

    async def fail(**_kwargs: object) -> Any:
        raise ProductionReuseStorageUnavailableError("synthetic storage details")

    monkeypatch.setattr(loader, "_load_pinned", fail)
    caplog.set_level(logging.WARNING, logger="cti_app.application.editorial_visual_revision.loader")

    with pytest.raises(VisualRevisionRejected) as caught:
        await loader.load(
            subject_id=UUID(int=1),
            snapshot_id=_SNAPSHOT_ID,
            occurrence_id=_OCCURRENCE_ID,
        )

    assert caught.value.code == "proposal_base_unavailable"
    assert "visual_revision_base_unavailable code=base:load_dependency" in caplog.text
    assert "synthetic storage details" not in caplog.text


@pytest.mark.asyncio
async def test_programming_error_is_not_masked_as_a_base_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = _loader()

    async def fail(**_kwargs: object) -> Any:
        raise AttributeError("injected loader seam regression")

    monkeypatch.setattr(loader, "_load_pinned", fail)

    with pytest.raises(AttributeError, match="injected loader seam regression"):
        await loader.load(
            subject_id=UUID(int=1),
            snapshot_id=_SNAPSHOT_ID,
            occurrence_id=_OCCURRENCE_ID,
        )
