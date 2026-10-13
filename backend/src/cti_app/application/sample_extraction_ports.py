from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from cti_app.domain.configuration_extraction import ConfigurationExtractionV1
from cti_app.domain.string_extraction import StringExtractionV1


class ObfuscatedStringExtractionPort(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def version(self) -> str | None: ...

    async def extract(self, sample_sha256: str, *, max_strings: int) -> StringExtractionV1: ...


class ConfigurationExtractionPort(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def allowed_parsers(self) -> Mapping[str, str]: ...

    async def extract(self, sample_sha256: str, *, parser_id: str) -> ConfigurationExtractionV1: ...
