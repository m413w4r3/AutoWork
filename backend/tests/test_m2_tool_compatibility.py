import os
import re
import tempfile
from pathlib import Path

import pytest

from cti_app.domain.code_features import (
    CodeFunction,
    CodeInstruction,
    build_code_ngrams,
)
from cti_app.infrastructure.smda import SmdaAdapter
from tests.fixtures.binary_factory import build_pe64


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


@pytest.mark.asyncio
async def test_smda_adapter_materializes_canonical_bytes(tmp_path: Path) -> None:
    observed: dict[str, bytes] = {}

    async def runner(argv, **kwargs):
        sample_path = Path(argv[-1])
        observed["payload"] = _read_bytes(sample_path)
        assert os.path.isabs(argv[-1])
        from cti_app.infrastructure.analysis_subprocess import (
            AnalysisSubprocessResult,
            AnalysisSubprocessStatus,
        )

        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.NON_ZERO_EXIT, 1, b"", b""
        )

    payload = build_pe64()
    source = tmp_path / "canonical.bin"
    source.write_bytes(payload)
    result = await SmdaAdapter(runner=runner).extract(
        source.read_bytes(), timeout_seconds=1, output_limit=1000, memory_limit_bytes=1000
    )
    assert result.status == "UNAVAILABLE"
    assert observed["payload"] == payload


def test_ngrams_aggregate_patterns_and_keep_the_first_occurrence() -> None:
    def function(offset: int) -> CodeFunction:
        return CodeFunction(
            offset=offset,
            instructions=(
                CodeInstruction(
                    offset=offset,
                    bytes=b"\x90",
                    mnemonic="nop",
                    escaped_bytes=(0x90,),
                ),
                CodeInstruction(
                    offset=offset + 1,
                    bytes=b"\xc3",
                    mnemonic="ret",
                    escaped_bytes=(0xC3,),
                ),
            ),
        )

    ngrams = build_code_ngrams((function(0x2000), function(0x3000)), (2,), max_per_sample=1)
    assert len(ngrams) == 1
    assert ngrams[0].pattern == "90 c3"
    assert ngrams[0].occurrence_count == 2
    assert ngrams[0].function_offset == 0x2000
    assert ngrams[0].start_offset == 0x2000


@pytest.mark.asyncio
async def test_real_smda_45_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("smda")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    source = tmp_path / "smoke.exe"
    source.write_bytes(build_pe64())
    result = await SmdaAdapter().extract(
        source.read_bytes(),
        timeout_seconds=120,
        output_limit=32 * 1024 * 1024,
        memory_limit_bytes=1024 * 1024 * 1024,
    )

    assert result.status == "SUCCEEDED", result.error
    assert result.extraction is not None
    assert result.extraction.smda_version == "4.5.0"
    assert result.extraction.escaper_compatibility_version == "4.4.5"
    assert result.extraction.intel_pic_hash_escape_version == "4.3.5"
    assert result.extraction.architecture == "x64"
    assert result.extraction.functions
    instructions = [
        instruction
        for function in result.extraction.functions
        for instruction in function.instructions
    ]
    assert instructions
    assert all(
        len(instruction.escaped_bytes) == len(instruction.bytes) for instruction in instructions
    )
    ngrams = build_code_ngrams(
        result.extraction.functions, (2,), max_per_sample=100
    )
    assert ngrams
    assert re.fullmatch(
        r"(?:[0-9a-f]{2}|\?\?)(?: (?:[0-9a-f]{2}|\?\?))+", ngrams[0].pattern
    )
