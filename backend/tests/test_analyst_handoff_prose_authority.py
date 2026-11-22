"""Prose authority is fixed when the analyst handoff inserts its investigation.

ADR-0011 D4: ``legacy`` stays the default for every existing caller, the M24
workshop path requests ``bridge_only`` explicitly, and an already-existing
investigation can never be silently kept or flipped under another authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.analyst_handoff import (
    AnalystHandoffPolicy,
    AnalystPostSynthesisService,
    ProseAuthorityConflictError,
)
from cti_app.application.analyst_input_pack import ANALYST_INPUT_PACK_BUCKET
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProseAuthority,
)


class _Investigations:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInvestigation] = {}

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id),
            None,
        )

    async def add(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation


class _Packs:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInputPack] = {}

    async def get_for_investigation(self, investigation_id: UUID) -> AnalystInputPack | None:
        return next(
            (item for item in self.items.values() if item.investigation_id == investigation_id),
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


class _ArtifactStore:
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


def _fixture() -> tuple[ProductionRun, ProductionArtifact, _Uow, _ArtifactStore]:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        research_date=date(2026, 11, 22),
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        status=ProductionArtifactStatus.VERIFIED,
        version=1,
        input_hash="a" * 64,
    )
    return run, synthesis, _Uow(), _ArtifactStore()


async def _ensure(
    service: AnalystPostSynthesisService,
    run: ProductionRun,
    synthesis: ProductionArtifact,
    **kwargs: Any,
):
    return await service.ensure_for_verified_synthesis(
        run=run,
        synthesis=synthesis,
        extraction_artifacts=(),
        extraction_items=(),
        policy=AnalystHandoffPolicy(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_handoff_defaults_to_legacy_prose_authority() -> None:
    run, synthesis, uow, store = _fixture()
    service = AnalystPostSynthesisService(lambda: uow, store)  # type: ignore[arg-type]

    created = await _ensure(service, run, synthesis)
    replay = await _ensure(service, run, synthesis)

    assert created is not None and replay is not None
    assert created.investigation_id == replay.investigation_id
    persisted = next(iter(uow.analyst_investigations.items.values()))
    assert persisted.prose_authority is ProseAuthority.LEGACY
    assert len(uow.analyst_investigations.items) == 1


@pytest.mark.asyncio
async def test_handoff_creates_a_bridge_only_investigation_when_requested() -> None:
    run, synthesis, uow, store = _fixture()
    service = AnalystPostSynthesisService(lambda: uow, store)  # type: ignore[arg-type]

    created = await _ensure(service, run, synthesis, prose_authority=ProseAuthority.BRIDGE_ONLY)
    replay = await _ensure(service, run, synthesis, prose_authority=ProseAuthority.BRIDGE_ONLY)

    assert created is not None and replay is not None
    assert created.investigation_id == replay.investigation_id
    persisted = next(iter(uow.analyst_investigations.items.values()))
    assert persisted.prose_authority is ProseAuthority.BRIDGE_ONLY
    assert len(uow.analyst_investigations.items) == 1


@pytest.mark.asyncio
async def test_reensure_with_a_different_prose_authority_is_a_typed_refusal() -> None:
    run, synthesis, uow, store = _fixture()
    service = AnalystPostSynthesisService(lambda: uow, store)  # type: ignore[arg-type]
    created = await _ensure(service, run, synthesis)
    assert created is not None

    with pytest.raises(ProseAuthorityConflictError) as exc_info:
        await _ensure(service, run, synthesis, prose_authority=ProseAuthority.BRIDGE_ONLY)

    assert exc_info.value.code == "prose_authority_conflict"
    assert exc_info.value.existing is ProseAuthority.LEGACY
    assert exc_info.value.requested is ProseAuthority.BRIDGE_ONLY
    persisted = uow.analyst_investigations.items[created.investigation_id]
    assert persisted.prose_authority is ProseAuthority.LEGACY
    assert len(uow.analyst_investigations.items) == 1


@pytest.mark.asyncio
async def test_bridge_only_investigation_refuses_a_legacy_reensure() -> None:
    run, synthesis, uow, store = _fixture()
    service = AnalystPostSynthesisService(lambda: uow, store)  # type: ignore[arg-type]
    created = await _ensure(service, run, synthesis, prose_authority=ProseAuthority.BRIDGE_ONLY)
    assert created is not None

    with pytest.raises(ProseAuthorityConflictError):
        await _ensure(service, run, synthesis)

    persisted = uow.analyst_investigations.items[created.investigation_id]
    assert persisted.prose_authority is ProseAuthority.BRIDGE_ONLY
