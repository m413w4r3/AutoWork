from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from cti_app.application.code_features import CodeFeatureService
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.code_features import (
    CodeFunction,
    CodeInstruction,
    build_code_ngrams,
    escaped_pattern,
    opcode_fragment16_lookup_value,
    validate_ngram_sizes,
)
from cti_app.infrastructure.database.models.core import CodeFeatureSetRow
from cti_app.infrastructure.database.repositories.core import _code_feature_set_from_row
from cti_app.infrastructure.smda import SmdaAdapterResult, SmdaExtraction


def _function() -> CodeFunction:
    return CodeFunction(
        offset=0x1000,
        instructions=(
            CodeInstruction(
                offset=0x1000,
                bytes=b"\x48\x8b",
                mnemonic="mov",
                escaped_bytes=(0x48, 0x8B),
            ),
            CodeInstruction(offset=0x1002, bytes=b"\x89", mnemonic="mov", escaped_bytes=(0x89,)),
            CodeInstruction(offset=0x1003, bytes=b"\x90", mnemonic="nop", escaped_bytes=(None,)),
            CodeInstruction(offset=0x2000, bytes=b"\xc3", mnemonic="ret", escaped_bytes=(0xC3,)),
        ),
    )


def test_escaped_bytes_are_canonical_and_masking_is_not_recomputed() -> None:
    assert escaped_pattern((0x48, "8B", "??", None)) == "48 8b ?? ??"


def test_ngrams_require_instruction_and_address_continuity() -> None:
    ngrams = build_code_ngrams((_function(),), (2,), max_per_sample=100)
    assert [(item.start_offset, item.byte_count) for item in ngrams] == [(0x1000, 3), (0x1002, 2)]
    assert ngrams[1].pattern == "89 ??"
    assert ngrams[1].masked_byte_count == 1


def test_ngram_sizes_are_unique_sorted_and_at_least_two() -> None:
    assert validate_ngram_sizes((4, 6, 8)) == (4, 6, 8)
    with pytest.raises(ValueError):
        validate_ngram_sizes((4, 4))
    with pytest.raises(ValueError):
        validate_ngram_sizes((3, 2))
    with pytest.raises(ValueError):
        validate_ngram_sizes((1,))


def test_ngrams_contain_structural_fields_only() -> None:
    raw = CodeInstruction(
        offset=0,
        bytes=b"\x90",
        mnemonic="x",
        escaped_bytes=(0,),
    )
    ngram = build_code_ngrams(
        (
            CodeFunction(
                offset=0,
                instructions=tuple(
                    replace_instruction_at(raw, index, index) for index in range(8)
                ),
            ),
        ),
        (8,),
        max_per_sample=1,
    )[0]
    assert opcode_fragment16_lookup_value(ngram) == "0001020304050607"
    masked = build_code_ngrams(
        (
            CodeFunction(
                offset=0,
                instructions=tuple(
                    replace_instruction_at(raw, index, None if index == 7 else index)
                    for index in range(8)
                ),
            ),
        ),
        (8,),
        max_per_sample=1,
    )[0]
    assert opcode_fragment16_lookup_value(masked) is None


def replace_instruction(instruction: CodeInstruction, token: object) -> CodeInstruction:
    return CodeInstruction(
        offset=instruction.offset,
        bytes=instruction.bytes,
        mnemonic=instruction.mnemonic,
        escaped_bytes=(token,) * len(instruction.bytes),
    )


def replace_instruction_at(
    instruction: CodeInstruction, offset: int, token: object
) -> CodeInstruction:
    return CodeInstruction(
        offset=offset,
        bytes=instruction.bytes,
        mnemonic=instruction.mnemonic,
        escaped_bytes=(token,) * len(instruction.bytes),
    )


class _FakeCodeFeatureSets:
    def __init__(self) -> None:
        self.stored = None

    async def get(self, *args: object):
        return self.stored

    async def add_if_absent(self, feature_set, feature_blob_id) -> bool:
        self.stored = replace(feature_set, feature_blob_id=feature_blob_id)
        return True

    async def index(self, feature_set) -> None:
        return None


class _FakeUow:
    def __init__(
        self,
        blob_id,
        code_feature_sets: _FakeCodeFeatureSets,
    ) -> None:
        self.samples = SimpleNamespace(
            get=self._get_sample,
        )
        self.code_feature_sets = code_feature_sets
        self._blob_id = blob_id
        self.commits = 0

    async def _get_sample(self, sample_id):
        return SimpleNamespace(blob_id=self._blob_id)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def commit(self) -> None:
        self.commits += 1


class _FakeBlobs:
    def __init__(self, blob: BlobRecord) -> None:
        self.blob = blob
        self.read_calls = 0
        self.ingest_calls = 0

    async def read(self, blob_id, *, max_bytes):
        self.read_calls += 1
        return b"payload"

    async def ingest(self, handle: BytesIO, *, logical_bucket: str, mime_type: str):
        self.ingest_calls += 1
        handle.read()
        return self.blob


class _FakeSmda:
    def __init__(self, functions=()) -> None:
        self.calls = 0
        self.functions = functions

    async def extract(self, payload: bytes, **kwargs) -> SmdaAdapterResult:
        self.calls += 1
        return SmdaAdapterResult(
            status="SUCCEEDED",
            extraction=SmdaExtraction(
                smda_version="4.5.0",
                escaper_compatibility_version="4.4.5",
                intel_pic_hash_escape_version="4.3.5",
                architecture="x64",
                functions=self.functions,
            ),
        )


def _eight_byte_function() -> CodeFunction:
    return CodeFunction(
        offset=0,
        instructions=tuple(
            CodeInstruction(
                offset=index,
                bytes=b"\x90",
                mnemonic="nop",
                escaped_bytes=(index,),
            )
            for index in range(8)
        ),
    )


@pytest.mark.asyncio
async def test_extract_idempotence_uses_persisted_smda_versions_before_adapter() -> None:
    sample_id = uuid4()
    blob = BlobRecord(
        descriptor=BlobDescriptor(
            sha256="a" * 64,
            size=7,
            mime_type="application/octet-stream",
            logical_bucket="samples",
        )
    )
    blobs = _FakeBlobs(blob)
    smda = _FakeSmda()
    code_feature_sets = _FakeCodeFeatureSets()
    uow = _FakeUow(blob.id, code_feature_sets)
    service = CodeFeatureService(blobs, lambda: uow, smda)

    first = await service.extract(sample_id=sample_id, parameters_sha256="parameters")
    second = await service.extract(sample_id=sample_id, parameters_sha256="parameters")

    assert second == first
    assert smda.calls == 1
    assert blobs.read_calls == 1
    assert blobs.ingest_calls == 1


def test_new_code_feature_json_has_structural_ngram_fields_only() -> None:
    feature = _code_feature_set_from_row(
        cast(CodeFeatureSetRow, SimpleNamespace(
            id=uuid4(), sample_id=uuid4(), blob_id=uuid4(), feature_blob_id=uuid4(),
            tool_version="smda", escaper_compatibility_version="escape",
            intel_pic_hash_escape_version="pic", parameters_sha256="parameters",
            architecture="x64", status="SUCCEEDED",
            payload={
                "ngrams": [{
                    "pattern": "90 90", "instruction_count": 2, "byte_count": 2,
                    "fixed_byte_count": 2, "masked_byte_count": 0,
                    "longest_fixed_run": 2, "function_offset": 0,
                    "start_offset": 0, "mnemonics": ["nop", "nop"],
                    "occurrence_count": 1,
                }],
                "packing": {
                    "max_executable_section_entropy": None, "executable_bytes": 0,
                    "recovered_function_count": 0, "executable_bytes_per_function": None,
                    "known_packer_marker_hits": [],
                },
            }, errors=[],
        ))
    )
    assert set(feature.as_json()["ngrams"][0]) == {
        "pattern", "instruction_count", "byte_count", "fixed_byte_count",
        "masked_byte_count", "longest_fixed_run", "function_offset", "start_offset",
        "mnemonics", "occurrence_count",
    }


def test_legacy_code_feature_payload_ignores_assessment_fields() -> None:
    row = SimpleNamespace(
        id=uuid4(), sample_id=uuid4(), blob_id=uuid4(), feature_blob_id=uuid4(),
        tool_version="smda", escaper_compatibility_version="escape",
        intel_pic_hash_escape_version="pic", parameters_sha256="parameters",
        architecture="x64", status="SUCCEEDED",
        payload={
            "ngrams": [{
                "pattern": "90 90", "instruction_count": 2, "byte_count": 2,
                "fixed_byte_count": 2, "masked_byte_count": 0, "longest_fixed_run": 2,
                "function_offset": 0, "start_offset": 0, "mnemonics": ["nop", "nop"],
                "occurrence_count": 1, "goodware_verdict": "PRESENT",
                "corpus_verdict": "FAMILY_SPECIFIC", "goodware_occurrence_count": 4,
            }],
            "packing": {
                "max_executable_section_entropy": None, "executable_bytes": 0,
                "recovered_function_count": 0, "executable_bytes_per_function": None,
                "known_packer_marker_hits": [],
            },
        }, errors=[],
    )
    feature = _code_feature_set_from_row(cast(CodeFeatureSetRow, row))
    assert feature.ngrams[0].pattern == "90 90"
    assert "goodware_verdict" not in feature.as_json()["ngrams"][0]
