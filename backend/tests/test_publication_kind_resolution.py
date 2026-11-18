from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.subject_content import (
    SubjectContentService,
    resolve_publication_kind,
)
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_routing import (
    ProductionRoutingDecisionV1,
    RoutingReasonCode,
    RoutingStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV7,
    PublicationDocumentV8,
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
from tests.test_subject_content_api import (
    _artifact,
    _document,
    _Payloads,
    _run,
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


def _content_service(
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


def test_snapshot_and_content_projection_share_publication_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _brief_v8_payload()
    workspace, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic publication paragraph"),)), monkeypatch
    )
    synthesis_artifact = artifacts.current["synthesis"]
    assert synthesis_artifact is not None
    publication_artifact = _make_artifact(
        "publication",
        payload,
        production_run_id=synthesis_artifact.production_run_id,
    )
    artifacts.current["publication"] = publication_artifact
    artifact_store.write(publication_artifact)
    content_service = _content_service(monkeypatch, payload)

    async def exercise() -> None:
        snapshot = await workspace.snapshot(subject_id)
        content = await content_service.content(subject_id)

        assert content is not None
        assert snapshot["publication_kind"] == content.publication_kind.value == "brief"

    asyncio.run(exercise())


def test_v4_legacy_kind_uses_adr_0008_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _document("Synthetic legacy brief")
    workspace, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic publication paragraph"),)), monkeypatch
    )
    synthesis_artifact = artifacts.current["synthesis"]
    assert synthesis_artifact is not None
    publication_artifact = _make_artifact(
        "publication",
        payload,
        production_run_id=synthesis_artifact.production_run_id,
    )
    artifacts.current["publication"] = publication_artifact
    artifact_store.write(publication_artifact)
    content_service = _content_service(monkeypatch, payload)

    async def exercise() -> None:
        snapshot = await workspace.snapshot(subject_id)
        content = await content_service.content(subject_id)

        assert content is not None
        assert snapshot["publication_kind"] == content.publication_kind.value == "brief"

    asyncio.run(exercise())


def _routing_decision(
    kind: PublicationKind,
    *,
    decision_hash: str = "e" * 64,
) -> ProductionRoutingDecisionV1:
    return ProductionRoutingDecisionV1(
        decision_id=uuid4(),
        production_run_id=uuid4(),
        pipeline_generation=1,
        extraction_artifact_id=None,
        extraction_artifact_hash=None,
        relevance_artifact_id=None,
        relevance_artifact_hash=None,
        input_hash="f" * 64,
        policy_version="routing-policy-v1",
        status=RoutingStatus.DECIDED,
        publication_kind=kind,
        reason_code=(
            RoutingReasonCode.SUBJECT_IOC_CONFIRMED
            if kind is PublicationKind.ARTICLE
            else RoutingReasonCode.NO_SUBJECT_IOC
        ),
        confirmed_ioc_count=0,
        contextual_ioc_count=0,
        ambiguous_ioc_count=0,
        critical_source_diagnostics=(),
        decided_at=datetime.now(UTC),
        decision_hash=decision_hash,
    )


def test_persisted_routing_decision_resolves_a_legacy_document() -> None:
    decision = _routing_decision(PublicationKind.ARTICLE)

    kind, routing_hash = resolve_publication_kind(decision, _document("Short legacy body"))

    assert kind is PublicationKind.ARTICLE
    assert routing_hash == decision.decision_hash


def test_v6_persisted_routing_decision_resolves_legacy_document() -> None:
    decision = _routing_decision(PublicationKind.ARTICLE)

    kind, routing_hash = resolve_publication_kind(decision, _v6(_base_v5()))

    assert kind is PublicationKind.ARTICLE
    assert routing_hash == decision.decision_hash


def test_v6_without_routing_decision_uses_adr_0008_legacy_resolver() -> None:
    kind, routing_hash = resolve_publication_kind(None, _v6(_base_v5()))

    assert kind is PublicationKind.BRIEF
    assert routing_hash is None


def test_conflicting_v8_and_persisted_route_resolves_to_none() -> None:
    decision = _routing_decision(PublicationKind.ARTICLE, decision_hash="d" * 64)

    kind, routing_hash = resolve_publication_kind(decision, _brief_v8_payload())

    assert kind is None
    assert routing_hash == decision.decision_hash
