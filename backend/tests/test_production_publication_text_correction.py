from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_text_correction import (
    PublicationTextCorrectionCommand,
    PublicationTextCorrectionError,
    PublicationTextCorrectionErrorCode,
    PublicationTextCorrectionService,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_v5_from_json,
    serialize_publication_document,
)
from tests.test_publication_document_v6 import _base_v5


class _ArtifactRepository:
    def __init__(self, items: tuple[ProductionArtifact, ...]) -> None:
        self.items = list(items)
        self.appended: list[ProductionArtifact] = []

    async def get(self, artifact_id: UUID):
        return next((item for item in self.items if item.id == artifact_id), None)

    async def get_current_for_revision(self, run_id: UUID, stage: str):
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    async def list_for_run(self, run_id: UUID):
        return tuple(item for item in self.items if item.production_run_id == run_id)

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)
        self.appended.append(artifact)


class _RunRepository:
    def __init__(self, run: object) -> None:
        self.run = run

    async def lock_creation_for_subject(self, _subject_id: UUID) -> None:
        return None

    async def get_current_for_subject(self, subject_id: UUID):
        return self.run if self.run.subject_id == subject_id else None


class _MemoryStore:
    def __init__(self, blobs: dict[UUID, bytes]) -> None:
        self.blobs = blobs

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        value = self.blobs[blob_id]
        if max_bytes is not None and len(value) > max_bytes:
            raise ValueError("blob exceeds requested read bound")
        return value

    async def store_stage_payloads(self, *, canonical: dict[str, object]):
        blob_id = uuid4()
        self.blobs[blob_id] = ProductionArtifactStore.canonical_json_bytes(canonical)
        return None, blob_id, None


class _Uow:
    def __init__(self, artifacts: _ArtifactRepository, runs: _RunRepository) -> None:
        self.production_artifacts = artifacts
        self.production_runs = runs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _fixture():
    document = _base_v5()
    payload = serialize_publication_document(document)
    canonical = ProductionArtifactStore.canonical_json_bytes(payload)
    blob_id = uuid4()
    run_id = uuid4()
    artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=document.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=hashlib.sha256(b"publication inputs").hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
    )
    artifacts = _ArtifactRepository((artifact,))
    store = _MemoryStore({blob_id: canonical})
    run = SimpleNamespace(id=run_id, subject_id=document.subject_id)
    runs = _RunRepository(run)

    def uow_factory():
        return _Uow(artifacts, runs)

    service = PublicationTextCorrectionService(
        uow_factory=uow_factory,  # type: ignore[arg-type]
        artifact_store=store,  # type: ignore[arg-type]
    )
    section_text = document.sections[0].paragraphs[0].text
    command = PublicationTextCorrectionCommand(
        subject_id=document.subject_id,
        artifact_id=artifact.id,
        artifact_version=artifact.version,
        artifact_input_hash=artifact.input_hash,
        artifact_sha256=hashlib.sha256(canonical).hexdigest(),
        field_path="sections[0].paragraphs[0].text",
        expected_text=section_text,
        replacement_text=f"{section_text} Correction éditoriale.",
        actor_id="human:editor-1",
        reason="Correction exacte demandée par l’éditeur.",
        idempotency_key="publication-text-correction-1",
    )
    return document, canonical, artifacts, store, service, command


def _at_path(payload: dict[str, object], path: str) -> object:
    value: object = payload
    for part in path.replace("[", ".").replace("]", "").split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]  # type: ignore[index]
    return value


def _masked_document(payload: dict[str, object], *, path: str, anchor: str) -> dict[str, object]:
    result = copy.deepcopy(payload)
    field = result
    parts = path.replace("[", ".").replace("]", "").split(".")
    for part in parts[:-1]:
        field = field[int(part)] if isinstance(field, list) else field[part]  # type: ignore[index]
    field[parts[-1]] = "<target>"  # type: ignore[index]
    rich_text = result["rich_text"]
    paragraph = next(
        item for item in rich_text["paragraphs"] if item["anchor"] == anchor  # type: ignore[index]
    )
    paragraph["spans"] = "<target-spans>"
    return result


@pytest.mark.asyncio
async def test_exact_v5_field_replacement_preserves_every_other_canonical_field() -> None:
    document, before_bytes, artifacts, store, service, command = _fixture()

    result = await service.correct(command)

    assert len(artifacts.appended) == 1
    assert artifacts.appended[0].version == 2
    document_blob_id = artifacts.items[0].canonical_blob_id
    assert document_blob_id is not None
    assert store.blobs[document_blob_id] == before_bytes
    after_bytes = store.blobs[result.artifact.canonical_blob_id]
    before_payload = json.loads(before_bytes)
    after_payload = json.loads(after_bytes)
    anchor = "section:0:paragraph:0001"
    assert _at_path(after_payload, command.field_path) == command.replacement_text
    before_rich = next(
        item for item in before_payload["rich_text"]["paragraphs"] if item["anchor"] == anchor
    )
    after_rich = next(
        item for item in after_payload["rich_text"]["paragraphs"] if item["anchor"] == anchor
    )
    assert after_rich["anchor"] == before_rich["anchor"]
    assert after_rich["spans"] == [{"role": "text", "text": command.replacement_text}]
    masked_before = _masked_document(before_payload, path=command.field_path, anchor=anchor)
    masked_after = _masked_document(after_payload, path=command.field_path, anchor=anchor)
    assert masked_before == masked_after
    assert ProductionArtifactStore.canonical_json_bytes(masked_before) == (
        ProductionArtifactStore.canonical_json_bytes(masked_after)
    )
    corrected = publication_document_v5_from_json(after_payload)
    assert isinstance(corrected, PublicationDocumentV5)
    assert result.canonical_sha256 == hashlib.sha256(after_bytes).hexdigest()
    provenance = result.artifact.metadata["manual_publication_text_correction"]
    assert provenance["parent_artifact_id"] == str(artifacts.items[0].id)
    assert provenance["before_sha256"] == hashlib.sha256(before_bytes).hexdigest()
    assert provenance["after_sha256"] == result.canonical_sha256
    assert provenance["actor_id"] == command.actor_id
    assert provenance["reason"] == command.reason
    assert not hasattr(service, "model_gateway")


@pytest.mark.asyncio
async def test_exact_request_is_idempotent_and_returns_the_appended_artifact() -> None:
    _, _, artifacts, _, service, command = _fixture()

    first = await service.correct(command)
    replay = await service.correct(command)

    assert first.artifact.id == replay.artifact.id
    assert replay.replayed is True
    assert len(artifacts.appended) == 1


@pytest.mark.asyncio
async def test_correction_refuses_expected_text_mismatch_and_non_text_fields() -> None:
    _, _, artifacts, _, service, command = _fixture()

    with pytest.raises(PublicationTextCorrectionError) as mismatch:
        await service.correct(
            replace(command, expected_text="not the current text")
        )
    assert mismatch.value.code is PublicationTextCorrectionErrorCode.EXPECTED_TEXT_MISMATCH

    with pytest.raises(PublicationTextCorrectionError) as non_text:
        await service.correct(
            replace(command, field_path="sections[0].paragraphs[0].finding_refs")
        )
    assert non_text.value.code is PublicationTextCorrectionErrorCode.FIELD_NOT_TEXT
    assert artifacts.appended == []


@pytest.mark.asyncio
async def test_correction_refuses_a_newer_publication_version() -> None:
    _, _, artifacts, _, service, command = _fixture()
    newer = ProductionArtifact(
        production_run_id=artifacts.items[0].production_run_id,
        subject_id=artifacts.items[0].subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash=hashlib.sha256(b"newer publication").hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=artifacts.items[0].canonical_blob_id,
    )
    artifacts.items.append(newer)

    with pytest.raises(PublicationTextCorrectionError) as stale:
        await service.correct(command)

    assert stale.value.code is PublicationTextCorrectionErrorCode.STALE_ARTIFACT
    assert artifacts.appended == []
