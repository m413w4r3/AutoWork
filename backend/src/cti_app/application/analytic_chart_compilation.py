"""Application port and result for deterministic editorial chart compilation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cti_app.domain.production_editorial_enrichment import ChartSpecV1


class AnalyticChartCompilationError(Exception):
    def __init__(self, code: str = "editorial_enrichment_chart_render_failed") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CompiledChart:
    chart_key: str
    media_type: str
    media_bytes: bytes
    media_sha256: str
    compiler: str
    compiler_version: str
    compiler_policy_version: str


class AnalyticChartCompiler(Protocol):
    async def compile(self, chart: ChartSpecV1) -> CompiledChart: ...
