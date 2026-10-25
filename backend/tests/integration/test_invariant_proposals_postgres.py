"""P10 PostgreSQL proof with persisted synthetic inputs and a local fake model."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.goodware import (
    _FEATURES_SQL,
    _METADATA_SQL,
    INDEX_FILENAME,
    INDEX_FORMAT_VERSION,
    KEY_VERSION,
    MANIFEST_FILENAME,
    NON_DISCRIMINANT_PATTERN_VERSION,
    NORMALIZATION_VERSION,
    SCHEMA_VERSION,
    SOURCE_FORMAT,
    GoodwareMeasurementService,
    GoodwareService,
    _canonical_json,
    _expected_metadata,
    _source_set_sha256,
    baseline_fingerprint_sha256,
    goodware_lookup_key,
)
from cti_app.application.invariant_proposals import ProposalConversationService
from cti_app.application.invariants import InvariantRegistryService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelCapabilities,
    ModelGateway,
    ModelRouter,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.analysis import SampleFeatureSetV1, SampleFormat
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Sample, SampleState, Subject
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.model_conversations import ConversationPurpose, ConversationTurnStatus
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelTransport,
    ModelUsage,
)
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

_P10_RESPONSE = (
    "advisor-wire.v1\n"
    "TYPE: p10_proposal\n"
    "STATUS: succeeded\n"
    "CANDIDATE: literal_string | c2_indicator | exact | M05_SYNTHETIC_C2_MARKER | "
    "Synthetic marker from the persisted inert fixture. | refs=E1\n"
    "RISK: none\n"
    "VALIDATION: compare only with the local synthetic corpus\n"
    "QUESTION: none\n"
)


class _LocalP10Adapter:
    provider = ModelProvider.FAKE
    backend = ModelBackend.FAKE
    transport = ModelTransport.FAKE
    requested_model = "m05-c1-local-scripted"
    is_external = False
    capabilities = ModelCapabilities(conversation=True)

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(
        self,
        request: Any,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        assert request.conversation is not None
        assert request.web_search is False
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=4, output_tokens=6, total_tokens=10),
            output_text=_P10_RESPONSE,
        )

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("The local scripted P10 adapter does not resume requests")


def _write_goodware_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source_dir = tmp_path / "goodware-sources"
    artifact_dir = tmp_path / "goodware-artifact"
    source_dir.mkdir()
    artifact_dir.mkdir()

    source_bytes = b"synthetic local goodware source fixture\n"
    (source_dir / "good-strings.db").write_bytes(source_bytes)
    source = {
        "filename": "good-strings.db",
        "feature_kind": "string",
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "size": len(source_bytes),
        "entry_count": 1,
        "occurrence_sum": 2,
    }
    source_set_sha256 = _source_set_sha256([source])
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_format": SOURCE_FORMAT,
        "source_set_sha256": source_set_sha256,
        "normalization_version": NORMALIZATION_VERSION,
        "key_version": KEY_VERSION,
        "index_format_version": INDEX_FORMAT_VERSION,
        "baseline_fingerprint_sha256": baseline_fingerprint_sha256(source_set_sha256),
        "record_count": 1,
        "occurrence_sum": 2,
        "index_sha256": "0" * 64,
        "index_size": 1,
        "sources": [source],
    }

    index_path = artifact_dir / INDEX_FILENAME
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(_FEATURES_SQL)
        connection.execute(_METADATA_SQL)
        connection.execute(
            "INSERT INTO features VALUES (?, ?)",
            (goodware_lookup_key("string", "unrelated-fixture-value"), 2),
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            _expected_metadata(manifest, pattern_version=NON_DISCRIMINANT_PATTERN_VERSION).items(),
        )
        connection.commit()
    finally:
        connection.close()

    index_bytes = index_path.read_bytes()
    manifest["index_sha256"] = hashlib.sha256(index_bytes).hexdigest()
    manifest["index_size"] = len(index_bytes)
    (artifact_dir / MANIFEST_FILENAME).write_text(
        _canonical_json(manifest) + "\n", encoding="utf-8"
    )
    return artifact_dir, source_dir


async def _seed_p10_subject(
    uow_factory: Any,
    artifact_store: ProductionArtifactStore,
) -> tuple[AnalystInvestigation, Sample, SampleFeatureSetV1]:
    suffix = uuid4().hex[:12]
    edition = Edition(
        country=f"M05-C synthetic P10 fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.CLEAR,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Synthetic P10 sample fixture",
        slug=f"m05-c-p10-{suffix}",
        tlp=TLP.CLEAR,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis_payload = {
        "title": "Synthetic P10 fixture synthesis",
        "subject_id": str(subject.id),
        "lead": [],
        "sections": [],
        "timeline": [],
        "uncertainties": ["Synthetic fixture only; no external source was consulted."],
        "indicators": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="m05c-p10-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        canonical_blob_id=synthesis_blob_id,
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"synthetic_fixture": True},
        "policy": {"tlp": "clear", "do_not_submit": True, "external_llm_allowed": False},
    }
    input_blob_id, input_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="m05c-p10-input-packs"
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(max_cycles=1, max_vt_read_units=0, max_hits_acquired=0),
        input_pack_blob_id=input_blob_id,
        input_sha256=input_sha256,
    )
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_blob_id,
        sha256=input_sha256,
        schema_version="analyst-input-pack-v1",
    )

    sample_bytes = b"M05-C inert fixture\nM05_SYNTHETIC_C2_MARKER\n"
    sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()
    sample_blob_id = await artifact_store.put_bytes(
        sample_bytes,
        bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    sample = Sample(
        subject_id=subject.id,
        blob_id=sample_blob_id,
        original_name="m05-c-inert-fixture.bin",
        origin="M05-C synthetic P10 integration fixture",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=True,
        external_llm_allowed=False,
        origin_kind=EntitySampleOrigin.MANUAL,
        state=SampleState.QUARANTINED,
        source_service="m05-c-p10-fixture",
        source_object_id=sample_sha256,
        expected_hash=sample_sha256,
    )
    marker = "M05_SYNTHETIC_C2_MARKER"
    marker_offset = sample_bytes.index(marker.encode("ascii"))
    feature_set = SampleFeatureSetV1(
        sample_id=sample.id,
        blob_id=sample_blob_id,
        extractor_version="m05-c-fixture-string-extractor-v1",
        parameters_sha256=hashlib.sha256(b"m05-c-fixture-static-parameters-v1").hexdigest(),
        format=SampleFormat.UNKNOWN,
        size=len(sample_bytes),
        md5=hashlib.md5(sample_bytes, usedforsecurity=False).hexdigest(),
        sha1=hashlib.sha1(sample_bytes, usedforsecurity=False).hexdigest(),
        sha256=sample_sha256,
        strings=({"value": marker, "offsets": (marker_offset,), "occurrence_count": 1},),
        sections=(),
        imports=(),
        exports=(),
        resources=(),
        signature=None,
        imphash=None,
        ssdeep=None,
        tlsh=None,
        rich_header_hash=None,
        opcode_fragment16=(),
        partial_errors=("Synthetic test extractor; not a production sample analysis.",),
        tlp=TLP.CLEAR,
        do_not_submit=True,
        external_llm_allowed=False,
    )

    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.samples.add(sample)
        inserted = await uow.sample_feature_sets.add_if_absent(feature_set, sample_blob_id)
        assert inserted
        await uow.sample_feature_sets.index(feature_set)
        await uow.commit()
    return investigation, sample, feature_set


@pytest.mark.asyncio
async def test_p10_propose_uses_persisted_preconditions_and_replays_turn(
    uow_factory: Any,
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    del migrated_postgres_url
    blob_store = FilesystemBlobStore(tmp_path / "m05c-p10-blobs")
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    investigation, sample, feature_set = await _seed_p10_subject(uow_factory, artifact_store)

    artifact_dir, source_dir = _write_goodware_fixture(tmp_path)
    goodware_service = GoodwareService(blob_catalog, uow_factory)
    baseline = await goodware_service.import_index(artifact_dir, source_dir)
    await goodware_service.bind(investigation.id, baseline.id)
    goodware_measurement = GoodwareMeasurementService(
        blob_store,
        uow_factory,
        cache_root=tmp_path / "m05c-goodware-cache",
    )
    prepared = await goodware_measurement.prepare(baseline.id)
    assert prepared.baseline_id == baseline.id
    assert await prepared.lookup("string", "unrelated-fixture-value") == 2

    async with uow_factory() as uow:
        stored_sample = await uow.samples.get(sample.id)
        stored_baseline = await uow.goodware_baselines.get(baseline.id)
        stored_binding = await uow.investigation_goodware_baselines.get(investigation.id)
        stored_features = await uow.sample_feature_sets.list_for_samples((sample.id,))
        index_artifact = await uow.goodware_baselines.get_index_artifact(
            baseline.id,
            index_format_version=INDEX_FORMAT_VERSION,
            key_version=KEY_VERSION,
        )
    assert stored_sample is not None
    assert stored_baseline is not None
    assert stored_binding == baseline.id
    assert stored_features
    assert stored_features[0]["payload"]["sha256"] == feature_set.sha256
    assert index_artifact is not None

    adapter = _LocalP10Adapter()
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        openai_drafting=adapter,
        openai_critic=adapter,
        qwen=adapter,
        fake=adapter,
    )
    gateway = ModelGateway(
        router,
        uow_factory,
        BlobModelOutputStore(blob_catalog),
    )
    model_conversations = ModelConversationService(uow_factory, gateway, blob_store)
    invariant_registry = InvariantRegistryService(uow_factory, goodware_measurement)
    service = ProposalConversationService(
        uow_factory,
        model_conversations,
        invariant_registry,
        goodware_measurement,
        local_provider=ModelProvider.FAKE,
    )

    first = await service.propose(investigation_id=investigation.id, cycle_number=1)
    assert first.outcome.value == "succeeded"
    assert first.snapshot.goodware_baseline_id == baseline.id
    assert first.snapshot.context["origin_samples"][0]["sample_id"] == str(sample.id)
    assert first.snapshot.context["static_features"]
    assert len(first.invariant_results) == 1
    p09_result = first.invariant_results[0]
    assert p09_result.invariant is not None or p09_result.rejection is not None
    assert len(adapter.calls) == 1

    async with uow_factory() as uow:
        conversation = await uow.model_conversations.get(first.conversation_id)
        turns = await uow.model_conversation_turns.list_for_conversation(first.conversation_id)
        assert conversation is not None
        assert conversation.purpose is ConversationPurpose.PIVOT_RESEARCH
        assert conversation.expected_profile == f"p10:{investigation.id}"
        assert len(turns) == 1
        assert turns[0].id == first.turn.id
        assert turns[0].status is ConversationTurnStatus.SUCCEEDED

    replay = await service.propose(investigation_id=investigation.id, cycle_number=1)
    assert replay.turn.id == first.turn.id
    assert replay.idempotency_key == first.idempotency_key
    assert replay.outcome is first.outcome
    assert len(replay.invariant_results) == 1
    assert len(adapter.calls) == 1
    async with uow_factory() as uow:
        turns_after_replay = await uow.model_conversation_turns.list_for_conversation(
            first.conversation_id
        )
    assert len(turns_after_replay) == 1
