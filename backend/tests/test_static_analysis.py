from uuid import uuid4

from cti_app.domain.classification import TLP
from cti_app.infrastructure.static_analysis import StaticFeatureExtractor
from tests.fixtures.binary_factory import (
    FIXTURE_ASCII_STRING,
    RICH_CLEAR_MD5,
    build_elf64,
    build_pe64,
    build_rtf,
    build_truncated_pe,
    build_unknown,
    expected_opcode_fragments,
)


def _extract(payload: bytes):
    return StaticFeatureExtractor().extract(
        sample_id=uuid4(),
        blob_id=uuid4(),
        payload=payload,
        parameters_sha256="a" * 64,
        tlp=TLP.GREEN,
        do_not_submit=False,
        external_llm_allowed=True,
        max_strings=100,
        min_string_length=4,
    )


def test_static_formats_hashes_strings_and_fragments() -> None:
    pe = _extract(build_pe64())
    assert pe.format.value == "PE" and pe.rich_header_hash == RICH_CLEAR_MD5
    assert any(item["value"] == FIXTURE_ASCII_STRING.decode() for item in pe.strings)
    assert pe.opcode_fragment16 == expected_opcode_fragments() and pe.sections
    assert _extract(build_elf64()).format.value == "ELF"
    assert _extract(build_rtf()).format.value == "RTF"
    assert _extract(build_unknown()).format.value == "UNKNOWN"
    assert _extract(build_truncated_pe()).partial_errors
