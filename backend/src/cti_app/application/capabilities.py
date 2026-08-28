from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.jobs import JobExecutionContext, JobParameters, JobRegistry
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.capabilities import Capability, CapabilitySet, CapabilitySetStatus
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.reference_corpus import ReferenceCorpusAssessment
from cti_app.infrastructure.analysis_subprocess import AnalysisSubprocessStatus
from cti_app.infrastructure.capa import CapaRunner, parse_capa_output, ruleset_manifest

CAPA_ANALYSIS_JOB_KIND = "sample.capa.v1"


class CapaAnalysisJobParameters(JobParameters):
    sample_id: UUID


class CapabilitiesService:
    def __init__(
        self,
        blobs: BlobCatalogService,
        uow_factory: UnitOfWorkFactory,
        *,
        rules_path: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        max_memory_bytes: int,
        runner: CapaRunner | None = None,
    ) -> None:
        self._blobs, self._uow_factory = blobs, uow_factory
        self._rules_path = rules_path
        self._timeout = timeout_seconds
        self._output, self._memory = max_output_bytes, max_memory_bytes
        self._runner = runner or CapaRunner()

    async def analyze(self, sample_id: UUID) -> CapabilitySet:
        manifest = ruleset_manifest(self._rules_path)
        parameters = {
            "analysis_capa_timeout_seconds": self._timeout,
            "analysis_capa_max_output_bytes": self._output,
            "analysis_capa_max_memory_bytes": self._memory,
        }
        parameter_hash = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
        async with self._uow_factory() as uow:
            sample = await uow.samples.get(sample_id)
            if sample is None:
                raise EntityNotFoundError(f"Sample {sample_id} does not exist")
            if manifest is not None:
                existing = await uow.capability_sets.get(
                    sample_id, "9.4.0", manifest, parameter_hash
                )
                if existing is not None:
                    return existing
        if manifest is None:
            result = CapabilitySet(
                sample_id=sample_id,
                tool_name="capa",
                tool_version="9.4.0",
                ruleset_sha256="",
                parameters_sha256=parameter_hash,
                status=CapabilitySetStatus.UNAVAILABLE,
                capabilities=(),
                errors=("ruleset unavailable",),
            )
        else:
            payload = await self._blobs.read(sample.blob_id, max_bytes=200 * 1024 * 1024)
            version, execution = await self._runner.run(
                sample=payload,
                rules_path=self._rules_path,
                timeout_seconds=self._timeout,
                output_limit=self._output,
                memory_limit_bytes=self._memory,
            )
            if execution.status is not AnalysisSubprocessStatus.SUCCEEDED:
                status = (
                    CapabilitySetStatus.INVALID_OUTPUT
                    if execution.status is AnalysisSubprocessStatus.OUTPUT_LIMIT
                    else CapabilitySetStatus.UNAVAILABLE
                )
                result = CapabilitySet(
                    sample_id=sample_id,
                    tool_name="capa",
                    tool_version=version,
                    ruleset_sha256=manifest,
                    parameters_sha256=parameter_hash,
                    status=status,
                    capabilities=(),
                    errors=(execution.status.value,),
                )
            else:
                values, errors = parse_capa_output(execution.stdout)
                result = CapabilitySet(
                    sample_id=sample_id,
                    tool_name="capa",
                    tool_version=version,
                    ruleset_sha256=manifest,
                    parameters_sha256=parameter_hash,
                    status=(
                        CapabilitySetStatus.SUCCEEDED
                        if not errors
                        else CapabilitySetStatus.INVALID_OUTPUT
                    ),
                    capabilities=tuple(Capability(**value) for value in values),
                    errors=errors,
                )
        descriptor = await self._blobs.ingest(
            BytesIO(json.dumps(result.as_json(), sort_keys=True).encode()),
            logical_bucket="capability-sets",
            mime_type="application/json",
        )
        async with self._uow_factory() as uow:
            inserted = await uow.capability_sets.add_if_absent(result, descriptor.id)
            if not inserted:
                existing = await uow.capability_sets.get(
                    sample_id, "9.4.0", result.ruleset_sha256, parameter_hash
                )
                if existing is None:
                    raise RuntimeError("capability set conflict without row")
                return existing
            await uow.capability_sets.index(result)
            await uow.commit()
        return result

    async def assess_capability(
        self, rule_id: str, *, min_family_samples: int = 5
    ) -> ReferenceCorpusAssessment:
        """Assess a capability against the real benign and malware corpus."""
        from cti_app.application.reference_corpus import ReferenceCorpusService

        return await ReferenceCorpusService(self._uow_factory).assess(
            feature_kind="capability",
            normalized_value=rule_id,
            min_family_samples=min_family_samples,
        )


def register_capa_analysis_job(registry: JobRegistry, service: CapabilitiesService) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, CapaAnalysisJobParameters):
            raise TypeError("invalid parameters")
        result = await service.analyze(parameters.sample_id)
        await context.report_progress(1, 1, "Analyse CAPA terminée")
        return (
            f"capability-sets://{result.sample_id}/"
            f"{result.parameters_sha256}/{result.ruleset_sha256}"
        )

    registry.register(CAPA_ANALYSIS_JOB_KIND, CapaAnalysisJobParameters, handler)
