from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.config import Settings
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
)
from cti_app.workers import dotnet_analysis as dotnet_analysis_module
from cti_app.workers.confined_analysis import _execute_tool
from cti_app.workers.dotnet_analysis import (
    _inspect_cli_image,
    _method_records,
    _obfuscation_likelihood,
    _try_ilspy_decompile,
    _type_records,
    run_dotnet_analysis,
)
from tests.dotnet_fixture import MINIMAL_DOTNET_ASSEMBLY

_MINIMAL_DOTNET_ASSEMBLY = MINIMAL_DOTNET_ASSEMBLY


def _extract_monodis_method_body(output: str, rid: int) -> str | None:
    lines = output.splitlines(keepends=True)
    marker = f"// method line {rid}"
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == marker)
    except StopIteration:
        return None
    for end in range(start + 1, len(lines)):
        if lines[end].lstrip().startswith("} // end of method "):
            return "".join(lines[start : end + 1])
        if lines[end].startswith("// method line "):
            return None
    return None


def _parameters(operation: str, **values: str) -> ConfinedAnalysisJobParameters:
    return ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=hashlib.sha256(_MINIMAL_DOTNET_ASSEMBLY).hexdigest(),
        operation_key="confined-analysis:" + "a" * 64,
        tool=ConfinedAnalysisTool.DOTNET_ILSPY,
        timeout_seconds=30,
        max_output_bytes=16_384,
        analysis_parameters={"operation": operation, **values},
    )


def test_dotnet_fixture_has_stable_cli_identity_and_architecture() -> None:
    metadata = _inspect_cli_image(_MINIMAL_DOTNET_ASSEMBLY)

    assert metadata is not None
    assert metadata["format"] == "PE/CLI (.NET metadata; CLR/Mono-compatible)"
    assert metadata["architecture"] == "x86"
    assert metadata["metadata_signature"] == "BSJB"
    assert metadata["code_execution"] is False
    assert metadata["managed_analysis_path"].startswith("ilspycmd")


def test_invalid_assembly_fails_cleanly_without_invoking_a_tool() -> None:
    async def must_not_run() -> None:
        result = await run_dotnet_analysis(
            _parameters("methods"),
            b"not a PE or CLI assembly",
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("monodis must not run for invalid input"),
            extract_monodis_method_body=_extract_monodis_method_body,
        )
        assert result["schema_version"] == "dotnet-analysis-result.v1"
        assert result["status"] == "FAILED"
        assert result["available_reason"] == "dotnet_invalid_or_missing_cli_metadata"

    import asyncio

    asyncio.run(must_not_run())


def test_confined_worker_dispatches_dotnet_tool_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    calls: list[tuple[ConfinedAnalysisJobParameters, bytes, Settings]] = []

    async def fake_dotnet_analysis(
        parameters: ConfinedAnalysisJobParameters,
        payload: bytes,
        settings: Settings,
        **_kwargs: object,
    ) -> dict[str, str]:
        calls.append((parameters, payload, settings))
        return {"schema_version": "dotnet-analysis-result.v1", "status": "SUCCEEDED"}

    monkeypatch.setattr(
        "cti_app.workers.confined_analysis.run_dotnet_analysis", fake_dotnet_analysis
    )
    parameters = _parameters("methods")
    settings = Settings(_env_file=None)
    payload = _MINIMAL_DOTNET_ASSEMBLY

    result = asyncio.run(
        _execute_tool(
            parameters,
            sample=object(),
            payload=payload,
            blob_id=uuid4(),
            blobs=None,  # type: ignore[arg-type]
            uow_factory=lambda: None,  # type: ignore[arg-type]
            settings=settings,
        )
    )

    assert result == {"schema_version": "dotnet-analysis-result.v1", "status": "SUCCEEDED"}
    assert calls == [(parameters, payload, settings)]


@pytest.mark.asyncio
async def test_ilspy_subprocess_uses_dedicated_dotnet_memory_and_gc_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    il = (
        ".method public hidebysig static int32 Fixture::Main() cil managed\n"
        "{\n  IL_0000: ldc.i4.7\n  IL_0001: ret\n"
        "} // end of method Fixture::Main\n"
    )

    async def fake_run_analysis_subprocess(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.SUCCEEDED,
            0,
            il.encode(),
            b"",
        )

    monkeypatch.setattr(
        dotnet_analysis_module, "run_analysis_subprocess", fake_run_analysis_subprocess
    )
    parameters = _parameters("decompile", method_rid="1")
    settings = Settings(
        _env_file=None,
        dotnet_analysis_address_space_limit_bytes=9 * 1024**3,
    )
    selected = {"rid": 1, "type_name": "Fixture", "method_name": "Main"}

    result = await _try_ilspy_decompile(
        "/usr/local/bin/ilspycmd",
        _MINIMAL_DOTNET_ASSEMBLY,
        selected,
        [selected],
        parameters,
        settings,
    )

    assert isinstance(result, dict)
    assert "ldc.i4.7" in result["stdout"]
    kwargs = calls["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["memory_limit_bytes"] == 9 * 1024**3
    assert kwargs["timeout_seconds"] <= 45
    environment = kwargs["environment"]
    assert isinstance(environment, dict)
    assert environment["DOTNET_GCHeapHardLimit"] == "0x10000000"
    assert environment["DOTNET_gcConcurrent"] == "0"


def test_elf_routes_to_native_analysis_without_calling_dotnet_tools() -> None:
    import asyncio

    result = asyncio.run(
        run_dotnet_analysis(
            _parameters("assembly"),
            b"\x7fELF" + bytes(64),
            Settings(_env_file=None),
            monodis_version=lambda: pytest.fail("monodis must not run for ELF input"),
            extract_monodis_method_body=_extract_monodis_method_body,
        )
    )

    assert result["status"] == "POLICY_DENIED"
    assert result["file_format"] == "ELF"
    assert result["recommended_native_tool"] == "native.inspect"


def test_method_and_type_rows_expose_metadata_tokens_and_rids() -> None:
    methods, methods_complete = _method_records(
        "Method Table (1..2)\n"
        "########## .Fixture\n"
        "1: default int32 Main ()  (param: 1 impl_flags: cil managed )\n"
        "2: default void Helper ()  (param: 1 impl_flags: cil managed )\n"
    )
    types, types_complete = _type_records(
        "Typedef Table\n"
        "1: (null) (flist=1, mlist=1, flags=0x0, extends=0x0)\n"
        "2: Fixture (flist=1, mlist=1, flags=0x100181, extends=0x5)\n"
    )

    assert methods_complete and types_complete
    assert methods == [
        {
            "rid": 1,
            "token": "0x06000001",
            "type_name": "Fixture",
            "method_name": "Main",
            "signature": "default int32 Main ()",
        },
        {
            "rid": 2,
            "token": "0x06000002",
            "type_name": "Fixture",
            "method_name": "Helper",
            "signature": "default void Helper ()",
        },
    ]
    assert types == [{"rid": 2, "token": "0x02000002", "type_name": "Fixture"}]


def test_obfuscation_label_is_a_bounded_name_heuristic() -> None:
    assert not _obfuscation_likelihood([{"method_name": "a"}] * 9)
    assert _obfuscation_likelihood([{"method_name": "a"}] * 10)
    assert not _obfuscation_likelihood([{"method_name": "Initialize"}] * 10)


def test_dotnet_request_selects_methods_only_by_stable_rid() -> None:
    request = ConfinedAnalysisRequest(
        investigation_id=uuid4(),
        association_id=uuid4(),
        sample_id=uuid4(),
        artifact_id=uuid4(),
        sha256="b" * 64,
        artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
        tool=ConfinedAnalysisTool.DOTNET_ILSPY,
        timeout_seconds=30,
        max_output_bytes=16_384,
        analysis_parameters={"operation": "decompile", "method_rid": "1"},
    )

    assert request.analysis_parameters["method_rid"] == "1"
    with pytest.raises(ValueError, match="method RID is required"):
        ConfinedAnalysisRequest(
            investigation_id=uuid4(),
            association_id=uuid4(),
            sample_id=uuid4(),
            artifact_id=uuid4(),
            sha256="b" * 64,
            artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
            tool=ConfinedAnalysisTool.DOTNET_ILSPY,
            timeout_seconds=30,
            max_output_bytes=16_384,
            analysis_parameters={"operation": "decompile"},
        )
