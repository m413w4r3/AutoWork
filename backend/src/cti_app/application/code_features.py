from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from typing import Protocol
from uuid import UUID

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.code_features import (
    CodeFeatureSet,
    CodeFeatureStatus,
    CodeFunction,
    PackingSignals,
    build_code_ngrams,
    validate_ngram_sizes,
)
from cti_app.infrastructure.smda import SmdaAdapter, SmdaAdapterResult
from cti_app.infrastructure.static_analysis import build_packing_signals


class BlobIngestor(Protocol):
    async def ingest(
        self, handle: BytesIO, *, logical_bucket: str, mime_type: str
    ) -> BlobRecord: ...

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


class CodeFeatureService:
    def __init__(
        self,
        blobs: BlobIngestor,
        uow_factory: UnitOfWorkFactory,
        smda: SmdaAdapter,
        *,
        tool_version: str = "4.5.0",
        escaper_compatibility_version: str = "4.4.5",
        intel_pic_hash_escape_version: str = "4.3.5",
    ) -> None:
        self._blobs = blobs
        self._uow_factory = uow_factory
        self._smda = smda
        self._tool_version = tool_version
        self._escaper_version = escaper_compatibility_version
        self._pic_version = intel_pic_hash_escape_version

    async def extract(
        self,
        *,
        sample_id: UUID,
        parameters_sha256: str,
        code_ngram_sizes: tuple[int, ...] = (4, 6, 8),
        code_ngram_max_per_sample: int = 100_000,
        analysis_max_sample_bytes: int = 200 * 1024 * 1024,
        smda_timeout_seconds: float = 120.0,
        smda_max_output_bytes: int = 32 * 1024 * 1024,
        smda_max_memory_bytes: int = 1024 * 1024 * 1024,
    ) -> CodeFeatureSet:
        validate_ngram_sizes(code_ngram_sizes)
        async with self._uow_factory() as uow:
            sample = await uow.samples.get(sample_id)
            if sample is None:
                raise ValueError("sample does not exist")
            existing = await uow.code_feature_sets.get(
                sample_id,
                self._tool_version,
                self._escaper_version,
                self._pic_version,
                parameters_sha256,
            )
            if existing is not None:
                return existing

        payload = await self._blobs.read(sample.blob_id, max_bytes=analysis_max_sample_bytes)
        result = await self._smda.extract(
            payload,
            timeout_seconds=smda_timeout_seconds,
            output_limit=smda_max_output_bytes,
            memory_limit_bytes=smda_max_memory_bytes,
        )
        if result.status == "SUCCEEDED" and result.extraction is not None:
            extraction = result.extraction
            async with self._uow_factory() as uow:
                existing = await uow.code_feature_sets.get(
                    sample_id,
                    extraction.smda_version,
                    extraction.escaper_compatibility_version,
                    extraction.intel_pic_hash_escape_version,
                    parameters_sha256,
                )
                if existing is not None:
                    return existing
            feature_set = await self._build_success(
                sample_id=sample_id,
                blob_id=sample.blob_id,
                payload=payload,
                parameters_sha256=parameters_sha256,
                result=result,
                code_ngram_sizes=code_ngram_sizes,
                code_ngram_max_per_sample=code_ngram_max_per_sample,
            )
        else:
            feature_set = CodeFeatureSet(
                sample_id=sample_id,
                blob_id=sample.blob_id,
                tool_version=self._tool_version,
                escaper_compatibility_version=self._escaper_version,
                intel_pic_hash_escape_version=self._pic_version,
                parameters_sha256=parameters_sha256,
                architecture="UNKNOWN",
                status=CodeFeatureStatus(result.status),
                ngrams=(),
                packing=_packing_signals(payload, ()),
                errors=(result.error,) if result.error else (),
            )
        payload = json.dumps(feature_set.as_json(), separators=(",", ":"), sort_keys=True).encode()
        feature_blob = await self._blobs.ingest(
            BytesIO(payload), logical_bucket="code-feature-sets", mime_type="application/json"
        )
        async with self._uow_factory() as uow:
            inserted = await uow.code_feature_sets.add_if_absent(feature_set, feature_blob.id)
            if inserted:
                await uow.code_feature_sets.index(feature_set)
                await uow.commit()
            else:
                existing = await uow.code_feature_sets.get(
                    sample_id,
                    feature_set.tool_version,
                    feature_set.escaper_compatibility_version,
                    feature_set.intel_pic_hash_escape_version,
                    parameters_sha256,
                )
                if existing is not None:
                    return existing
        return replace(feature_set, feature_blob_id=feature_blob.id)

    async def _build_success(
        self,
        *,
        sample_id: UUID,
        blob_id: UUID,
        payload: bytes,
        parameters_sha256: str,
        result: SmdaAdapterResult,
        code_ngram_sizes: tuple[int, ...],
        code_ngram_max_per_sample: int,
    ) -> CodeFeatureSet:
        assert result.extraction is not None
        extraction = result.extraction
        ngrams = build_code_ngrams(
            extraction.functions,
            code_ngram_sizes,
            max_per_sample=code_ngram_max_per_sample,
        )
        return CodeFeatureSet(
            sample_id=sample_id,
            blob_id=blob_id,
            tool_version=extraction.smda_version,
            escaper_compatibility_version=extraction.escaper_compatibility_version,
            intel_pic_hash_escape_version=extraction.intel_pic_hash_escape_version,
            parameters_sha256=parameters_sha256,
            architecture=extraction.architecture,
            status=CodeFeatureStatus.SUCCEEDED,
            ngrams=ngrams,
            packing=_packing_signals(payload, extraction.functions),
        )


def _packing_signals(payload: bytes, functions: tuple[CodeFunction, ...]) -> PackingSignals:
    return build_packing_signals(payload, len(functions))


__all__ = ["CodeFeatureService"]
