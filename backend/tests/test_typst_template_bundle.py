from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    compute_template_bundle_hash,
    load_template_bundle,
)

_MANIFEST_FILES = (
    "RENDERER/publication.typ",
    "RENDERER/publication_helpers.typ",
    "UTILS/document_style.typ",
    "UTILS/helpers.typ",
    "UTILS/colors.typ",
    "UTILS/header_footer.typ",
    "UTILS/chap.png",
)


@pytest.fixture
def template_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "chpTypst"
    manifest = {"template_version": "chp-article-v1", "files": list(_MANIFEST_FILES)}
    (root / "renderer-manifest.json").parent.mkdir(parents=True)
    (root / "renderer-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for index, relative_path in enumerate(_MANIFEST_FILES):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"listed-file-{index}".encode())
    for relative_path in (
        "main.typ",
        "main.pdf",
        "TEMPLATES/article.typ",
        "articles/article01/article01.typ",
        "breves/breve01/breves01.typ",
    ):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"example content")
    return root


def test_same_bundle_has_same_hash_and_uses_manifest_version(template_bundle: Path) -> None:
    first = compute_template_bundle_hash(template_bundle)
    second = compute_template_bundle_hash(template_bundle)
    digest = hashlib.sha256()
    for relative_path in sorted(_MANIFEST_FILES):
        path_bytes = relative_path.encode("utf-8")
        file_bytes = (template_bundle / relative_path).read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(file_bytes).to_bytes(8, "big"))
        digest.update(file_bytes)

    assert first[0] == "chp-article-v1"
    assert first == second
    assert first[1] == digest.hexdigest()


def test_template_bundle_is_frozen_and_keeps_the_hashed_bytes(template_bundle: Path) -> None:
    bundle = load_template_bundle(template_bundle)
    listed_file = template_bundle / _MANIFEST_FILES[0]
    original_bytes = listed_file.read_bytes()

    assert bundle.template_version == "chp-article-v1"
    assert bundle.sha256 == compute_template_bundle_hash(template_bundle)[1]
    assert next(
        file.content for file in bundle.files if file.relative_path == _MANIFEST_FILES[0]
    ) == (original_bytes)
    with pytest.raises(AttributeError):
        bundle.template_version = "changed"  # type: ignore[misc]

    listed_file.write_bytes(b"changed after snapshot")

    assert bundle.sha256 != compute_template_bundle_hash(template_bundle)[1]
    assert next(
        file.content for file in bundle.files if file.relative_path == _MANIFEST_FILES[0]
    ) == (original_bytes)


def test_manifest_listed_file_bytes_change_the_hash(template_bundle: Path) -> None:
    before = compute_template_bundle_hash(template_bundle)
    listed_file = template_bundle / _MANIFEST_FILES[0]
    listed_file.write_bytes(listed_file.read_bytes() + b" changed")

    after = compute_template_bundle_hash(template_bundle)

    assert after[0] == before[0]
    assert after[1] != before[1]


@pytest.mark.parametrize(
    "relative_path",
    (
        "main.typ",
        "main.pdf",
        "TEMPLATES/article.typ",
        "articles/article01/article01.typ",
        "breves/breve01/breves01.typ",
    ),
)
def test_files_not_listed_in_manifest_do_not_change_hash(
    template_bundle: Path, relative_path: str
) -> None:
    before = compute_template_bundle_hash(template_bundle)
    example_file = template_bundle / relative_path
    example_file.write_bytes(b"changed example content")

    after = compute_template_bundle_hash(template_bundle)

    assert after == before


def test_missing_manifest_listed_file_raises_typed_error(template_bundle: Path) -> None:
    (template_bundle / _MANIFEST_FILES[0]).unlink()

    with pytest.raises(TemplateBundleInvalidError, match="missing"):
        compute_template_bundle_hash(template_bundle)


def test_parent_traversal_manifest_path_is_rejected_before_reading_outside(
    template_bundle: Path,
) -> None:
    outside = template_bundle.parent / "outside.typ"
    outside.write_bytes(b"must not be read")
    manifest = {"template_version": "chp-article-v1", "files": ["../outside.typ"]}
    (template_bundle / "renderer-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(TemplateBundleInvalidError, match="escapes the bundle"):
        compute_template_bundle_hash(template_bundle)


@pytest.mark.parametrize("listed_path", ("/outside.typ", "C:/outside.typ", "UTILS\\helpers.typ"))
def test_absolute_and_backslash_manifest_paths_are_rejected(
    template_bundle: Path, listed_path: str
) -> None:
    manifest = {"template_version": "chp-article-v1", "files": [listed_path]}
    (template_bundle / "renderer-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(TemplateBundleInvalidError):
        load_template_bundle(template_bundle)


def test_duplicate_manifest_paths_are_rejected(template_bundle: Path) -> None:
    relative_path = _MANIFEST_FILES[0]
    manifest = {
        "template_version": "chp-article-v1",
        "files": [relative_path, relative_path],
    }
    (template_bundle / "renderer-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(TemplateBundleInvalidError, match="duplicate"):
        load_template_bundle(template_bundle)
