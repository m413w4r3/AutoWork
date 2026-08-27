from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.classification import TLP


class SampleFormat(StrEnum):
    PE = "PE"
    ELF = "ELF"
    RTF = "RTF"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleFeatureSetV1:
    sample_id: UUID
    blob_id: UUID
    extractor_version: str
    parameters_sha256: str
    format: SampleFormat
    size: int
    md5: str
    sha1: str
    sha256: str
    strings: tuple[dict[str, Any], ...]
    sections: tuple[dict[str, Any], ...]
    imports: tuple[str, ...]
    exports: tuple[str, ...]
    resources: tuple[dict[str, Any], ...]
    signature: dict[str, Any] | None
    imphash: str | None
    ssdeep: str | None
    tlsh: str | None
    rich_header_hash: str | None
    opcode_fragment16: tuple[str, ...]
    partial_errors: tuple[str, ...]
    tlp: TLP
    do_not_submit: bool
    external_llm_allowed: bool

    def as_json(self) -> dict[str, Any]:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["sample_id"] = str(self.sample_id)
        data["blob_id"] = str(self.blob_id)
        data["format"] = self.format.value
        data["tlp"] = self.tlp.value
        for key in ("strings", "sections", "imports", "exports", "resources", "opcode_fragment16", "partial_errors"):
            data[key] = list(data[key])
        return data
