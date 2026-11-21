from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from cti_app.domain.publication_document import (
    PublicationDocumentV7,
    PublicationDocumentV8,
    parse_publication_document,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.test_editorial_workspace_snapshot import (
    _make_artifact,
    _paragraph,
    _synthesis,
    _workspace_service,
)
from tests.test_publication_document_v6 import _base_v5
from tests.test_publication_document_v7 import _v6


def _legacy_documents() -> list[dict[str, Any]]:
    v5 = _base_v5()
    v6 = _v6(v5)
    v7 = PublicationDocumentV7(
        base_publication=v6,
        publication_kind=PublicationKind.BRIEF,
        routing_decision_hash="d" * 64,
        routing_policy_version="legacy-read-only-routing-v1",
    )
    v8 = PublicationDocumentV8(base_publication=v7, article_format=None)
    return [
        serialize_publication_document(v5.document),
        serialize_publication_document(v5),
        serialize_publication_document(v6),
        serialize_publication_document(v7),
        serialize_publication_document(v8),
    ]


def test_v4_to_v8_exact_readers_have_no_write_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documents = _legacy_documents()
    assert [document["schema_version"] for document in documents] == ["4", "5", "6", "7", "8"]

    for document in documents:
        parsed = parse_publication_document(document)
        assert serialize_publication_document(parsed) == document

        service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
            _synthesis((_paragraph("synthetic workspace text"),)), monkeypatch
        )
        synthesis = artifacts.current["synthesis"]
        assert synthesis is not None
        publication = _make_artifact(
            "publication", document, version=4, production_run_id=synthesis.production_run_id
        )
        artifacts.current["publication"] = publication
        artifact_store.write(publication)

        snapshot = asyncio.run(service.snapshot(subject_id))
        assert snapshot["capabilities"]["read_only_reason"] == "format_not_editable"
        assert all(
            not value
            for key, value in snapshot["capabilities"].items()
            if key != "read_only_reason"
        )
        assert snapshot["blocks"]
        assert all(block["allowed_operations"] == [] for block in snapshot["blocks"])
        assert all(
            block["read_only_reason"] == "format_not_editable" for block in snapshot["blocks"]
        )


def test_historical_view_does_not_rewrite_canonical_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _legacy_documents()[-1]
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic workspace text"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    publication = _make_artifact(
        "publication", document, version=8, production_run_id=synthesis.production_run_id
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    assert publication.canonical_blob_id is not None
    original_bytes = artifact_store._payloads[publication.canonical_blob_id]
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    original_version = publication.version

    snapshot = asyncio.run(service.snapshot(subject_id))

    assert snapshot["publication_version"] == original_version
    assert (
        hashlib.sha256(artifact_store._payloads[publication.canonical_blob_id]).hexdigest()
        == original_sha256
    )
    assert artifact_store._payloads[publication.canonical_blob_id] == original_bytes
    assert publication.version == original_version


def test_kind_unknown_keeps_history_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, _repository, _artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic workspace text"),)), monkeypatch
    )

    snapshot = asyncio.run(service.snapshot(subject_id))

    assert snapshot["publication_kind"] is None
    assert snapshot["capabilities"]["read_only_reason"] == "publication_kind_unknown"
    assert all(
        not value for key, value in snapshot["capabilities"].items() if key != "read_only_reason"
    )
    assert all(block["allowed_operations"] == [] for block in snapshot["blocks"])
