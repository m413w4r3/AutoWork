from __future__ import annotations

import hashlib
import json
from io import BytesIO
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.capabilities import (
    CapabilitiesService,
    register_capa_analysis_job,
)
from cti_app.application.jobs import JobExecutionContext, JobParameters, JobRegistry
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.analysis import SampleFeatureSetV1
from cti_app.domain.entities import ProvenanceEvent, SampleHashSource
from cti_app.domain.errors import EntityNotFoundError
from cti_app.infrastructure.static_analysis import StaticFeatureExtractor

STATIC_ANALYSIS_JOB_KIND = "sample.static_analysis.v1"


class StaticAnalysisJobParameters(JobParameters):
    sample_id: UUID


class StaticAnalysisService:
    def __init__(
        self,
        blobs: BlobCatalogService,
        uow_factory: UnitOfWorkFactory,
        extractor: StaticFeatureExtractor | None = None,
        *,
        max_sample_bytes: int = 200 * 1024 * 1024,
        string_min_length: int = 4,
        max_strings: int = 10_000,
    ) -> None:
        self._blobs, self._uow_factory, self._extractor = (
            blobs,
            uow_factory,
            extractor or StaticFeatureExtractor(),
        )
        self._max_sample_bytes, self._string_min_length, self._max_strings = (
            max_sample_bytes,
            string_min_length,
            max_strings,
        )

    async def analyze(self, sample_id: UUID) -> SampleFeatureSetV1:
        parameters = {
            "analysis_string_min_length": self._string_min_length,
            "analysis_max_strings": self._max_strings,
            "analysis_max_sample_bytes": self._max_sample_bytes,
        }
        parameter_hash = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
        async with self._uow_factory() as uow:
            sample = await uow.samples.get(sample_id)
            if sample is None:
                raise EntityNotFoundError(f"Sample {sample_id} does not exist")
            existing = await uow.sample_feature_sets.get(sample_id, "static-v1", parameter_hash)
            if existing is not None:
                return existing
        payload = await self._blobs.read(sample.blob_id, max_bytes=self._max_sample_bytes)
        features = self._extractor.extract(
            sample_id=sample.id,
            blob_id=sample.blob_id,
            payload=payload,
            parameters_sha256=parameter_hash,
            tlp=sample.tlp,
            do_not_submit=sample.do_not_submit,
            external_llm_allowed=sample.external_llm_allowed,
            max_strings=self._max_strings,
            min_string_length=self._string_min_length,
        )
        descriptor = await self._blobs.ingest(
            BytesIO(json.dumps(features.as_json(), sort_keys=True).encode()),
            logical_bucket="sample-features",
            mime_type="application/json",
        )
        async with self._uow_factory() as uow:
            inserted = await uow.sample_feature_sets.add_if_absent(features, descriptor.id)
            if not inserted:
                existing = await uow.sample_feature_sets.get(sample_id, "static-v1", parameter_hash)
                if existing is None:
                    raise RuntimeError("feature set conflict without row")
                return existing
            await uow.sample_feature_sets.index(features)
            current = await uow.samples.get(sample_id)
            if current is not None:
                for name in ("imphash", "ssdeep", "tlsh", "rich_header_hash"):
                    value = getattr(features, name)
                    current_value = getattr(current, name)
                    source = getattr(current, f"{name}_source")
                    if source == SampleHashSource.VT:
                        if value is not None and value != current_value:
                            await uow.provenance.append(
                                ProvenanceEvent(
                                    aggregate_type="sample",
                                    aggregate_id=sample.id,
                                    event_type="sample.local_similarity_divergence",
                                    payload={
                                        "field": name,
                                        "vt_value": current_value,
                                        "local_value": value,
                                    },
                                    tlp=sample.tlp,
                                    subject_id=sample.subject_id,
                                    actor_id="system:analysis-worker",
                                )
                            )
                    elif value is not None:
                        setattr(current, name, value)
                        setattr(current, f"{name}_source", SampleHashSource.LOCAL)
                await uow.samples.save(current)
            await uow.commit()
        return features


def create_analysis_job_registry(
    service: StaticAnalysisService,
    capabilities_service: CapabilitiesService | None = None,
) -> JobRegistry:
    registry = JobRegistry()

    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, StaticAnalysisJobParameters):
            raise TypeError("invalid parameters")
        result = await service.analyze(parameters.sample_id)
        await context.report_progress(1, 1, "Analyse statique terminée")
        return f"sample-features://{result.sample_id}/{result.parameters_sha256}"

    registry.register(STATIC_ANALYSIS_JOB_KIND, StaticAnalysisJobParameters, handler)
    if capabilities_service is not None:
        register_capa_analysis_job(registry, capabilities_service)
    return registry
