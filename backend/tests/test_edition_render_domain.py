from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.domain.edition_render import (
    EDITION_RENDER_FORMAT,
    EDITION_RENDER_POLICY_VERSION,
    EDITION_RENDERER,
    EDITION_RENDERER_VERSION,
    EDITION_TEMPLATE_VERSION,
    EditionRender,
    EditionRenderAcquisition,
    EditionRenderAcquisitionOutcome,
    EditionRenderFormat,
    EditionRenderStatus,
    edition_render_acquisition_outcome,
)


def _render(**overrides: object) -> EditionRender:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "edition_release_id": uuid4(),
        "renderer": EDITION_RENDERER,
        "renderer_version": EDITION_RENDERER_VERSION,
        "template_version": EDITION_TEMPLATE_VERSION,
        "template_sha256": "a" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "font_bundle_version": "chp-fonts-v1",
        "render_policy_version": EDITION_RENDER_POLICY_VERSION,
        "format": EditionRenderFormat.PDF,
        "input_hash": "b" * 64,
        "source_blob_id": None,
        "render_data_blob_id": None,
        "output_blob_id": None,
        "output_sha256": None,
        "output_byte_size": None,
        "status": EditionRenderStatus.RUNNING,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return EditionRender(**values)  # type: ignore[arg-type]


def test_edition_render_has_the_specified_frozen_slotted_contract() -> None:
    assert EDITION_RENDER_FORMAT is EditionRenderFormat.PDF
    assert EDITION_RENDERER == "typst"
    assert EDITION_RENDERER_VERSION == "edition-v2-typst-v1"
    assert EDITION_TEMPLATE_VERSION == "chp-edition-v1"
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
        render.status = EditionRenderStatus.FAILED  # type: ignore[misc]


def test_edition_render_validates_enums_and_output_invariants() -> None:
    assert tuple(item.value for item in EditionRenderFormat) == ("pdf",)
    assert tuple(item.value for item in EditionRenderStatus) == (
        "running",
        "succeeded",
        "failed",
    )
    with pytest.raises(ValueError, match="format"):
        _render(format="pdf")
    with pytest.raises(ValueError, match="status"):
        _render(status="running")
    with pytest.raises(ValueError, match="output metadata"):
        _render(status=EditionRenderStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="error_code"):
        _render(status=EditionRenderStatus.FAILED)
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
        status=EditionRenderStatus.SUCCEEDED,
        output_blob_id=uuid4(),
        output_sha256="c" * 64,
        output_byte_size=10,
    )
    failed = _render(status=EditionRenderStatus.FAILED, error_code="typst_compile_failed")

    assert (
        edition_render_acquisition_outcome(fresh_running, stale_running_before=stale_before)
        is EditionRenderAcquisitionOutcome.IN_PROGRESS
    )
    assert (
        edition_render_acquisition_outcome(stale_running, stale_running_before=stale_before)
        is EditionRenderAcquisitionOutcome.ACQUIRED
    )
    assert (
        edition_render_acquisition_outcome(succeeded, stale_running_before=stale_before)
        is EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED
    )
    assert (
        edition_render_acquisition_outcome(failed, stale_running_before=stale_before)
        is EditionRenderAcquisitionOutcome.ACQUIRED
    )

    acquisition = EditionRenderAcquisition(EditionRenderAcquisitionOutcome.ACQUIRED, fresh_running)
    assert acquisition.render is fresh_running
    with pytest.raises(ValueError, match="does not match"):
        EditionRenderAcquisition(EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED, fresh_running)
