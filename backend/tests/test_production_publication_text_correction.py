from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from cti_app.api.production import PublicationTextCorrectionPayload
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_qa import _semantic_annotation_has_full_occurrence_coverage
from cti_app.application.publication_text_correction import (
    MAX_PUBLICATION_TEXT_CORRECTION_TEXT_LENGTH,
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
    PublicationDocumentV6,
    PublicationDocumentV6ProvenanceV1,
    publication_document_v5_from_json,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import SemanticRole, SemanticTextSpanV1
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


def _fixture(*, augmented_current: bool = False):
    payload = serialize_publication_document(_base_v5())
    final_before = " ; la correspondance de ces valeurs reste à examiner."
    final_after = (
        " Leur correspondance précise avec chacun des composants nommés n\u2019est pas établie "
        "dans les éléments techniques décrits ; il ne faut donc pas leur attribuer "
        "arbitrairement un nom de fichier."
    )
    spans = [
        {"role": "text", "text": "Les résultats extraits par "},
        {"role": "proper_name", "text": "dmpdump"},
        {"role": "text", "text": " portent le condensat "},
        {"role": "technical_literal", "text": "SHA-256"},
        {"role": "text", "text": " et les deux empreintes "},
        {"role": "ioc", "text": "a" * 64},
        {"role": "text", "text": " ainsi que "},
        {"role": "ioc", "text": "b" * 64},
        {"role": "text", "text": final_before},
    ]
    target_text = "".join(span["text"] for span in spans)
    payload["lead"][0]["text"] = target_text
    rich_paragraph = next(
        item for item in payload["rich_text"]["paragraphs"] if item["anchor"] == "lead:0001"
    )
    rich_paragraph["spans"] = spans
    document = publication_document_v5_from_json(payload)
    payload = serialize_publication_document(document)
    canonical = ProductionArtifactStore.canonical_json_bytes(payload)
    blob_id = uuid4()
    run_id = uuid4()
    artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=run_id,
        subject_id=document.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=8 if augmented_current else 1,
        input_hash=hashlib.sha256(b"publication inputs").hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
    )
    store = _MemoryStore({blob_id: canonical})
    artifact_items = [artifact]
    if augmented_current:
        v6 = PublicationDocumentV6(
            base_publication=document,
            provenance=PublicationDocumentV6ProvenanceV1(
                base_publication_artifact_id=artifact.id,
                base_publication_sha256=publication_document_v5_sha256(document),
            ),
        )
        current_blob_id = uuid4()
        store.blobs[current_blob_id] = ProductionArtifactStore.canonical_json_bytes(
            serialize_publication_document(v6)
        )
        current = ProductionArtifact(
            id=uuid4(),
            production_run_id=run_id,
            subject_id=document.subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=9,
            input_hash=hashlib.sha256(b"augmented publication").hexdigest(),
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=current_blob_id,
            metadata={"previous_publication_artifact_id": str(artifact.id)},
        )
        artifact_items.append(current)
    artifacts = _ArtifactRepository(tuple(artifact_items))
    run = SimpleNamespace(id=run_id, subject_id=document.subject_id)
    runs = _RunRepository(run)
    qa_calls = 0

    async def qa_runner(_uow, _artifact, _publication):
        nonlocal qa_calls
        qa_calls += 1
        if qa_calls == 1:
            return {
                "passed": True,
                "checks": {"exact_projection": True, "semantic_annotation_coverage": True},
                "errors": [],
                "warnings": [],
            }
        return {
            "passed": False,
            "checks": {"exact_projection": False, "semantic_annotation_coverage": True},
            "errors": ["Publication differs from canonical Assembly projection"],
            "warnings": [],
        }

    def uow_factory():
        return _Uow(artifacts, runs)

    service = PublicationTextCorrectionService(
        uow_factory=uow_factory,  # type: ignore[arg-type]
        artifact_store=store,  # type: ignore[arg-type]
        qa_runner=qa_runner,
    )
    section_text = document.lead[0].text
    command = PublicationTextCorrectionCommand(
        subject_id=document.subject_id,
        artifact_id=artifact.id,
        artifact_version=artifact.version,
        artifact_input_hash=artifact.input_hash,
        artifact_sha256=hashlib.sha256(canonical).hexdigest(),
        field_path="lead[0].text",
        expected_text=section_text,
        replacement_text=section_text.replace(final_before, final_after),
        actor_id="human:editor-1",
        reason="Correction exacte demandée par l'éditeur.",
        idempotency_key="publication-text-correction-1",
    )
    return document, canonical, artifacts, store, service, command


@pytest.mark.asyncio
async def test_text_correction_appends_publication_without_changing_synthesis_or_enrichment() -> (
    None
):
    _document, _canonical, artifacts, store, service, command = _fixture()
    stages = (ProductionArtifactStage.SYNTHESIS, ProductionArtifactStage.EDITORIAL_ENRICHMENT)
    run_id = artifacts.items[0].production_run_id
    subject_id = artifacts.items[0].subject_id
    for stage, version, payload in (
        (ProductionArtifactStage.SYNTHESIS, 3, b'{"stage":"synthetic-synthesis"}'),
        (
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            5,
            b'{"stage":"synthetic-enrichment"}',
        ),
    ):
        blob_id = uuid4()
        store.blobs[blob_id] = payload
        artifacts.items.append(
            ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=stage,
                version=version,
                input_hash=hashlib.sha256(stage.value.encode()).hexdigest(),
                status=ProductionArtifactStatus.VERIFIED,
                canonical_blob_id=blob_id,
            )
        )
    before = {
        stage: tuple(
            (
                item.id,
                item.version,
                item.input_hash,
                hashlib.sha256(store.blobs[item.canonical_blob_id]).hexdigest(),
            )
            for item in artifacts.items
            if item.stage is stage and item.canonical_blob_id is not None
        )
        for stage in stages
    }

    result = await service.correct(command)

    assert result.artifact.stage is ProductionArtifactStage.PUBLICATION
    assert len(artifacts.appended) == 1
    assert artifacts.appended[0].id == result.artifact.id
    for stage in stages:
        after = tuple(
            (
                item.id,
                item.version,
                item.input_hash,
                hashlib.sha256(store.blobs[item.canonical_blob_id]).hexdigest(),
            )
            for item in artifacts.items
            if item.stage is stage and item.canonical_blob_id is not None
        )
        assert after == before[stage]


def _install_chain_qa_runner(
    service: PublicationTextCorrectionService, *, fail_other_chained_check: bool = False
) -> None:
    qa_calls = 0

    async def qa_runner(_uow, artifact, _publication):
        nonlocal qa_calls
        chained = bool(artifact.metadata.get("manual_publication_text_correction"))
        is_before = qa_calls % 2 == 0
        qa_calls += 1
        warnings = ["Stable publication QA warning"]
        checks = {"exact_projection": False, "semantic_annotation_coverage": True}
        errors = ["Publication differs from canonical Assembly projection"]
        if chained:
            if fail_other_chained_check and not is_before:
                checks["semantic_annotation_coverage"] = False
                errors.append("A semantic annotation check failed")
            return {
                "passed": False,
                "checks": checks,
                "errors": errors,
                "warnings": warnings,
            }
        if is_before:
            return {
                "passed": True,
                "checks": {"exact_projection": True, "semantic_annotation_coverage": True},
                "errors": [],
                "warnings": warnings,
            }
        return {"passed": False, "checks": checks, "errors": errors, "warnings": warnings}

    service._qa_runner = qa_runner


def _second_lead_command(
    command: PublicationTextCorrectionCommand,
    first: ProductionArtifact,
    first_sha256: str,
    store: _MemoryStore,
) -> PublicationTextCorrectionCommand:
    assert first.canonical_blob_id is not None
    payload = json.loads(store.blobs[first.canonical_blob_id])
    expected_text = payload["lead"][0]["text"]
    old_text = "arbitrairement un nom de fichier."
    new_text = "un nom de fichier sans vérification complémentaire."
    assert old_text in expected_text
    return replace(
        command,
        artifact_id=first.id,
        artifact_version=first.version,
        artifact_input_hash=first.input_hash,
        artifact_sha256=first_sha256,
        expected_text=expected_text,
        replacement_text=expected_text.replace(old_text, new_text, 1),
        idempotency_key="publication-text-correction-2",
    )


def _semantic_rebase_fixture():
    document, _canonical, artifacts, store, service, command = _fixture()
    payload = serialize_publication_document(document)
    hash_one = "7bdd" + "c" * 54 + "628f27"
    hash_two = "b6c4" + "d" * 56 + "0f36"
    old_final = (
        ". Ces valeurs peuvent servir à conserver une trace des échantillons hachés. "
        "Leur correspondance précise avec chacun des composants nommés n\u2019est pas établie "
        "dans les éléments techniques décrits ; il ne faut donc pas leur attribuer "
        "arbitrairement un nom de fichier."
    )
    before_spans = [
        {
            "role": "text",
            "text": "Pour l\u2019analyse statique et le rapprochement d\u2019échantillons,",
        },
        {"role": "proper_name", "text": "dmpdump"},
        {"role": "text", "text": " rapporte les empreintes "},
        {"role": "technical_literal", "text": "SHA-256"},
        {"role": "text", "text": " "},
        {"role": "text", "text": ""},
        {"role": "ioc", "text": hash_one},
        {"role": "text", "text": " et "},
        {"role": "ioc", "text": hash_two},
        {"role": "text", "text": old_final},
    ]
    before_text = "".join(span["text"] for span in before_spans)
    payload["lead"][0]["text"] = before_text
    target_paragraph = next(
        item for item in payload["rich_text"]["paragraphs"] if item["anchor"] == "lead:0001"
    )
    target_paragraph["spans"] = before_spans

    title_text = "[Example publisher] VirusTotal nvcontainer.exe document.exe Irak"
    payload["title"] = title_text
    title_paragraph = next(
        item for item in payload["rich_text"]["paragraphs"] if item["anchor"] == "title"
    )
    title_paragraph["spans"] = [
        {"role": "text", "text": "[Example publisher] "},
        {"role": "product", "text": "VirusTotal"},
        {"role": "text", "text": " "},
        {"role": "path", "text": "nvcontainer.exe"},
        {"role": "text", "text": " "},
        {"role": "path", "text": "document.exe"},
        {"role": "text", "text": " "},
        {"role": "proper_name", "text": "Irak"},
    ]

    new_final = (
        f". Ces valeurs peuvent servir à conserver une trace des échantillons hachés. "
        f"Les rapports VirusTotal associent l\u2019empreinte {hash_one} à « nvcontainer.exe » "
        f"et mentionnent « document.exe » parmi les noms de soumission de l\u2019empreinte "
        f"{hash_two} ; ces désignations rapportées ne permettent pas d\u2019établir "
        f"indépendamment que l\u2019un de ces échantillons correspond au fichier initialement "
        f"signalé depuis l\u2019Irak."
    )
    replacement_text = before_text.replace(old_final, new_final)
    replacement_spans = (
        SemanticTextSpanV1(
            SemanticRole.TEXT,
            "Pour l\u2019analyse statique et le rapprochement d\u2019échantillons,",
        ),
        SemanticTextSpanV1(SemanticRole.PROPER_NAME, "dmpdump"),
        SemanticTextSpanV1(SemanticRole.TEXT, " rapporte les empreintes "),
        SemanticTextSpanV1(SemanticRole.TECHNICAL_LITERAL, "SHA-256"),
        SemanticTextSpanV1(SemanticRole.TEXT, " "),
        SemanticTextSpanV1(SemanticRole.IOC, hash_one),
        SemanticTextSpanV1(SemanticRole.TEXT, " et "),
        SemanticTextSpanV1(SemanticRole.IOC, hash_two),
        SemanticTextSpanV1(
            SemanticRole.TEXT,
            ". Ces valeurs peuvent servir à conserver une trace des échantillons hachés. ",
        ),
        SemanticTextSpanV1(SemanticRole.TEXT, "Les rapports "),
        SemanticTextSpanV1(SemanticRole.PRODUCT, "VirusTotal"),
        SemanticTextSpanV1(SemanticRole.TEXT, " associent l\u2019empreinte "),
        SemanticTextSpanV1(SemanticRole.IOC, hash_one),
        SemanticTextSpanV1(SemanticRole.TEXT, " à « "),
        SemanticTextSpanV1(SemanticRole.PATH, "nvcontainer.exe"),
        SemanticTextSpanV1(SemanticRole.TEXT, " » et mentionnent « "),
        SemanticTextSpanV1(SemanticRole.PATH, "document.exe"),
        SemanticTextSpanV1(
            SemanticRole.TEXT,
            " » parmi les noms de soumission de l\u2019empreinte ",
        ),
        SemanticTextSpanV1(SemanticRole.IOC, hash_two),
        SemanticTextSpanV1(
            SemanticRole.TEXT,
            " ; ces désignations rapportées ne permettent pas d\u2019établir indépendamment "
            "que l\u2019un de ces échantillons correspond au fichier initialement signalé "
            "depuis l\u2019",
        ),
        SemanticTextSpanV1(SemanticRole.PROPER_NAME, "Irak"),
        SemanticTextSpanV1(SemanticRole.TEXT, "."),
    )
    document = publication_document_v5_from_json(payload)
    canonical = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(document)
    )
    artifact = artifacts.items[0]
    assert artifact.canonical_blob_id is not None
    store.blobs[artifact.canonical_blob_id] = canonical
    qa_results: list[dict[str, object]] = []

    async def qa_runner(_uow, _artifact, publication):
        coverage = _semantic_annotation_has_full_occurrence_coverage(publication)
        is_before = not qa_results
        checks = {"exact_projection": is_before, "semantic_annotation_coverage": coverage}
        errors = [] if is_before else ["Publication differs from canonical Assembly projection"]
        if not coverage:
            errors.append("A semantic annotation is missing from another exact occurrence")
        result: dict[str, object] = {
            "passed": is_before and coverage,
            "checks": checks,
            "errors": errors,
            "warnings": [],
        }
        qa_results.append(result)
        return result

    service._qa_runner = qa_runner
    command = replace(
        command,
        artifact_sha256=hashlib.sha256(canonical).hexdigest(),
        expected_text=before_text,
        replacement_text=replacement_text,
        replacement_spans=replacement_spans,
    )
    return document, canonical, artifacts, store, service, command, qa_results


def _title_fixture():
    document, _, artifacts, store, service, command = _fixture()
    base_payload = serialize_publication_document(document)
    before_title = "[Example publisher] WebSockets activity for Region X"
    title_spans = [
        {"role": "text", "text": "[Example publisher] "},
        {"role": "technical", "text": "WebSockets"},
        {"role": "text", "text": " activity for "},
        {"role": "proper_name", "text": "Region X"},
    ]
    base_payload["title"] = before_title
    title_paragraph = next(
        item for item in base_payload["rich_text"]["paragraphs"] if item["anchor"] == "title"
    )
    title_paragraph["spans"] = title_spans
    title_document = publication_document_v5_from_json(base_payload)
    canonical = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(title_document)
    )
    base_artifact = artifacts.items[0]
    assert base_artifact.canonical_blob_id is not None
    store.blobs[base_artifact.canonical_blob_id] = canonical
    replacement_spans = (
        SemanticTextSpanV1(SemanticRole.TEXT, "[Example publisher] "),
        SemanticTextSpanV1(SemanticRole.TECHNICAL, "WebSockets"),
        SemanticTextSpanV1(SemanticRole.TEXT, " campaign in "),
        SemanticTextSpanV1(SemanticRole.PROPER_NAME, "Region X"),
    )
    replacement_text = "".join(span.text for span in replacement_spans)
    command = replace(
        command,
        artifact_sha256=hashlib.sha256(canonical).hexdigest(),
        field_path="title",
        expected_text=before_title,
        replacement_text=replacement_text,
        replacement_spans=replacement_spans,
    )
    return title_document, canonical, artifacts, store, service, command


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
        item
        for item in rich_text["paragraphs"]
        if item["anchor"] == anchor  # type: ignore[index]
    )
    paragraph["spans"] = "<target-spans>"
    return result


@pytest.mark.asyncio
async def test_exact_v5_field_replacement_preserves_every_other_canonical_field() -> None:
    _document, before_bytes, artifacts, store, service, command = _fixture()

    result = await service.correct(command)

    assert len(artifacts.appended) == 1
    assert artifacts.appended[0].version == 2
    document_blob_id = artifacts.items[0].canonical_blob_id
    assert document_blob_id is not None
    assert store.blobs[document_blob_id] == before_bytes
    after_bytes = store.blobs[result.artifact.canonical_blob_id]
    before_payload = json.loads(before_bytes)
    after_payload = json.loads(after_bytes)
    anchor = "lead:0001"
    assert _at_path(after_payload, command.field_path) == command.replacement_text
    before_rich = next(
        item for item in before_payload["rich_text"]["paragraphs"] if item["anchor"] == anchor
    )
    after_rich = next(
        item for item in after_payload["rich_text"]["paragraphs"] if item["anchor"] == anchor
    )
    assert after_rich["anchor"] == before_rich["anchor"]
    assert [span["role"] for span in after_rich["spans"]] == [
        span["role"] for span in before_rich["spans"]
    ]
    assert "".join(span["text"] for span in after_rich["spans"]) == command.replacement_text
    assert after_rich["spans"][:-1] == before_rich["spans"][:-1]
    assert (
        after_rich["spans"][-1]["text"]
        == command.replacement_text[
            len(command.expected_text) - len(before_rich["spans"][-1]["text"]) :
        ]
    )
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
    assert provenance["validation"]["method"] == (
        "canonical_publication_qa_v5_with_declared_text_edit"
    )
    assert result.artifact.status is ProductionArtifactStatus.VERIFIED
    assert not hasattr(service, "model_gateway")


@pytest.mark.asyncio
async def test_title_replacement_accepts_explicit_spans_and_records_them_for_provenance() -> None:
    _document, before_bytes, artifacts, store, service, command = _title_fixture()
    before_payload = json.loads(before_bytes)

    result = await service.correct(command)
    replay = await service.correct(command)

    assert result.artifact.id == replay.artifact.id
    assert replay.replayed is True
    assert len(artifacts.appended) == 1
    assert artifacts.appended[0].version == 2
    original_blob_id = artifacts.items[0].canonical_blob_id
    assert original_blob_id is not None
    assert store.blobs[original_blob_id] == before_bytes
    after_bytes = store.blobs[result.artifact.canonical_blob_id]
    after_payload = json.loads(after_bytes)
    assert after_payload["title"] == command.replacement_text
    title_paragraph = next(
        item for item in after_payload["rich_text"]["paragraphs"] if item["anchor"] == "title"
    )
    assert title_paragraph["anchor"] == "title"
    assert title_paragraph["spans"] == [
        {"role": span.role.value, "text": span.text} for span in command.replacement_spans or ()
    ]
    assert "".join(span["text"] for span in title_paragraph["spans"]) == command.replacement_text
    masked_before = _masked_document(before_payload, path="title", anchor="title")
    masked_after = _masked_document(after_payload, path="title", anchor="title")
    assert masked_before == masked_after
    assert ProductionArtifactStore.canonical_json_bytes(masked_before) == (
        ProductionArtifactStore.canonical_json_bytes(masked_after)
    )
    corrected = publication_document_v5_from_json(after_payload)
    assert corrected.title == command.replacement_text
    provenance = result.artifact.metadata["manual_publication_text_correction"]
    before_title_paragraph = next(
        item for item in before_payload["rich_text"]["paragraphs"] if item["anchor"] == "title"
    )
    assert provenance["semantic_spans_before"] == before_title_paragraph["spans"]
    assert provenance["semantic_spans_after"] == title_paragraph["spans"]


def test_replacement_spans_api_contract_accepts_any_field_path() -> None:
    common = {
        "artifact_id": uuid4(),
        "artifact_version": 1,
        "artifact_input_hash": "a" * 64,
        "artifact_sha256": "b" * 64,
        "field_path": "title",
        "expected_text": "old",
        "replacement_text": "new title",
        "reason": "Editorial correction",
    }
    accepted = PublicationTextCorrectionPayload(
        **common,
        replacement_spans=[
            {"role": "text", "text": "new "},
            {"role": "proper_name", "text": "title"},
        ],
    )
    assert accepted.replacement_spans is not None
    assert [span.role for span in accepted.replacement_spans] == [
        SemanticRole.TEXT,
        SemanticRole.PROPER_NAME,
    ]

    non_title = PublicationTextCorrectionPayload(
        **{**common, "field_path": "sections[5].paragraphs[6].text"},
        replacement_spans=[{"role": "technical", "text": "new title"}],
    )
    assert non_title.field_path == "sections[5].paragraphs[6].text"
    with pytest.raises(ValidationError):
        PublicationTextCorrectionPayload(
            **common,
            replacement_spans=[{"role": "unknown", "text": "new title"}],
        )
    with pytest.raises(ValidationError):
        PublicationTextCorrectionPayload(
            **common,
            replacement_spans=[{"role": "text", "text": "different"}],
        )


@pytest.mark.asyncio
async def test_replacement_spans_reject_empty_and_oversized_requests() -> None:
    _, _, artifacts, _, service, command = _fixture()
    oversized_text = "x" * (MAX_PUBLICATION_TEXT_CORRECTION_TEXT_LENGTH + 1)
    invalid_commands = (
        replace(
            command,
            replacement_spans=(SemanticTextSpanV1(SemanticRole.TEXT, ""),),
        ),
        replace(
            command,
            replacement_spans=(
                SemanticTextSpanV1(SemanticRole.TEXT, command.replacement_text + " extra"),
            ),
        ),
        replace(
            command,
            field_path="title",
            replacement_text="",
            replacement_spans=(SemanticTextSpanV1(SemanticRole.TEXT, ""),),
        ),
        replace(
            command,
            field_path="title",
            replacement_text="x" * 257,
            replacement_spans=tuple(SemanticTextSpanV1(SemanticRole.TEXT, "x") for _ in range(257)),
        ),
        replace(
            command,
            field_path="title",
            replacement_text=oversized_text,
            replacement_spans=(SemanticTextSpanV1(SemanticRole.TEXT, oversized_text),),
        ),
    )

    for invalid_command in invalid_commands:
        with pytest.raises(PublicationTextCorrectionError) as invalid:
            await service.correct(invalid_command)
        assert invalid.value.code is PublicationTextCorrectionErrorCode.REPLACEMENT_SPANS_INVALID
    assert artifacts.appended == []


@pytest.mark.asyncio
async def test_replacement_spans_rebase_semantics_and_pass_occurrence_coverage() -> None:
    document, _, artifacts, store, service, command, qa_results = _semantic_rebase_fixture()
    assert _semantic_annotation_has_full_occurrence_coverage(document)

    result = await service.correct(command)

    correction = result.artifact.metadata["manual_publication_text_correction"]
    assert correction["validation"]["checks_after"]["semantic_annotation_coverage"] is True
    assert qa_results[-1]["checks"]["exact_projection"] is False
    assert qa_results[-1]["checks"]["semantic_annotation_coverage"] is True
    corrected = json.loads(store.blobs[result.artifact.canonical_blob_id])
    target = next(
        item for item in corrected["rich_text"]["paragraphs"] if item["anchor"] == "lead:0001"
    )
    assert target["spans"] == [
        {"role": span.role.value, "text": span.text} for span in command.replacement_spans or ()
    ]
    assert artifacts.appended == [result.artifact]


@pytest.mark.asyncio
async def test_replacement_spans_reject_an_unannotated_repeated_term() -> None:
    _document, _, artifacts, _, service, command, qa_results = _semantic_rebase_fixture()
    replacement_spans = list(command.replacement_spans or ())
    product_index = next(
        index for index, span in enumerate(replacement_spans) if span.role is SemanticRole.PRODUCT
    )
    product = replacement_spans[product_index]
    replacement_spans[product_index] = SemanticTextSpanV1(SemanticRole.TEXT, product.text)

    with pytest.raises(PublicationTextCorrectionError) as rejected:
        await service.correct(replace(command, replacement_spans=tuple(replacement_spans)))

    assert rejected.value.code is PublicationTextCorrectionErrorCode.PUBLICATION_QA_FAILED
    assert qa_results[-1]["checks"]["semantic_annotation_coverage"] is False
    assert artifacts.appended == []


@pytest.mark.asyncio
async def test_replacement_spans_cannot_rewrite_an_original_ioc() -> None:
    _document, _, artifacts, _, service, command, _qa_results = _semantic_rebase_fixture()
    spans = list(command.replacement_spans or ())
    original_ioc = next(
        span for span in spans if span.role is SemanticRole.IOC and span.text.startswith("7bdd")
    )
    changed_ioc = "9" * 64
    ioc_index = spans.index(original_ioc)
    spans[ioc_index] = SemanticTextSpanV1(SemanticRole.IOC, changed_ioc)
    changed_text = command.replacement_text.replace(original_ioc.text, changed_ioc, 1)

    with pytest.raises(PublicationTextCorrectionError) as rejected:
        await service.correct(
            replace(command, replacement_text=changed_text, replacement_spans=tuple(spans))
        )

    assert rejected.value.code is PublicationTextCorrectionErrorCode.SEMANTIC_SPAN_CONFLICT
    assert artifacts.appended == []


@pytest.mark.asyncio
async def test_correction_can_derive_a_new_base_from_the_current_augmented_envelope() -> None:
    _, _, artifacts, store, service, command = _fixture(augmented_current=True)

    result = await service.correct(command)

    assert result.artifact.version == 10
    correction = result.artifact.metadata["manual_publication_text_correction"]
    assert correction["parent_artifact_id"] == str(artifacts.items[0].id)
    assert correction["superseded_head_artifact_id"] == str(artifacts.items[1].id)
    corrected_bytes = store.blobs[result.artifact.canonical_blob_id]
    assert json.loads(corrected_bytes)["schema_version"] == "5"


@pytest.mark.asyncio
async def test_correction_refuses_to_change_an_ioc_span() -> None:
    _, _, artifacts, _, service, command = _fixture()
    replacement = command.expected_text.replace("a" * 64, "c" * 64, 1)

    with pytest.raises(PublicationTextCorrectionError) as conflict:
        await service.correct(replace(command, replacement_text=replacement))

    assert conflict.value.code is PublicationTextCorrectionErrorCode.SEMANTIC_SPAN_CONFLICT
    assert artifacts.appended == []


@pytest.mark.asyncio
async def test_exact_request_is_idempotent_and_returns_the_appended_artifact() -> None:
    _, _, artifacts, _, service, command = _fixture()

    first = await service.correct(command)
    replay = await service.correct(command)

    assert first.artifact.id == replay.artifact.id
    assert replay.replayed is True
    assert len(artifacts.appended) == 1


@pytest.mark.asyncio
async def test_correction_chains_from_accepted_correction_and_is_idempotent() -> None:
    _, _, artifacts, store, service, command = _fixture()
    _install_chain_qa_runner(service)

    first = await service.correct(command)
    second_command = _second_lead_command(command, first.artifact, first.canonical_sha256, store)
    second = await service.correct(second_command)
    replay = await service.correct(second_command)

    first_provenance = first.artifact.metadata["manual_publication_text_correction"]
    second_provenance = second.artifact.metadata["manual_publication_text_correction"]
    validation = second_provenance["validation"]
    assert second.artifact.version == first.artifact.version + 1
    assert second_provenance["parent_artifact_id"] == str(first.artifact.id)
    assert second_provenance["parent_correction_artifact_id"] == str(first.artifact.id)
    assert second_provenance["origin_v5_sha256"] == first_provenance["before_v5_sha256"]
    assert second_provenance["before_sha256"] == first.canonical_sha256
    assert validation["method"] == ("canonical_publication_qa_v5_with_declared_text_edit_chained")
    assert validation["checks_before"] == validation["checks_after"]
    assert validation["warnings"] == ["Stable publication QA warning"]
    assert second.artifact.id == replay.artifact.id
    assert replay.replayed is True
    assert len(artifacts.appended) == 2


@pytest.mark.asyncio
async def test_chained_correction_rejects_another_failed_qa_check() -> None:
    _, _, artifacts, store, service, command = _fixture()
    _install_chain_qa_runner(service, fail_other_chained_check=True)

    first = await service.correct(command)
    second_command = _second_lead_command(command, first.artifact, first.canonical_sha256, store)
    with pytest.raises(PublicationTextCorrectionError) as rejected:
        await service.correct(second_command)

    assert rejected.value.code is PublicationTextCorrectionErrorCode.PUBLICATION_QA_FAILED
    assert len(artifacts.appended) == 1


@pytest.mark.asyncio
async def test_non_correction_artifact_still_requires_exact_projection_before_edit() -> None:
    _, _, artifacts, _, service, command = _fixture()

    async def noncanonical_qa_runner(_uow, _artifact, _publication):
        return {
            "passed": False,
            "checks": {"exact_projection": False, "semantic_annotation_coverage": True},
            "errors": ["Publication differs from canonical Assembly projection"],
            "warnings": [],
        }

    service._qa_runner = noncanonical_qa_runner
    with pytest.raises(PublicationTextCorrectionError) as rejected:
        await service.correct(command)

    assert rejected.value.code is PublicationTextCorrectionErrorCode.PUBLICATION_QA_FAILED
    assert artifacts.appended == []


@pytest.mark.asyncio
async def test_correction_refuses_expected_text_mismatch_and_non_text_fields() -> None:
    _, _, artifacts, _, service, command = _fixture()

    with pytest.raises(PublicationTextCorrectionError) as mismatch:
        await service.correct(replace(command, expected_text="not the current text"))
    assert mismatch.value.code is PublicationTextCorrectionErrorCode.EXPECTED_TEXT_MISMATCH

    with pytest.raises(PublicationTextCorrectionError) as non_text:
        await service.correct(replace(command, field_path="lead[0].evidence_refs"))
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


def test_edit_may_replace_non_ioc_semantic_spans_inside_the_declared_region() -> None:
    from cti_app.application.publication_text_correction import _validate_semantic_span_rebase

    before = [
        {"role": "text", "text": "Pour au moins "},
        {"role": "technical_literal", "text": "64 MB"},
        {"role": "text", "text": ", transmet."},
    ]
    after = [
        {"role": "text", "text": "Selon le code, pour au moins "},
        {"role": "technical_literal", "text": "64 Mio"},
        {"role": "text", "text": ", prévoit."},
    ]
    _validate_semantic_span_rebase(
        before,
        after,
        before_text="".join(item["text"] for item in before),
        replacement_text="".join(item["text"] for item in after),
    )
    ioc_before = [
        {"role": "text", "text": "a "},
        {"role": "ioc", "text": "x" * 64},
        {"role": "text", "text": " b"},
    ]
    ioc_after = [{"role": "text", "text": "a changed b"}]
    with pytest.raises(PublicationTextCorrectionError):
        _validate_semantic_span_rebase(
            ioc_before,
            ioc_after,
            before_text="".join(item["text"] for item in ioc_before),
            replacement_text="a changed b",
        )
