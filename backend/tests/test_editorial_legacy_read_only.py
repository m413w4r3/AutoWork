from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_legacy_read_only import publication_read_only_reason
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
    from cti_app.api import production as production_api
    from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus

    documents = _legacy_documents()
    assert [document["schema_version"] for document in documents] == ["4", "5", "6", "7", "8"]

    class _ArtifactRepository:
        def __init__(self, historical: SimpleNamespace, current: SimpleNamespace) -> None:
            self.historical = historical
            self.current = current

        async def get(self, artifact_id: UUID) -> SimpleNamespace | None:
            return self.historical if artifact_id == self.historical.id else None

        async def get_current(self, _run_id: UUID, _stage: str) -> SimpleNamespace:
            return self.current

    class _HistoryStore:
        def __init__(self, payloads: dict[UUID, bytes]) -> None:
            self.payloads = payloads

        async def read_bytes(self, blob_id: UUID) -> bytes:
            return self.payloads[blob_id]

        async def read_json(self, blob_id: UUID) -> dict[str, Any]:
            return json.loads(self.payloads[blob_id])

    class _UowContext:
        def __init__(self, uow: SimpleNamespace) -> None:
            self.uow = uow

        async def __aenter__(self) -> SimpleNamespace:
            return self.uow

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def no_routing_decision(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        production_api,
        "get_adopted_production_routing_decision",
        no_routing_decision,
    )

    for document in documents:
        parsed = parse_publication_document(document)
        assert serialize_publication_document(parsed) == document
        subject_id = uuid4()
        run_id = uuid4()
        historical_blob_id = uuid4()
        current_blob_id = uuid4()
        historical_bytes = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        current_bytes = json.dumps(
            documents[-1],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        historical = SimpleNamespace(
            id=uuid4(),
            stage=ProductionArtifactStage.PUBLICATION,
            subject_id=subject_id,
            production_run_id=run_id,
            canonical_blob_id=historical_blob_id,
            version=int(document["schema_version"]),
            input_hash="a" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            reused_from_artifact_id=None,
            metadata={},
        )
        current = SimpleNamespace(
            id=uuid4(),
            stage=ProductionArtifactStage.PUBLICATION,
            subject_id=subject_id,
            production_run_id=run_id,
            canonical_blob_id=current_blob_id,
            version=9,
            input_hash="b" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            reused_from_artifact_id=None,
            metadata={},
        )
        repository = _ArtifactRepository(historical, current)

        async def get_current_for_subject(
            _subject_id: UUID, *, expected_run_id: UUID = run_id
        ) -> SimpleNamespace:
            return SimpleNamespace(id=expected_run_id)

        uow = SimpleNamespace(
            production_runs=SimpleNamespace(get_current_for_subject=get_current_for_subject),
            production_artifacts=repository,
        )
        store = _HistoryStore(
            {historical_blob_id: historical_bytes, current_blob_id: current_bytes}
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    uow_factory=lambda current_uow=uow: _UowContext(current_uow),
                    production_artifact_store=store,
                )
            )
        )

        body = asyncio.run(
            production_api.get_publication_artifact_version(
                subject_id,
                historical.id,
                request,  # type: ignore[arg-type]
            )
        )

        assert body["read_only_reason"] in {"historical_version", "publication_kind_unknown"}
        assert "capabilities" not in body
        assert "allowed_operations" not in body
        assert body["canonical_sha256"] == hashlib.sha256(historical_bytes).hexdigest()
        assert body["canonical_content"] == document
        assert historical.version == int(document["schema_version"])
        assert store.payloads[historical_blob_id] == historical_bytes


def test_current_v8_publication_keeps_editorial_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_workspace import registry as registry_module
    from cti_app.application.editorial_workspace import service as service_module

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic current paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    document = _legacy_documents()[-1]
    publication = _make_artifact(
        "publication", document, version=8, production_run_id=synthesis.production_run_id
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    monkeypatch.setattr(service_module, "_figure_insertion_available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        registry_module,
        "table_insertion_preparation_ready",
        lambda **_kwargs: True,
    )

    snapshot = asyncio.run(service.snapshot(subject_id))

    assert snapshot["publication_kind"] == PublicationKind.BRIEF.value
    assert snapshot["capabilities"]["read_only_reason"] is None
    paragraph = next(block for block in snapshot["blocks"] if block["kind"] == "paragraph")
    assert {
        "rewrite_paragraph",
        "create_table",
        "create_source_figure",
    } <= set(paragraph["allowed_operations"])


@pytest.mark.parametrize("unsupported_stage", ("synthesis", "editorial_enrichment"))
def test_unsupported_pinned_editorial_format_remains_read_only(
    monkeypatch: pytest.MonkeyPatch,
    unsupported_stage: str,
) -> None:
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic current paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    if unsupported_stage == "synthesis":
        synthesis.content["schema_version"] = "1"
        artifact_store.write(synthesis)
    else:
        enrichment = _make_artifact(
            "editorial_enrichment",
            {
                "schema_version": 11,
                "tables": [],
                "diagrams": [],
                "charts": [],
                "source_figures": [],
            },
            production_run_id=synthesis.production_run_id,
        )
        artifacts.current["editorial_enrichment"] = enrichment
        artifact_store.write(enrichment)
    publication = _make_artifact(
        "publication",
        _legacy_documents()[-1],
        version=8,
        production_run_id=synthesis.production_run_id,
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)

    snapshot = asyncio.run(service.snapshot(subject_id))

    assert snapshot["publication_kind"] == PublicationKind.BRIEF.value
    assert snapshot["capabilities"]["read_only_reason"] == "format_not_editable"
    assert all(block["allowed_operations"] == [] for block in snapshot["blocks"])


def test_explicitly_historical_v8_has_a_stable_read_only_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _legacy_documents()[-1]
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic current paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    current_publication = _make_artifact(
        "publication", document, version=9, production_run_id=synthesis.production_run_id
    )
    historical_publication = _make_artifact(
        "publication", document, version=8, production_run_id=synthesis.production_run_id
    )
    artifacts.current["publication"] = current_publication
    artifact_store.write(current_publication)
    artifact_store.write(historical_publication)
    original_bytes = artifact_store._payloads[historical_publication.canonical_blob_id]
    original_sha256 = hashlib.sha256(original_bytes).hexdigest()

    current_snapshot = asyncio.run(service.snapshot(subject_id))
    historical_reason = publication_read_only_reason(
        document,
        PublicationKind.BRIEF,
        historical=True,
    )

    assert current_snapshot["capabilities"]["read_only_reason"] is None
    assert historical_reason is not None
    assert historical_reason.value == "historical_version"
    assert hashlib.sha256(original_bytes).hexdigest() == original_sha256
    assert historical_publication.version == 8


def test_current_v8_snapshot_does_not_rewrite_canonical_sha(
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


def test_current_unknown_kind_keeps_workspace_read_only(
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


def _read_only_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, UUID]:
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic current paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    synthesis.content["schema_version"] = "1"
    artifact_store.write(synthesis)
    publication = _make_artifact(
        "publication",
        _legacy_documents()[-1],
        version=8,
        production_run_id=synthesis.production_run_id,
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    return service, subject_id


@pytest.mark.parametrize(
    ("operation", "block_kind"),
    (
        ("rewrite_title", "title"),
        ("rewrite_paragraph", "paragraph"),
        ("create_table", "paragraph"),
        ("create_bar_chart", "paragraph"),
        ("create_source_figure", "paragraph"),
    ),
)
def test_read_only_context_refuses_every_revision_operation(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    block_kind: str,
) -> None:
    from cti_app.domain.editorial_workspace import EditorialReadOnlyError

    service, subject_id = _read_only_workspace(monkeypatch)

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["capabilities"]["read_only_reason"] == "format_not_editable"
        block = next(block for block in snapshot["blocks"] if block["kind"] == block_kind)
        with pytest.raises(EditorialReadOnlyError) as caught:
            await service.resolve_revision_target(
                subject_id=subject_id,
                snapshot_id=UUID(str(snapshot["snapshot_id"])),
                block_id=UUID(str(block["block_id"])),
                operation=operation,
            )
        assert caught.value.code == "editorial_read_only"
        assert caught.value.reason == "format_not_editable"

    asyncio.run(exercise())


def test_current_v8_context_still_resolves_editable_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_workspace import registry as registry_module
    from cti_app.application.editorial_workspace import service as service_module

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic current paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    publication = _make_artifact(
        "publication",
        _legacy_documents()[-1],
        version=8,
        production_run_id=synthesis.production_run_id,
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    monkeypatch.setattr(service_module, "_figure_insertion_available", lambda *_a, **_k: True)
    monkeypatch.setattr(
        registry_module, "table_insertion_preparation_ready", lambda **_kwargs: True
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["capabilities"]["read_only_reason"] is None
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        resolved = await service.resolve_revision_target(
            subject_id=subject_id,
            snapshot_id=UUID(str(snapshot["snapshot_id"])),
            block_id=UUID(str(title["block_id"])),
            operation="rewrite_title",
        )
        assert resolved.target.kind.value == "title"

    asyncio.run(exercise())


def test_manual_edit_prepare_refuses_read_only_context_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch
    from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation
    from tests.test_editorial_proposal_preparation import _setup

    setup = _setup()
    workspace, subject_id = _read_only_workspace(monkeypatch)
    setup.service._workspace_service = workspace
    setup.service._discussion_context_builder = None

    async def exercise() -> None:
        snapshot = await workspace.snapshot(subject_id)
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        with pytest.raises(EditorialProposalApplicationError) as caught:
            await setup.service.prepare_manual_patch(
                subject_id=subject_id,
                snapshot_id=UUID(str(snapshot["snapshot_id"])),
                occurrence_id=UUID(str(title["block_id"])),
                actor_id=uuid4(),
                idempotency_key="manual-client-key-readonly-01",
                patch=ManualPatch(
                    typed_patch=ManualTextPatch(
                        operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                        text="Synthetic replaced title",
                    )
                ),
            )
        assert caught.value.code == "editorial_read_only"
        assert caught.value.reason == "format_not_editable"
        assert setup.repository.records == []
        assert setup.blob_bytes == {}

    asyncio.run(exercise())


def test_manual_edit_checks_read_only_before_building_the_discussion_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only snapshots must answer `editorial_read_only`, not a context failure.

    The production discussion context refuses read-only blocks with a generic
    `editorial_context_unavailable`; checking it first would surface
    `proposal_base_unavailable` instead of the stable read-only code.
    """
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError
    from cti_app.application.editorial_thread_types import EditorialThreadApplicationError
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch
    from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation
    from tests.test_editorial_proposal_preparation import _setup

    setup = _setup()
    workspace, subject_id = _read_only_workspace(monkeypatch)
    setup.service._workspace_service = workspace
    context_calls: list[tuple[UUID, UUID]] = []

    class ReadOnlyContextBuilder:
        async def build_manual(self, subject_id: UUID, occurrence_id: UUID) -> object:
            context_calls.append((subject_id, occurrence_id))
            raise EditorialThreadApplicationError("editorial_context_unavailable")

    setup.service._discussion_context_builder = ReadOnlyContextBuilder()

    async def exercise() -> None:
        snapshot = await workspace.snapshot(subject_id)
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        occurrence_id = UUID(str(title["block_id"]))
        with pytest.raises(EditorialProposalApplicationError) as caught:
            await setup.service.prepare_manual_patch(
                subject_id=subject_id,
                snapshot_id=UUID(str(snapshot["snapshot_id"])),
                occurrence_id=occurrence_id,
                actor_id=uuid4(),
                idempotency_key="manual-client-key-readonly-order-01",
                patch=ManualPatch(
                    typed_patch=ManualTextPatch(
                        operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                        text="Synthetic replaced title",
                    )
                ),
            )
        assert caught.value.code == "editorial_read_only"
        assert caught.value.reason == "format_not_editable"
        assert context_calls == []
        assert setup.repository.records == []
        assert setup.blob_bytes == {}

    asyncio.run(exercise())


def test_manual_table_prepare_refuses_read_only_context_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTablePatch
    from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
    from tests.test_editorial_proposal_preparation import _setup
    from tests.test_editorial_table_insertion import _table_spec

    setup = _setup()
    workspace, subject_id = _read_only_workspace(monkeypatch)
    reference = ExtractionEvidenceRefV1(
        uuid4(),
        EvidenceKind.FACT,
        hashlib.sha256(b"synthetic read-only table evidence").hexdigest(),
    )

    async def exercise() -> None:
        snapshot = await workspace.snapshot(subject_id)
        paragraph = next(block for block in snapshot["blocks"] if block["kind"] == "paragraph")
        anchor_id = UUID(str(paragraph["block_id"]))
        table = _table_spec(anchor_id, (reference,))
        enrichment_context = {
            "anchor_block_id": str(anchor_id),
            "base_content": {"tables": []},
            "evidence_pack": SimpleNamespace(_handle_to_ref={"ev-1": reference}),
        }

        class ContextBuilder:
            async def build_manual(self, *_args: object) -> SimpleNamespace:
                return SimpleNamespace(
                    proposal_snapshot_id=UUID(str(snapshot["snapshot_id"])),
                    proposal_operations=("create_table",),
                    proposal_enrichment_context=enrichment_context,
                )

        setup.service._artifact_store = object()
        setup.service._workspace_service = workspace
        setup.service._discussion_context_builder = ContextBuilder()

        with pytest.raises(EditorialProposalApplicationError) as caught:
            await setup.service.prepare_manual_patch(
                subject_id=subject_id,
                snapshot_id=UUID(str(snapshot["snapshot_id"])),
                occurrence_id=anchor_id,
                actor_id=uuid4(),
                idempotency_key="manual-client-key-readonly-table-01",
                patch=ManualPatch(
                    typed_patch=ManualTablePatch(anchor_block_id=anchor_id, table=table)
                ),
            )
        assert caught.value.code == "editorial_read_only"
        assert caught.value.reason == "format_not_editable"
        assert setup.repository.records == []

    asyncio.run(exercise())


def test_accept_resolution_refuses_read_only_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_proposal_adoption import (
        EditorialProposalAdoptionError,
        EditorialProposalAdoptionService,
    )

    workspace, subject_id = _read_only_workspace(monkeypatch)
    adoption = EditorialProposalAdoptionService(
        uow_factory=cast(Any, None),
        blob_store=cast(Any, None),
        artifact_store=cast(Any, None),
        workspace_service=workspace,
        inputs=cast(Any, None),
        synthesis_writer=cast(Any, None),
        enrichment_writer=cast(Any, None),
    )

    async def exercise() -> None:
        snapshot = await workspace.snapshot(subject_id)
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        with pytest.raises(EditorialProposalAdoptionError) as caught:
            await adoption._resolve_current(subject_id, UUID(str(title["block_id"])))
        assert caught.value.code == "editorial_read_only"
        assert caught.value.reason == "format_not_editable"

    asyncio.run(exercise())


def test_accept_resolution_keeps_current_v8_context_editable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionService

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic current paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    publication = _make_artifact(
        "publication",
        _legacy_documents()[-1],
        version=8,
        production_run_id=synthesis.production_run_id,
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    adoption = EditorialProposalAdoptionService(
        uow_factory=cast(Any, None),
        blob_store=cast(Any, None),
        artifact_store=cast(Any, None),
        workspace_service=service,
        inputs=cast(Any, None),
        synthesis_writer=cast(Any, None),
        enrichment_writer=cast(Any, None),
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        resolution = await adoption._resolve_current(subject_id, UUID(str(title["block_id"])))
        assert resolution.read_only_reason is None
        assert resolution.entry is not None

    asyncio.run(exercise())
