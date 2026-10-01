from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.domain.edition_render import (
    EDITION_RENDER_POLICY_VERSION,
    EDITION_RENDERER,
    EDITION_RENDERER_VERSION,
    EditionRender,
)
from cti_app.domain.typst_render import (
    TypstRenderAcquisition,
    TypstRenderAcquisitionOutcome,
    TypstRenderFormat,
    TypstRenderStatus,
    typst_render_acquisition_outcome,
)


def _render(**overrides: object) -> EditionRender:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "edition_release_id": uuid4(),
        "renderer": EDITION_RENDERER,
        "renderer_version": EDITION_RENDERER_VERSION,
        "template_version": "chp-edition-v1",
        "template_sha256": "a" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "font_bundle_version": "chp-fonts-v1",
        "render_policy_version": EDITION_RENDER_POLICY_VERSION,
        "format": TypstRenderFormat.PDF,
        "input_hash": "b" * 64,
        "source_blob_id": None,
        "render_data_blob_id": None,
        "output_blob_id": None,
        "output_sha256": None,
        "output_byte_size": None,
        "status": TypstRenderStatus.RUNNING,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return EditionRender(**values)  # type: ignore[arg-type]


def test_edition_render_has_the_specified_frozen_slotted_contract() -> None:
    assert EDITION_RENDERER == "typst"
    assert EDITION_RENDERER_VERSION == "edition-v2-typst-v1"
    assert EDITION_RENDER_POLICY_VERSION == "typst-edition-v2-v1"
    assert [field.name for field in fields(EditionRender)] == [
        "id",
        "edition_release_id",
        "renderer",
        "renderer_version",
        "template_version",
        "template_sha256",
        "compiler",
        "compiler_version",
        "font_bundle_version",
        "render_policy_version",
        "format",
        "input_hash",
        "source_blob_id",
        "render_data_blob_id",
        "output_blob_id",
        "output_sha256",
        "output_byte_size",
        "status",
        "error_code",
        "error_message",
        "created_at",
        "updated_at",
    ]
    render = _render()
    assert not hasattr(render, "__dict__")
    with pytest.raises(AttributeError):
        render.status = TypstRenderStatus.FAILED  # type: ignore[misc]


def test_edition_render_validates_enums_and_output_invariants() -> None:
    assert tuple(item.value for item in TypstRenderFormat) == ("pdf",)
    assert tuple(item.value for item in TypstRenderStatus) == (
        "running",
        "succeeded",
        "failed",
    )
    with pytest.raises(ValueError, match="format"):
        _render(format="pdf")
    with pytest.raises(ValueError, match="status"):
        _render(status="running")
    with pytest.raises(ValueError, match="output metadata"):
        _render(status=TypstRenderStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="error_code"):
        _render(status=TypstRenderStatus.FAILED)
    with pytest.raises(ValueError, match="output_byte_size"):
        _render(output_byte_size=True)
    with pytest.raises(ValueError, match="template_sha256"):
        _render(template_sha256="A" * 64)
    with pytest.raises(ValueError, match="font_bundle_version"):
        _render(font_bundle_version=" \t ")


def test_edition_render_acquisition_outcomes_match_status_and_age() -> None:
    stale_before = datetime.now(UTC) - timedelta(seconds=30)
    fresh_running = _render()
    stale_running = replace(fresh_running, updated_at=stale_before - timedelta(seconds=1))
    succeeded = _render(
        status=TypstRenderStatus.SUCCEEDED,
        output_blob_id=uuid4(),
        output_sha256="c" * 64,
        output_byte_size=10,
    )
    failed = _render(status=TypstRenderStatus.FAILED, error_code="typst_compile_failed")

    assert (
        typst_render_acquisition_outcome(fresh_running, stale_running_before=stale_before)
        is TypstRenderAcquisitionOutcome.IN_PROGRESS
    )
    assert (
        typst_render_acquisition_outcome(stale_running, stale_running_before=stale_before)
        is TypstRenderAcquisitionOutcome.ACQUIRED
    )
    assert (
        typst_render_acquisition_outcome(succeeded, stale_running_before=stale_before)
        is TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED
    )
    assert (
        typst_render_acquisition_outcome(failed, stale_running_before=stale_before)
        is TypstRenderAcquisitionOutcome.ACQUIRED
    )

    acquisition = TypstRenderAcquisition(TypstRenderAcquisitionOutcome.ACQUIRED, fresh_running)
    assert acquisition.render is fresh_running
    with pytest.raises(ValueError, match="does not match"):
        TypstRenderAcquisition(TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED, fresh_running)
