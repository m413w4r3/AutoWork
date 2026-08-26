"""Canonical storage of already obtained successful VirusTotal responses.

The blob is written/catalogued first.  The following PostgreSQL transaction creates
the immutable observation and its optional normalized view.  A failed transaction
can therefore leave an unreferenced, safely deduplicated blob; retrying the same
response reuses it and never issues a VirusTotal request.
"""

import json
from datetime import datetime
from io import BytesIO
from typing import Any
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.virustotal import (
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalPage,
    VirusTotalSearchResult,
)
from cti_app.domain.virustotal import VirusTotalFileView, VirusTotalObservation, VirusTotalOperation

VIRUSTOTAL_RAW_BUCKET = "virustotal-raw"
VIRUSTOTAL_JSON_MIME_TYPE = "application/json"


class VirusTotalObservationService:
    def __init__(self, catalog: BlobCatalogService, uow_factory: UnitOfWorkFactory) -> None:
        self._catalog = catalog
        self._uow_factory = uow_factory

    async def store_file_report(
        self, report: VirusTotalFileReport, *, subject_id: UUID | None = None, observed_at: datetime
    ) -> VirusTotalObservation:
        observation = await self._store_raw(
            raw_body=report.raw_json,
            operation=VirusTotalOperation.FILE_REPORT,
            capability="file_report",
            source_identifier=report.file.lookup_value,
            safe_parameters={"file_hash": report.file.lookup_value},
            subject_id=subject_id,
            observed_at=observed_at,
            input_cursor=None,
            output_cursor=None,
            observed_count=1,
            exhaustive=True,
            page_order=0,
        )
        view = _file_view(observation.id, report.file)
        async with self._uow_factory() as uow:
            await uow.virustotal_file_views.add_if_absent(view)
            await uow.commit()
        return observation

    async def store_file_relationship(
        self,
        page: VirusTotalPage,
        *,
        file_hash: str,
        relation: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return await self._store_pages(
            page.raw_json_pages,
            VirusTotalOperation.FILE_RELATIONSHIP,
            "file_relationships",
            file_hash,
            {"file_hash": file_hash, "relation": relation},
            page,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def store_intelligence_search(
        self,
        result: VirusTotalSearchResult,
        *,
        query: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return await self._store_pages(
            result.raw_json_pages,
            VirusTotalOperation.INTELLIGENCE_SEARCH,
            "intelligence_search",
            query,
            {"query": query},
            result,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def _store_pages(
        self,
        bodies: tuple[bytes, ...],
        operation: VirusTotalOperation,
        capability: str,
        source: str,
        parameters: dict[str, Any],
        result: VirusTotalPage | VirusTotalSearchResult,
        subject_id: UUID | None,
        input_cursor: str | None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        observations: list[VirusTotalObservation] = []
        cursor = input_cursor
        for order, body in enumerate(bodies):
            payload = json.loads(body)
            data = payload.get("data", [])
            emitted = _cursor(payload)
            observations.append(
                await self._store_raw(
                    raw_body=body,
                    operation=operation,
                    capability=capability,
                    source_identifier=source,
                    safe_parameters=parameters,
                    subject_id=subject_id,
                    observed_at=observed_at,
                    input_cursor=cursor,
                    output_cursor=emitted,
                    observed_count=len(data) if isinstance(data, list) else 0,
                    exhaustive=result.exhaustive if order == len(bodies) - 1 else False,
                    page_order=order,
                )
            )
            cursor = emitted
        return tuple(observations)

    async def _store_raw(
        self,
        *,
        raw_body: bytes,
        operation: VirusTotalOperation,
        capability: str,
        source_identifier: str,
        safe_parameters: dict[str, Any],
        subject_id: UUID | None,
        observed_at: datetime,
        input_cursor: str | None,
        output_cursor: str | None,
        observed_count: int,
        exhaustive: bool,
        page_order: int,
    ) -> VirusTotalObservation:
        blob = await self._catalog.ingest(
            BytesIO(raw_body),
            logical_bucket=VIRUSTOTAL_RAW_BUCKET,
            mime_type=VIRUSTOTAL_JSON_MIME_TYPE,
        )
        observation = VirusTotalObservation(
            operation=operation,
            capability=capability,
            source_identifier=source_identifier,
            safe_parameters=safe_parameters,
            http_status=200,
            blob_id=blob.id,
            raw_sha256=blob.descriptor.sha256,
            raw_size=blob.descriptor.size,
            observed_at=observed_at,
            subject_id=subject_id,
            input_cursor=input_cursor,
            output_cursor=output_cursor,
            observed_count=observed_count,
            exhaustive=exhaustive,
            page_order=page_order,
        )
        async with self._uow_factory() as uow:
            await uow.virustotal_observations.add(observation)
            await uow.commit()
        return observation


def _cursor(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    value = meta.get("cursor") if isinstance(meta, dict) else None
    return value if isinstance(value, str) else None


def _file_view(observation_id: UUID, file: VirusTotalFile) -> VirusTotalFileView:
    return VirusTotalFileView(
        observation_id=observation_id,
        vt_file_id=file.id,
        file_type=file.type,
        lookup_hash=file.lookup_value,
        meaningful_name=file.meaningful_name,
        type_description=file.type_description,
        size=file.size,
        last_analysis_stats=file.last_analysis_stats,
        first_submission_date=file.first_submission_date,
        last_submission_date=file.last_submission_date,
        last_modification_date=file.last_modification_date,
        tags=file.tags,
    )
