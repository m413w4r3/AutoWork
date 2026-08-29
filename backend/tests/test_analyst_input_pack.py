from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_handoff import AnalystHandoffPolicy, AnalystPostSynthesisService
from cti_app.application.analyst_input_pack import (
    ANALYST_INPUT_PACK_BUCKET,
    build_analyst_input_pack_v1,
)
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)


class _Investigations:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInvestigation] = {}

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id), None
        )

    async def add(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation


class _Packs:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInputPack] = {}

    async def get_for_investigation(self, investigation_id: UUID) -> AnalystInputPack | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.investigation_id == investigation_id
            ),
            None,
        )

    async def append(self, pack: AnalystInputPack) -> None:
        self.items[pack.id] = pack


class _Uow:
    def __init__(self) -> None:
        self.analyst_investigations = _Investigations()
        self.analyst_input_packs = _Packs()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Store:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        assert bucket == ANALYST_INPUT_PACK_BUCKET
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id, hashlib.sha256(encoded).hexdigest()

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]


def test_pack_is_canonical_and_uses_only_accepted_structured_file_indicators() -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(), edition_id=uuid4()
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="a" * 64,
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="b" * 64,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis, budget=LoopBudget()
    )
    items = [
        {
            "id": "q2-hash",
            "normalized_value": "A" * 64,
            "artifact_type": "hash",
            "supported": True,
            "indicator_status": "confirmed_ioc",
            "source_ids": ["S2", "S1"],
        },
        {
            "value": "b" * 64,
            "artifact_type": "hash",
            "supported": True,
            "indicator_status": "excluded",
            "source_ids": ["S3"],
        },
        # A hash-looking sentence is not an accepted structured indicator.
        {
            "value": "prose " + "c" * 64,
            "artifact_type": "hash",
            "supported": True,
            "indicator_status": "confirmed_ioc",
            "source_ids": ["S4"],
        },
    ]
    pack = build_analyst_input_pack_v1(
        run=run,
        investigation=investigation,
        synthesis=synthesis,
        extraction_artifacts=(extraction,),
        extraction_items=items,
        tlp="amber",
        do_not_submit=True,
        external_llm_allowed=False,
        research_date=date(2026, 8, 26),
    )
    again = build_analyst_input_pack_v1(
        run=run,
        investigation=investigation,
        synthesis=synthesis,
        extraction_artifacts=(extraction,),
        extraction_items=reversed(items),
        tlp="amber",
        do_not_submit=True,
        external_llm_allowed=False,
        research_date=date(2026, 8, 26),
    )

    assert ANALYST_INPUT_PACK_BUCKET == "analyst-input-packs"
    assert pack.canonical_bytes == again.canonical_bytes
    assert pack.sha256 == hashlib.sha256(pack.canonical_bytes).hexdigest()
    assert pack.payload["file_indicators"] == [
        {
            "hash_type": "sha256",
            "value": "a" * 64,
            "provenance": {"source_ids": ["S1", "S2"], "extraction_item_id": "q2-hash"},
        }
    ]
    assert pack.payload["policy"] == {
        "tlp": "amber",
        "do_not_submit": True,
        "external_llm_allowed": False,
    }


@pytest.mark.asyncio
async def test_handoff_is_idempotent_and_only_uses_q2_structured_indicators() -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        research_date=date(2026, 8, 26),
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="a" * 64,
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="b" * 64,
    )
    uow, store = _Uow(), _Store()
    service = AnalystPostSynthesisService(lambda: uow, store)  # type: ignore[arg-type]
    items = [
        {
            "id": "accepted",
            "normalized_value": "a" * 64,
            "artifact_type": "hash",
            "indicator_status": "confirmed_ioc",
            "supported": True,
            "source_ids": ["S1"],
        },
        {
            "id": "prose",
            "value": "A hash mentioned in prose: " + "b" * 64,
            "artifact_type": "hash",
            "indicator_status": "confirmed_ioc",
            "supported": True,
            "source_ids": ["S1"],
        },
    ]
    created = await service.ensure_for_verified_synthesis(
        run=run,
        synthesis=synthesis,
        extraction_artifacts=(extraction,),
        extraction_items=items,
        policy=AnalystHandoffPolicy(external_llm_allowed=True),
    )
    replay = await service.ensure_for_verified_synthesis(
        run=run,
        synthesis=synthesis,
        extraction_artifacts=(extraction,),
        extraction_items=items,
        policy=AnalystHandoffPolicy(external_llm_allowed=True),
    )

    assert created is not None and replay is not None
    assert created.investigation_id == replay.investigation_id
    assert len(uow.analyst_investigations.items) == len(uow.analyst_input_packs.items) == 1
    persisted = next(iter(store.payloads.values()))
    assert [item["value"] for item in persisted["file_indicators"]] == ["a" * 64]


@pytest.mark.asyncio
async def test_handoff_rejects_an_inconsistent_existing_pack() -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        research_date=date(2026, 8, 26),
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="a" * 64,
    )
    uow, store = _Uow(), _Store()
    bad = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(),
        input_pack_blob_id=uuid4(),
        input_sha256="a" * 64,
    )
    await uow.analyst_investigations.add(bad)
    service = AnalystPostSynthesisService(lambda: uow, store)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inconsistent"):
        await service.ensure_for_verified_synthesis(
            run=run,
            synthesis=synthesis,
            extraction_artifacts=(),
            extraction_items=(),
            policy=AnalystHandoffPolicy(),
        )
