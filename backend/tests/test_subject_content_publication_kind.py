from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.subject_content import SubjectContentService
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Subject
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.publication_document import (
    PublicationDocumentV7,
    PublicationDocumentV8,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.test_publication_document_v6 import _base_v5
from tests.test_publication_document_v7 import _v6
from tests.test_subject_content_api import (
    SUBJECT_ID,
    _app,
    _artifact,
    _client,
    _Payloads,
    _run,
    _Uow,
)


def _brief_v8_payload() -> dict[str, object]:
    base = _base_v5()
    document = PublicationDocumentV8(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(base),
            publication_kind=PublicationKind.BRIEF,
            routing_decision_hash="d" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=None,
    )
    return serialize_publication_document(document)


def _subject_content_service(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> SubjectContentService:
    run = _run(generation=3)
    blob_id = uuid4()
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, blob_id)
    service = SubjectContentService(
        cast(UnitOfWorkFactory, object()),
        _Payloads({blob_id: payload}),
    )
    current = SimpleNamespace(run=run, artifact=artifact, routing_decision=None)

    async def current_publication(_subject_id: UUID) -> SimpleNamespace:
        return current

    monkeypatch.setattr(service, "current_publication", current_publication)
    return service


@pytest.mark.anyio
async def test_v8_brief_kind_survives_content_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _subject_content_service(monkeypatch, _brief_v8_payload())

    content = await service.content(SUBJECT_ID)

    assert content is not None
    assert content.document_version == "8"
    assert content.schema_version == "5"
    assert content.canonical_content["schema_version"] == "5"
    assert getattr(content, "publication_kind", None) is PublicationKind.BRIEF


@pytest.mark.anyio
async def test_content_api_exposes_v8_kind_next_to_unwrapped_content() -> None:
    payload = _brief_v8_payload()
    subject = Subject(
        edition_id=uuid4(),
        title="Synthetic subject",
        slug="synthetic-subject",
        tlp=TLP.AMBER,
        id=SUBJECT_ID,
    )
    run = _run(generation=3)
    blob_id = uuid4()
    artifact = _artifact(run, ProductionArtifactStage.PUBLICATION, blob_id)
    app = _app(_Uow(subject, [run], [artifact]), _Payloads({blob_id: payload}))

    async with await _client(app) as api:
        response = await api.get(f"/api/subjects/{SUBJECT_ID}/content")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["publication_kind"] == "brief"
    assert body["canonical_content"]["schema_version"] == "5"
