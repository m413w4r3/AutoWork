import json

import pytest

from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
)
from cti_app.infrastructure.smda import SmdaAdapter, parse_smda_output


def _output(architecture: str = "intel.64bit") -> dict:
    return {
        "smda_version": "4.5.0",
        "escaper_compatibility_version": "4.4.5",
        "intel_pic_hash_escape_version": "4.3.5",
        "architecture": architecture,
        "functions": [
            {
                "offset": 4096,
                "basic_blocks": [
                    {
                        "offset": 4096,
                        "instructions": [
                            {
                                "offset": 4096,
                                "bytes": "488b",
                                "mnemonic": "mov",
                                "escaped_bytes": [72, 139],
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_fake_runner_success_and_unsupported_architecture() -> None:
    async def runner(*args, **kwargs) -> AnalysisSubprocessResult:
        return AnalysisSubprocessResult(
            status=AnalysisSubprocessStatus.SUCCEEDED,
            exit_code=0,
            stdout=json.dumps(_output()).encode(),
            stderr=b"",
        )

    adapter = SmdaAdapter(runner=runner)
    result = await adapter.extract(
        b"sample", timeout_seconds=1, output_limit=1000, memory_limit_bytes=1000
    )
    assert result.status == "SUCCEEDED"
    assert result.extraction is not None
    assert result.extraction.architecture == "x64"

    async def unsupported(*args, **kwargs) -> AnalysisSubprocessResult:
        return AnalysisSubprocessResult(
            status=AnalysisSubprocessStatus.SUCCEEDED,
            exit_code=0,
            stdout=json.dumps(_output("aarch64")).encode(),
            stderr=b"",
        )

    result = await SmdaAdapter(runner=unsupported).extract(
        b"sample", timeout_seconds=1, output_limit=1000, memory_limit_bytes=1000
    )
    assert result.status == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_fake_runner_maps_output_errors_to_invalid_output() -> None:
    async def runner(*args, **kwargs) -> AnalysisSubprocessResult:
        return AnalysisSubprocessResult(
            status=AnalysisSubprocessStatus.OUTPUT_LIMIT,
            exit_code=0,
            stdout=b"",
            stderr=b"",
        )

    result = await SmdaAdapter(runner=runner).extract(
        b"sample", timeout_seconds=1, output_limit=1, memory_limit_bytes=1000
    )
    assert result.status == "INVALID_OUTPUT"


def test_parser_rejects_non_minimal_output() -> None:
    data = _output()
    data["extra"] = True
    with pytest.raises(ValueError):
        parse_smda_output(data)


def test_pinned_smda_public_api_is_importable() -> None:
    pytest.importorskip("smda")
    from smda.common import SmdaFunction
    from smda.Disassembler import Disassembler, SmdaReport
    from smda.SmdaConfig import SmdaConfig

    assert Disassembler is not None
    assert SmdaConfig.ESCAPER_DOWNWARD_COMPATIBILITY
    assert SmdaFunction.INTEL_PIC_HASH_ESCAPE_VERSION
    assert hasattr(SmdaReport, "getInstructionEscaper")
