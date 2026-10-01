from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.domain.publication_render import (
    PublicationRender,
    PublicationRenderFormat,
    PublicationRenderStatus,
    compute_publication_render_input_hash,
)


def _identity() -> dict[str, object]:
    return {
        "publication_artifact_id": uuid4(),
        "publication_content_sha256": "1" * 64,
        "renderer": "typst",
        "renderer_version": "publication-v4-typst-v1",
        "template_version": "chp-article-v1",
        "template_sha256": "2" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "format": PublicationRenderFormat.PDF,
        "font_bundle_version": "chp-fonts-v1",
        "render_policy_version": "typst-publication-v4-v1",
    }


def _render(**overrides: object) -> PublicationRender:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "id": uuid4(),
        "publication_artifact_id": uuid4(),
        "renderer": "typst",
        "renderer_version": "publication-v4-typst-v1",
        "template_version": "chp-article-v1",
        "template_sha256": "a" * 64,
        "compiler": "typst",
        "compiler_version": "0.15.1",
        "format": PublicationRenderFormat.PDF,
        "input_hash": "b" * 64,
        "source_blob_id": None,
        "render_data_blob_id": None,
        "output_blob_id": None,
        "output_sha256": None,
        "output_byte_size": None,
        "status": PublicationRenderStatus.RUNNING,
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return PublicationRender(**values)  # type: ignore[arg-type]


def test_publication_render_has_the_exact_frozen_slotted_contract() -> None:
    assert [field.name for field in fields(PublicationRender)] == [
        "id",
        "publication_artifact_id",
        "renderer",
        "renderer_version",
        "template_version",
        "template_sha256",
        "compiler",
        "compiler_version",
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
        render.status = PublicationRenderStatus.FAILED  # type: ignore[misc]


def test_publication_render_validates_enum_and_status_invariants() -> None:
    assert tuple(item.value for item in PublicationRenderFormat) == ("pdf",)
    assert tuple(item.value for item in PublicationRenderStatus) == (
        "running",
        "succeeded",
        "failed",
    )

    with pytest.raises(ValueError, match="format"):
        _render(format="pdf")
    with pytest.raises(ValueError, match="status"):
        _render(status="running")
    with pytest.raises(ValueError, match="output metadata"):
        _render(status=PublicationRenderStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="error_code"):
        _render(status=PublicationRenderStatus.FAILED)
    with pytest.raises(ValueError, match="template_sha256"):
        _render(template_sha256="A" * 64)


def test_publication_render_input_hash_is_deterministic_and_tracks_every_input() -> None:
    identity = _identity()
    expected = compute_publication_render_input_hash(**identity)  # type: ignore[arg-type]
    assert compute_publication_render_input_hash(**identity) == expected  # type: ignore[arg-type]

    mutations: tuple[tuple[str, object], ...] = (
        ("publication_artifact_id", uuid4()),
        ("publication_content_sha256", "3" * 64),
        ("renderer", "other-renderer"),
        ("renderer_version", "other-renderer-version"),
        ("template_version", "other-template-version"),
        ("template_sha256", "4" * 64),
        ("compiler", "other-compiler"),
        ("compiler_version", "other-compiler-version"),
        ("format", "html"),
        ("font_bundle_version", "other-font-bundle"),
        ("render_policy_version", "other-render-policy"),
    )
    for field_name, value in mutations:
        changed = {**identity, field_name: value}
        assert compute_publication_render_input_hash(**changed) != expected  # type: ignore[arg-type]


def test_render_id_and_timestamps_do_not_affect_the_render_input_hash() -> None:
    identity = _identity()
    input_hash = compute_publication_render_input_hash(**identity)  # type: ignore[arg-type]
    now = datetime.now(UTC)
    first = _render(input_hash=input_hash, created_at=now, updated_at=now)
    second = replace(
        first,
        id=uuid4(),
        created_at=now + timedelta(days=1),
        updated_at=now + timedelta(days=2),
    )

    assert first.input_hash == second.input_hash == input_hash
    assert first.id != second.id
    assert first.created_at != second.created_at
