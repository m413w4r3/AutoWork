"""Canonical storage of already obtained successful VirusTotal responses.

The blob is written/catalogued first.  A single PostgreSQL transaction then
creates the immutable observation and, for file reports, its normalized
view.  A failed transaction can therefore leave an unreferenced, safely
deduplicated blob; retrying the same response reuses it and never issues a
VirusTotal request.
"""

import json
from datetime import datetime
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.virustotal import (
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalPage,
    VirusTotalSearchResult,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalFileView,
    VirusTotalObservation,
    VirusTotalOperation,
)

VIRUSTOTAL_RAW_BUCKET = "virustotal-raw"
VIRUSTOTAL_JSON_MIME_TYPE = "application/json"


class VirusTotalObservationService:
    def __init__(self, catalog: BlobCatalogService, uow_factory: UnitOfWorkFactory) -> None:
        self._catalog = catalog
        self._uow_factory = uow_factory

    async def store_file_report(
        self,
        report: VirusTotalFileReport,
        *,
        subject_id: UUID | None = None,
        observed_at: datetime,
        checkpoint_id: str | None = None,
    ) -> VirusTotalObservation:
        if checkpoint_id is not None:
            async with self._uow_factory() as uow:
                existing = await uow.virustotal_observations.find_file_report_checkpoint(
                    checkpoint_id, report.file.lookup_value
                )
            if existing is not None:
                return existing
        blob = await self._catalog.ingest(
            BytesIO(report.raw_json),
            logical_bucket=VIRUSTOTAL_RAW_BUCKET,
            mime_type=VIRUSTOTAL_JSON_MIME_TYPE,
        )
        safe_parameters = {
            "file_hash": report.file.lookup_value,
            "transport": report.transport.value,
            "api_generation": report.api_generation.value,
        }
        if checkpoint_id is not None:
            safe_parameters["checkpoint_id"] = checkpoint_id
        observation = VirusTotalObservation(
            operation=VirusTotalOperation.FILE_REPORT,
            capability=VirusTotalCapability.FILE_REPORT,
            source_identifier=report.file.lookup_value,
            safe_parameters=safe_parameters,
            http_status=report.http_status,
            blob_id=blob.id,
            raw_sha256=blob.descriptor.sha256,
            raw_size=blob.descriptor.size,
            observed_at=observed_at,
            subject_id=subject_id,
            input_cursor=None,
            output_cursor=None,
            observed_count=1,
            exhaustive=True,
            page_order=0,
            execution_id=uuid4(),
        )
        view = _file_view(observation.id, report.file)
        async with self._uow_factory() as uow:
            await uow.virustotal_observations.add(observation)
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
            page.http_statuses,
            VirusTotalOperation.FILE_RELATIONSHIP,
            VirusTotalCapability.FILE_RELATIONSHIPS,
            file_hash,
            {
                "file_hash": file_hash,
                "relation": relation,
                "limit": page.limit_used,
                "input_cursor": input_cursor,
                "transport": page.transport.value,
                "api_generation": page.api_generation.value,
            },
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
            result.http_statuses,
            VirusTotalOperation.INTELLIGENCE_SEARCH,
            VirusTotalCapability.INTELLIGENCE_SEARCH,
            query,
            {
                "query": query,
                "limit": result.limit_used,
                "input_cursor": input_cursor,
                "descriptors_only": True,
                "transport": result.transport.value,
                "api_generation": result.api_generation.value,
            },
            result,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def _store_pages(
        self,
        bodies: tuple[bytes, ...],
        http_statuses: tuple[int, ...],
        operation: VirusTotalOperation,
        capability: VirusTotalCapability,
        source: str,
        parameters: dict[str, Any],
        result: VirusTotalPage | VirusTotalSearchResult,
        subject_id: UUID | None,
        input_cursor: str | None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        execution_id = uuid4()
        observations: list[VirusTotalObservation] = []
        cursor = input_cursor
        for order, (body, http_status) in enumerate(zip(bodies, http_statuses, strict=True)):
            payload = json.loads(body)
            data = payload.get("data", [])
            emitted = _cursor(payload)
            blob = await self._catalog.ingest(
                BytesIO(body),
                logical_bucket=VIRUSTOTAL_RAW_BUCKET,
                mime_type=VIRUSTOTAL_JSON_MIME_TYPE,
            )
            observation = VirusTotalObservation(
                operation=operation,
                capability=capability,
                source_identifier=source,
                safe_parameters=parameters,
                http_status=http_status,
                blob_id=blob.id,
                raw_sha256=blob.descriptor.sha256,
                raw_size=blob.descriptor.size,
                observed_at=observed_at,
                subject_id=subject_id,
                input_cursor=cursor,
                output_cursor=emitted,
                observed_count=len(data) if isinstance(data, list) else 0,
                exhaustive=result.exhaustive if order == len(bodies) - 1 else False,
                page_order=order,
                execution_id=execution_id,
            )
            async with self._uow_factory() as uow:
                await uow.virustotal_observations.add(observation)
                await uow.commit()
            observations.append(observation)
            cursor = emitted
        return tuple(observations)


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
        vhash=file.vhash,
        imphash=file.imphash,
        ssdeep=file.ssdeep,
        tlsh=file.tlsh,
        main_icon_dhash=file.main_icon_dhash,
        rich_header_hash=file.rich_header_hash,
    )
