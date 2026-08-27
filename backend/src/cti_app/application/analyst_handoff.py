"""Canonical, model-free handoff from verified synthesis to analyst research."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cti_app.application.analyst_input_pack import (
    ANALYST_INPUT_PACK_BUCKET,
    ANALYST_INPUT_PACK_SCHEMA_VERSION,
    build_analyst_input_pack_v1,
    canonical_json_bytes,
)
from cti_app.application.persistence import ProductionUnitOfWork, ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionProfile,
    SubjectProductionRun,
)


def loop_budget_from_settings(settings: Any) -> LoopBudget:
    """Build a new investigation's `LoopBudget` from configured caps.

    `settings` is typed loosely to keep this module free of a hard import
    on `cti_app.config`; callers pass their already-loaded `Settings`.
    """
    return LoopBudget(
        max_cycles=settings.investigation_max_cycles,
        max_pivot_runs=settings.investigation_max_pivot_runs,
        max_hits_acquired=settings.investigation_max_hits_acquired,
        max_new_samples=settings.investigation_max_new_samples,
        max_vt_read_units=settings.investigation_max_vt_read_units,
    )


@dataclass(frozen=True, slots=True)
class AnalystHandoffPolicy:
    tlp: str | None = None
    do_not_submit: bool = False
    external_llm_allowed: bool = False


@dataclass(frozen=True, slots=True)
class AnalystHandoffResult:
    investigation_id: UUID
    input_sha256: str
    file_indicators: tuple[str, ...]


def analyst_handoff_policy_from_sources(sources: Iterable[object]) -> AnalystHandoffPolicy:
    """Derive the persisted policy fields without involving a model or network."""
    items = tuple(sources)
    do_not_submit = any(bool(getattr(item, "do_not_submit", False)) for item in items)
    external_llm_allowed = not any(
        bool(getattr(item, "do_not_submit", False))
        or not bool(getattr(item, "external_llm_allowed", True))
        for item in items
    )
    tlps = {getattr(item, "tlp", None) for item in items if getattr(item, "tlp", None)}
    return AnalystHandoffPolicy(
        tlp=next(iter(tlps)) if len(tlps) == 1 else None,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_llm_allowed,
    )


def _extraction_items(items: Iterable[Any]) -> list[dict[str, Any]]:
    """Translate verified Q2 values without inspecting synthesis prose."""
    converted: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            converted.append(item)
            continue
        artifact_type = getattr(item, "artifact_type", None)
        indicator_status = getattr(item, "indicator_status", None)
        converted.append(
            {
                "id": getattr(item, "local_id", None),
                "value": getattr(item, "value", None),
                "normalized_value": getattr(item, "normalized_value", None),
                "artifact_type": getattr(artifact_type, "value", artifact_type),
                "indicator_status": getattr(indicator_status, "value", indicator_status),
                "source_ids": list(getattr(item, "source_ids", ())),
                "supported": getattr(item, "supported", False),
            }
        )
    return converted


class AnalystPostSynthesisService:
    """Persist exactly one deterministic analyst handoff for a major run.

    This service does not call models or VirusTotal.  A caller may invoke the
    optional seed enrichment only after this canonical transaction commits.
    """

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        loop_budget_factory: Callable[[], LoopBudget] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        # Never read Settings here: the caller builds and injects the caps a
        # new investigation starts with.  Falling back to LoopBudget()'s own
        # zero business caps keeps existing callers behaviorally unchanged
        # until they are updated to inject the configured factory.
        self._loop_budget_factory = loop_budget_factory or (lambda: LoopBudget())

    async def ensure_for_verified_synthesis(
        self,
        *,
        run: SubjectProductionRun,
        synthesis: ProductionArtifact,
        extraction_artifacts: Iterable[ProductionArtifact],
        extraction_items: Iterable[Any],
        policy: AnalystHandoffPolicy,
        uow: ProductionUnitOfWork | None = None,
    ) -> AnalystHandoffResult | None:
        if run.profile is not ProductionProfile.MAJOR_ASSISTED:
            return None
        if run.research_date is None:
            raise ValueError("Analyst input pack requires frozen research_date")

        if uow is not None:
            return await self._ensure_in_uow(
                uow=uow,
                run=run,
                synthesis=synthesis,
                extraction_artifacts=tuple(extraction_artifacts),
                extraction_items=_extraction_items(extraction_items),
                policy=policy,
            )

        async with self._uow_factory() as transaction:
            result = await self._ensure_in_uow(
                uow=transaction,
                run=run,
                synthesis=synthesis,
                extraction_artifacts=tuple(extraction_artifacts),
                extraction_items=_extraction_items(extraction_items),
                policy=policy,
            )
            await transaction.commit()
            return result

    async def _ensure_in_uow(
        self,
        *,
        uow: ProductionUnitOfWork,
        run: SubjectProductionRun,
        synthesis: ProductionArtifact,
        extraction_artifacts: tuple[ProductionArtifact, ...],
        extraction_items: list[dict[str, Any]],
        policy: AnalystHandoffPolicy,
    ) -> AnalystHandoffResult:
        if run.research_date is None:
            raise ValueError("Analyst input pack requires frozen research_date")
        existing = await uow.analyst_investigations.get_for_run(run.id)
        if existing is not None:
            persisted_pack = await uow.analyst_input_packs.get_for_investigation(existing.id)
            if (
                persisted_pack is None
                or existing.input_pack_blob_id != persisted_pack.blob_id
                or existing.input_sha256 != persisted_pack.sha256
            ):
                raise ValueError("Analyst investigation input pack reference is inconsistent")
            payload = await self._artifact_store.read_json(persisted_pack.blob_id)
            if hashlib.sha256(canonical_json_bytes(payload)).hexdigest() != persisted_pack.sha256:
                raise ValueError("Analyst investigation input pack content is inconsistent")
            return AnalystHandoffResult(
                investigation_id=existing.id,
                input_sha256=persisted_pack.sha256,
                file_indicators=tuple(
                    item["value"]
                    for item in payload.get("file_indicators", [])
                    if isinstance(item, dict) and isinstance(item.get("value"), str)
                ),
            )

        investigation = AnalystInvestigation.from_verified_synthesis(
            synthesis=synthesis,
            budget=self._loop_budget_factory(),
        )
        pack = build_analyst_input_pack_v1(
            run=run,
            investigation=investigation,
            synthesis=synthesis,
            extraction_artifacts=extraction_artifacts,
            extraction_items=extraction_items,
            tlp=policy.tlp,
            do_not_submit=policy.do_not_submit,
            external_llm_allowed=policy.external_llm_allowed,
            research_date=run.research_date,
        )
        blob_id, sha256 = await self._artifact_store.put_canonical_json(
            pack.payload, bucket=ANALYST_INPUT_PACK_BUCKET
        )
        investigation.input_pack_blob_id = blob_id
        investigation.input_sha256 = sha256
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(
            AnalystInputPack(
                investigation_id=investigation.id,
                blob_id=blob_id,
                sha256=sha256,
                schema_version=ANALYST_INPUT_PACK_SCHEMA_VERSION,
            )
        )
        return AnalystHandoffResult(
            investigation_id=investigation.id,
            input_sha256=sha256,
            file_indicators=tuple(item["value"] for item in pack.payload["file_indicators"]),
        )
