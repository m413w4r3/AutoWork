"""The edition rule archive: one ZIP of every detection rule the bulletin published."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from io import BytesIO
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.publication import router as publication_router
from cti_app.application.edition_rule_archive import (
    EditionRuleArchiveError,
    EditionRuleArchiveService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import PublicationManifestEntryV1, PublicationManifestV1
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_A = UUID("22222222-2222-4222-8222-222222222222")
SUBJECT_B = UUID("33333333-3333-4333-8333-333333333333")
RUN_A = UUID("44444444-4444-4444-8444-444444444444")
RUN_B = UUID("55555555-5555-4555-8555-555555555555")

YARA_BODY = 'rule Marki { strings: $a = "marki" condition: $a }'
SIGMA_BODY = "title: Raw disk access\ndetection:\n  condition: selection\n"
SURICATA_BODY = 'alert tcp any any -> any 80 (msg:"beacon"; sid:1;)'


def _edition() -> Edition:
    return Edition(
        id=EDITION_ID,
        country="Iran",
        country_code="IR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=2,
        source_profile="test",
        status=EditionStatus.PUBLISHED,
    )


def _rule(rule_type: str, body: str, *, name: str | None, source_ids: list[str]) -> dict[str, Any]:
    return {
        "rule_type": rule_type,
        "name": name,
        "body": body,
        "source_ids": source_ids,
        "context": f"contexte {rule_type}",
        "evidence_quote": "extrait",
        "supported": True,
    }


def _artifact(
    artifact_id: UUID,
    run_id: UUID,
    subject_id: UUID,
    stage: ProductionArtifactStage,
    blob_id: UUID,
    *,
    metadata: dict[str, Any] | None = None,
) -> ProductionArtifact:
    return ProductionArtifact(
        id=artifact_id,
        production_run_id=run_id,
        subject_id=subject_id,
        stage=stage,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
        metadata=metadata or {},
    )


class _BlobStore:
    def __init__(self) -> None:
        self.blobs: dict[UUID, bytes] = {}

    def put(self, payload: dict[str, Any]) -> UUID:
        blob_id = uuid4()
        self.blobs[blob_id] = json.dumps(payload).encode()
        return blob_id

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return json.loads(self.blobs[blob_id])  # type: ignore[no-any-return]


class _Editions:
    def __init__(self, edition: Edition | None) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if self.edition and edition_id == self.edition.id else None


class _Manifests:
    def __init__(self, manifest: PublicationManifestV1 | None) -> None:
        self.manifest = manifest

    async def get_latest_for_edition(self, edition_id: UUID) -> PublicationManifestV1 | None:
        if self.manifest is None or self.manifest.edition_id != edition_id:
            return None
        return self.manifest


class _Artifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts
        self.current_calls: list[tuple[UUID, str]] = []

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        self.current_calls.append((run_id, stage))
        return next(
            (
                artifact
                for artifact in self.artifacts.values()
                if artifact.production_run_id == run_id and artifact.stage.value == stage
            ),
            None,
        )


class _Uow:
    def __init__(
        self,
        edition: Edition | None,
        manifest: PublicationManifestV1 | None,
        artifacts: dict[UUID, ProductionArtifact],
    ) -> None:
        self.editions = _Editions(edition)
        self.publication_manifests = _Manifests(manifest)
        self.production_artifacts = _Artifacts(artifacts)

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Fixture:
    """A published two-article edition whose rules are pinned by its manifest."""

    def __init__(
        self,
        *,
        article_b_rules: list[dict[str, Any]] | None = None,
        pin_extraction: bool = True,
        edition: Edition | None = None,
    ) -> None:
        self.blobs = _BlobStore()
        artifacts: dict[UUID, ProductionArtifact] = {}
        self.entries: list[PublicationManifestEntryV1] = []

        specs = [
            (
                1,
                SUBJECT_A,
                RUN_A,
                "TAG-182 et MarkiRAT",
                [_rule("yara", YARA_BODY, name="Marki", source_ids=["S1"])],
            ),
            (
                2,
                SUBJECT_B,
                RUN_B,
                "GigaWiper",
                article_b_rules
                if article_b_rules is not None
                else [
                    _rule("sigma", SIGMA_BODY, name="Raw disk", source_ids=["S6"]),
                    _rule("suricata", SURICATA_BODY, name=None, source_ids=["S3"]),
                ],
            ),
        ]
        for position, subject_id, run_id, title, rules in specs:
            extraction_id = uuid4()
            extraction_blob = self.blobs.put({"items": [], "uncertainties": [], "rules": rules})
            artifacts[extraction_id] = _artifact(
                extraction_id,
                run_id,
                subject_id,
                ProductionArtifactStage.EXTRACTION,
                extraction_blob,
            )
            publication_id = uuid4()
            publication_blob = self.blobs.put({"schema_version": "2", "title": title})
            artifacts[publication_id] = _artifact(
                publication_id,
                run_id,
                subject_id,
                ProductionArtifactStage.PUBLICATION,
                publication_blob,
                metadata=(
                    {"input_artifacts": {"extraction_artifact_id": str(extraction_id)}}
                    if pin_extraction
                    else {}
                ),
            )
            self.entries.append(
                PublicationManifestEntryV1(
                    position=position,
                    subject_id=subject_id,
                    production_run_id=run_id,
                    pipeline_generation=2,
                    document_artifact_id=publication_id,
                    document_artifact_version=1,
                    document_input_hash="a" * 64,
                )
            )

        self.manifest = PublicationManifestV1.create(
            edition_id=EDITION_ID,
            edition_version=3,
            batch_id=uuid4(),
            created_by="analyst",
            entries=tuple(self.entries),
            exclusions=(),
        )
        self.uow = _Uow(
            _edition() if edition is None else edition,
            self.manifest,
            artifacts,
        )

    def service(self) -> EditionRuleArchiveService:
        return EditionRuleArchiveService(lambda: self.uow, self.blobs)  # type: ignore[arg-type]


def _names(content: bytes) -> list[str]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return sorted(archive.namelist())


def _manifest(content: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return json.loads(archive.read("manifest.json"))  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_archive_groups_every_published_rule_by_engine() -> None:
    archive = await _Fixture().service().build(EDITION_ID)

    assert archive.filename == "regles-IR-2026-08.zip"
    assert archive.rule_count == 3
    assert archive.counts_by_type == {"yara": 1, "sigma": 1, "suricata": 1}
    names = _names(archive.content)
    assert names[:2] == ["README.md", "manifest.json"]
    assert [name.split("/")[0] for name in names[2:]] == ["sigma", "suricata", "yara"]
    assert names[-1].endswith(".yar")


@pytest.mark.asyncio
async def test_rule_files_carry_the_verbatim_published_body() -> None:
    archive = await _Fixture().service().build(EDITION_ID)

    with zipfile.ZipFile(BytesIO(archive.content)) as bundle:
        yara = next(name for name in bundle.namelist() if name.startswith("yara/"))
        assert bundle.read(yara).decode() == YARA_BODY
        # The filename ends on the body's own SHA-256, so the archive is
        # self-verifying without opening the manifest.
        assert hashlib.sha256(YARA_BODY.encode()).hexdigest() in yara


@pytest.mark.asyncio
async def test_manifest_names_the_article_that_carries_each_rule() -> None:
    payload = _manifest((await _Fixture().service().build(EDITION_ID)).content)

    assert payload["counts"] == {"total": 3, "yara": 1, "sigma": 1, "suricata": 1}
    assert payload["edition"]["country_code"] == "IR"
    by_type = {rule["type"]: rule for rule in payload["rules"]}
    assert by_type["yara"]["articles"] == [
        {"position": 1, "subject_id": str(SUBJECT_A), "title": "TAG-182 et MarkiRAT"}
    ]
    assert by_type["sigma"]["articles"][0]["title"] == "GigaWiper"
    assert by_type["suricata"]["name"] is None
    assert by_type["yara"]["source_ids"] == ["S1"]


@pytest.mark.asyncio
async def test_a_rule_published_twice_is_written_once_and_credits_both_articles() -> None:
    # Two articles citing the same vendor report legitimately carry the same
    # rule.  The analyst must not load it twice into a scanner.
    fixture = _Fixture(article_b_rules=[_rule("yara", YARA_BODY, name="Marki", source_ids=["S1"])])

    archive = await fixture.service().build(EDITION_ID)

    assert archive.rule_count == 1
    assert len([name for name in _names(archive.content) if name.startswith("yara/")]) == 1
    articles = _manifest(archive.content)["rules"][0]["articles"]
    assert [entry["position"] for entry in articles] == [1, 2]


@pytest.mark.asyncio
async def test_archive_reads_the_extraction_the_document_recorded_consuming() -> None:
    fixture = _Fixture()

    await fixture.service().build(EDITION_ID)

    # The pinned identity is enough: nothing resolves a "current" artifact,
    # so a later repair cannot retro-fit the published archive.
    assert fixture.uow.production_artifacts.current_calls == []


@pytest.mark.asyncio
async def test_a_document_without_input_pin_falls_back_to_the_current_extraction() -> None:
    fixture = _Fixture(pin_extraction=False)

    archive = await fixture.service().build(EDITION_ID)

    assert archive.rule_count == 3
    assert {stage for _, stage in fixture.uow.production_artifacts.current_calls} == {"extraction"}


@pytest.mark.asyncio
async def test_an_edition_without_rules_yields_a_valid_empty_archive() -> None:
    fixture = _Fixture(article_b_rules=[])
    fixture.blobs.blobs[
        fixture.uow.production_artifacts.artifacts[
            next(
                artifact_id
                for artifact_id, artifact in fixture.uow.production_artifacts.artifacts.items()
                if artifact.stage is ProductionArtifactStage.EXTRACTION
                and artifact.production_run_id == RUN_A
            )
        ].canonical_blob_id  # type: ignore[index]
    ] = json.dumps({"items": [], "uncertainties": [], "rules": []}).encode()

    archive = await fixture.service().build(EDITION_ID)

    assert archive.rule_count == 0
    assert _names(archive.content) == ["README.md", "manifest.json"]
    assert _manifest(archive.content)["counts"] == {"total": 0}


@pytest.mark.asyncio
async def test_the_archive_is_reproducible_byte_for_byte() -> None:
    fixture = _Fixture()
    first = await fixture.service().build(EDITION_ID)
    second = await fixture.service().build(EDITION_ID)

    assert first.content == second.content


@pytest.mark.asyncio
async def test_an_edition_without_a_publication_manifest_has_no_archive() -> None:
    fixture = _Fixture()
    fixture.uow.publication_manifests.manifest = None

    with pytest.raises(EditionRuleArchiveError) as error:
        await fixture.service().build(EDITION_ID)

    assert error.value.code == "publication_manifest_not_found"


@pytest.mark.asyncio
async def test_an_unknown_edition_is_reported_as_missing() -> None:
    fixture = _Fixture()
    fixture.uow.editions.edition = None

    with pytest.raises(EditionRuleArchiveError) as error:
        await fixture.service().build(EDITION_ID)

    assert error.value.code == "edition_not_found"


@pytest.mark.asyncio
async def test_a_missing_extraction_artifact_refuses_to_publish_a_partial_archive() -> None:
    fixture = _Fixture()
    for artifact_id, artifact in list(fixture.uow.production_artifacts.artifacts.items()):
        if artifact.stage is ProductionArtifactStage.EXTRACTION:
            del fixture.uow.production_artifacts.artifacts[artifact_id]
            break

    with pytest.raises(EditionRuleArchiveError) as error:
        await fixture.service().build(EDITION_ID)

    assert error.value.code == "extraction_artifact_unavailable"


async def _get_rules(service: EditionRuleArchiveService) -> Any:
    application = FastAPI()
    application.include_router(publication_router)
    application.state.edition_rule_archive_service = service
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        return await client.get(f"/api/editions/{EDITION_ID}/release/rules")


@pytest.mark.asyncio
async def test_endpoint_serves_the_archive_as_a_named_zip_attachment() -> None:
    response = await _get_rules(_Fixture().service())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == (
        'attachment; filename="regles-IR-2026-08.zip"'
    )
    assert response.headers["x-rule-count"] == "3"
    assert _names(response.content)[:2] == ["README.md", "manifest.json"]


@pytest.mark.asyncio
async def test_endpoint_reports_a_missing_manifest_as_not_found() -> None:
    fixture = _Fixture()
    fixture.uow.publication_manifests.manifest = None

    response = await _get_rules(fixture.service())

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "publication_manifest_not_found"


@pytest.mark.asyncio
async def test_endpoint_reports_an_unresolvable_extraction_as_a_conflict() -> None:
    fixture = _Fixture()
    for artifact_id, artifact in list(fixture.uow.production_artifacts.artifacts.items()):
        if artifact.stage is ProductionArtifactStage.EXTRACTION:
            del fixture.uow.production_artifacts.artifacts[artifact_id]
            break

    response = await _get_rules(fixture.service())

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "extraction_artifact_unavailable"
