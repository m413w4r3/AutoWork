from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.code_features import (
    CodeFunction,
    CodeInstruction,
    GoodwareVerdict,
    apply_corpus_assessment,
    build_code_ngrams,
    compare_goodware,
    escaped_pattern,
    opcode_fragment16_lookup_value,
    validate_ngram_sizes,
)
from cti_app.domain.reference_corpus import assess_reference_feature
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


def test_goodware_is_exact_and_only_for_unmasked_8_to_16_bytes() -> None:
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
    assert compare_goodware(ngram, 2).goodware_verdict is GoodwareVerdict.PRESENT
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
    assert compare_goodware(masked, 2).goodware_verdict is GoodwareVerdict.UNKNOWN
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


def test_corpus_assessment_preserves_small_and_unknown_states() -> None:
    ngram = build_code_ngrams(
        (CodeFunction(offset=0, instructions=tuple(
            CodeInstruction(offset=i, bytes=b"\x90", mnemonic="nop", escaped_bytes=(0x90,))
            for i in range(8)
        )),),
        (8,),
        max_per_sample=1,
    )[0]
    assessment = assess_reference_feature(
        feature_kind="code_ngram",
        normalized_value=ngram.pattern,
        malware_members=((uuid4(), "luna"),),
        benign_sample_occurrences=0,
        total_eligible_samples_by_family={"luna": 1},
    )
    assert apply_corpus_assessment(ngram, assessment).corpus_verdict == "CORPUS_TOO_SMALL"


class _FakeReferenceMembers:
    async def count_eligible_malware_samples_by_family(self) -> dict[str, int]:
        return {}


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
    def __init__(self, blob_id, code_feature_sets: _FakeCodeFeatureSets) -> None:
        self.samples = SimpleNamespace(
            get=self._get_sample,
        )
        self.reference_members = _FakeReferenceMembers()
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
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, payload: bytes, **kwargs) -> SmdaAdapterResult:
        self.calls += 1
        return SmdaAdapterResult(
            status="SUCCEEDED",
            extraction=SmdaExtraction(
                smda_version="4.5.0",
                escaper_compatibility_version="4.4.5",
                intel_pic_hash_escape_version="4.3.5",
                architecture="x64",
                functions=(),
            ),
        )


@pytest.mark.asyncio
async def test_extract_idempotence_uses_persisted_smda_versions_before_adapter() -> None:
    from cti_app.application.code_features import CodeFeatureService

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
