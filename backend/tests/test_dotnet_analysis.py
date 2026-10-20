from __future__ import annotations

import hashlib
from pathlib import Path
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
    _extract_ilspy_method,
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
        assert result["schema_version"] == "dotnet-analysis-result.v3"
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
        return {"schema_version": "dotnet-analysis-result.v3", "status": "SUCCEEDED"}

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

    assert result == {"schema_version": "dotnet-analysis-result.v3", "status": "SUCCEEDED"}
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
    assert not _obfuscation_likelihood([{"method_name": "a"}] * 19)
    assert _obfuscation_likelihood([{"method_name": "a"}] * 20)
    assert not _obfuscation_likelihood([{"method_name": "Initialize"}] * 20)


def test_ilspy_extracts_methods_from_benign_nested_unicode_fixture() -> None:
    fixture = Path(__file__).parent / "fixtures/dotnet/nested_unicode"
    assembly = (fixture / "NestedUnicodeFixture.dll").read_bytes()
    il = (fixture / "NestedUnicodeFixture.il").read_text(encoding="utf-8")

    metadata = _inspect_cli_image(assembly)
    assert metadata is not None
    assert metadata["code_execution"] is False
    outer_method = _extract_ilspy_method(
        il,
        "BenignFixtures.Outer",
        "OuterAnswer",
        0,
    )
    nested_overload = _extract_ilspy_method(il, "Inner", "Answer", 1)
    nested_method = _extract_ilspy_method(il, "Inner", "대답", 0)

    assert outer_method is not None
    assert "IL_0000: ldc.i4.s 42" in outer_method
    assert nested_overload is not None
    assert "IL_0002: add" in nested_overload
    assert nested_method is not None
    assert "IL_0002: add" in nested_method


def test_ilspy_metadata_maps_nested_unicode_methods_by_typedef_ranges() -> None:
    fixture = Path(__file__).parent / "fixtures/dotnet/nested_unicode"
    il = (fixture / "NestedUnicodeFixture.il").read_text(encoding="utf-8")
    typedef = (
        "Typedef Table\n"
        "1: (null) (flist=1, mlist=1, flags=0x0, extends=0x0)\n"
        "2: BenignFixtures.Outer (flist=1, mlist=1, flags=0x100181, extends=0x35)\n"
        "3: BenignFixtures.Outer/Inner (flist=1, mlist=2, flags=0x100182, extends=0x35)\n"
    )

    types, methods = dotnet_analysis_module._parse_ilspy_metadata(il, typedef)

    assert [item["type_name"] for item in types] == [
        "BenignFixtures.Outer",
        "BenignFixtures.Outer/Inner",
    ]
    assert [(item["rid"], item["method_name"]) for item in methods] == [
        (1, "OuterAnswer"),
        (2, "Answer"),
        (3, "Answer"),
        (4, "대답"),
    ]


@pytest.mark.asyncio
async def test_types_list_repairs_empty_monodis_names_from_ilspy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Path(__file__).parent / "fixtures/dotnet/nested_unicode"
    il = (fixture / "NestedUnicodeFixture.il").read_text(encoding="utf-8")
    typedef = (
        "Typedef Table\n"
        "1: (null) (flist=1, mlist=1, flags=0x0, extends=0x0)\n"
        "2:  (flist=1, mlist=1, flags=0x100181, extends=0x35)\n"
        "3:  (flist=1, mlist=2, flags=0x100182, extends=0x35)\n"
    )

    async def fake_monodis(_payload, _operations, _parameters, _settings, *, monodis_version):
        return {"status": "SUCCEEDED", "version": "2.0", "stdout": typedef}

    async def fake_subprocess(_argv, **_kwargs):
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, il.encode(), b"")

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)
    monkeypatch.setattr(dotnet_analysis_module, "run_analysis_subprocess", fake_subprocess)
    monkeypatch.setattr(dotnet_analysis_module, "ilspycmd_provenance", lambda **_kwargs: object())
    monkeypatch.setattr(
        dotnet_analysis_module.shutil,
        "which",
        lambda name: "/usr/local/bin/ilspycmd" if name == "ilspycmd" else None,
    )

    result = await run_dotnet_analysis(
        _parameters("types"),
        _MINIMAL_DOTNET_ASSEMBLY,
        Settings(_env_file=None),
        monodis_version=lambda: "2.0",
        extract_monodis_method_body=_extract_monodis_method_body,
    )

    assert result["engine"] == "ilspycmd"
    assert [record["type_name"] for record in result["records"]] == [
        "BenignFixtures.Outer",
        "BenignFixtures.Outer/Inner",
    ]


def test_method_table_parses_quoted_unicode_whitespace_identifiers() -> None:
    methods, complete = _method_records(
        "Method Table (1..2)\n"
        "########## \u00a0.\u00a0\n"
        "1: instance default void '.ctor' ()  (param: 1 impl_flags: cil managed )\n"
        "2: instance default string '\u00a0' ()  (param: 1 impl_flags: cil managed )\n"
    )

    assert complete
    assert len(methods) == 2
    assert all(record["type_name"] for record in methods)
    assert methods[1]["method_name"] == "\u00a0"


def test_obfuscation_heuristic_detects_repeated_hangul_but_not_readable_korean() -> None:
    renamed = [{"method_name": "쉥"}] * 30 + [
        {"method_name": f"LoadResource{index}"} for index in range(10)
    ]
    localized = [
        {"method_name": name}
        for name in (
            "초기화",
            "연결",
            "분석",
            "가져오기",
            "설정",
            "읽기",
            "쓰기",
            "검증",
            "처리하기",
            "저장하기",
            "불러오기",
            "삭제하기",
        )
    ]

    assert _obfuscation_likelihood(renamed)
    assert not _obfuscation_likelihood(localized)
    repeated_localized = [{"method_name": "연결"}] * 31 + [
        {"method_name": f"LoadResource{index}"} for index in range(176)
    ]
    assert not _obfuscation_likelihood(repeated_localized)


def _blackwood_method_names() -> list[str]:
    # Exact method names observed in methods.list observation
    # aae0069a-f490-5958-aa6d-9056d5489954; no sample bytes are used.
    hangul_counts = (
        ("쉥", 21),
        ("슁", 2),
        *(
            (name, 1)
            for name in (
                "쉩",
                "쉭",
                "쉯",
                "쉷",
                "쉸",
                "쉺",
                "쉼",
                "슀",
                "슄",
                "슆",
                "슇",
                "슋",
                "슌",
                "슎",
                "슏",
                "슐",
                "슑",
                "슕",
                "슚",
                "슝",
                "슞",
                "슠",
                "슡",
                "슢",
                "슦",
                "슫",
                "슮",
                "슳",
                "슷",
                "슸",
                "슺",
                "슻",
                "슿",
                "싂",
                "싌",
                "싎",
                "싏",
                "싒",
                "싗",
                "싛",
                "식",
            )
        ),
    )
    single_character_names = [name for name, count in hangul_counts for _ in range(count)]
    # The later Blackwood observation had 93 one-character Hangul names.
    # Keep the captured spellings and observed maximum frequency while filling
    # the remaining 29 entries with additional occurrences of observed names.
    singleton_names = [name for name, count in hangul_counts if count == 1]
    single_character_names.extend(singleton_names[:29])
    return (
        single_character_names
        + [
            "6",
            "BeginInvoke",
            "BeginInvoke",
            "BuildImportTable",
            "CopySections",
            "EndInvoke",
            "EndInvoke",
            "FinalizeSections",
            "GetModuleCount",
            "GetProcAddress",
            "GetValue",
            "GetValueFromBase64",
            "InitializeNewDomain",
            "Invoke",
            "Invoke",
            "PerformBaseRelocation",
            "RC4Decrypt",
        ]
        + [".ctor"] * 20
    )


def test_obfuscation_heuristic_detects_names_from_blackwood_distribution() -> None:
    method_names = _blackwood_method_names()
    assessment, metrics = dotnet_analysis_module._obfuscation_assessment(
        [{"method_name": name} for name in method_names]
    )

    assert len(method_names) == 130
    assert metrics["eligible_name_count"] == 110
    assert metrics["single_character_identifier_count"] == 93
    assert metrics["most_repeated_unicode_single_character_count"] == 21
    assert metrics["most_repeated_unicode_single_character_ratio"] == 0.2258
    assert metrics["renamed_name_ratio"] == 0.8455
    assert metrics["criteria"]["single_character_or_invisible_name_majority"] is True
    assert metrics["criteria"]["repeated_unicode_single_character_concentration"] is False
    assert assessment


@pytest.mark.asyncio
async def test_methods_list_detects_runtimebroker_invisible_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtimebroker_invisible_names = [
        "\u00a0",
        "\u1680",
        *(chr(codepoint) for codepoint in range(0x2000, 0x200C)),
    ]
    names = [
        runtimebroker_invisible_names[index % len(runtimebroker_invisible_names)]
        for index in range(207)
    ]
    rows = [
        f"{rid}: default void '{name}' () (param: 1 impl_flags: cil managed )"
        for rid, name in enumerate(names, start=1)
    ]

    async def fake_monodis(_payload, operations, _parameters, _settings, *, monodis_version):
        if operations == ["--method"]:
            return {
                "status": "SUCCEEDED",
                "version": "2.0",
                "stdout": "Method Table (1..207)\n########## RuntimeBroker\n" + "\n".join(rows),
            }
        assert operations == ["--typedef"]
        return {"status": "SUCCEEDED", "version": "2.0", "stdout": "Typedef Table\n"}

    async def fake_ilspy_metadata(*_args, **_kwargs):
        return "ilspycmd_unavailable_or_unattested"

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)
    monkeypatch.setattr(dotnet_analysis_module, "_try_ilspy_metadata", fake_ilspy_metadata)

    result = await run_dotnet_analysis(
        _parameters("methods", limit="200"),
        _MINIMAL_DOTNET_ASSEMBLY,
        Settings(_env_file=None),
        monodis_version=lambda: "2.0",
        extract_monodis_method_body=_extract_monodis_method_body,
    )

    assert result["status"] == "OBFUSCATED"
    assert result["obfuscation_assessment"] == "likely_renamed"
    assert result["total_items"] == 207
    assert result["obfuscation_metrics"]["eligible_name_count"] == 207
    assert result["obfuscation_metrics"]["unicode_invisible_name_count"] == 207
    assert result["obfuscation_metrics"]["renamed_name_ratio"] == 1.0
    assert result["obfuscation_metrics"]["criteria"]["single_character_or_invisible_name_majority"]


def test_obfuscation_heuristic_handles_unicode_categories_and_benign_names() -> None:
    assert all(
        dotnet_analysis_module._is_unicode_invisible_name(name)
        for name in ("\u00a0", "\u2028", "\u2029", "\u200b", "\u0000")
    )
    assert not dotnet_analysis_module._is_unicode_invisible_name("쉥")

    readable_korean = [
        "초기화",
        "연결",
        "분석",
        "가져오기",
        "설정",
        "읽기",
        "쓰기",
        "검증",
        "처리하기",
        "저장하기",
        "불러오기",
        "삭제하기",
        "계산",
        "비교",
        "변환",
        "요청",
        "응답",
        "네트워크",
        "인증",
        "권한",
        "구성",
        "해제",
        "연결하기",
        "초기설정",
    ]
    observed_hangul = list(
        dict.fromkeys(
            name for name in _blackwood_method_names() if len(name) == 1 and not name.isascii()
        )
    )
    below_half = [{"method_name": name} for name in observed_hangul[:19]]
    below_half.extend({"method_name": f"Method{index}"} for index in range(21))
    exactly_half = [{"method_name": name} for name in observed_hangul[:10]]
    exactly_half.extend({"method_name": f"RoutineMethod{index}"} for index in range(10))
    generic_names = ("i", "x", "T") * 6 + ("i",)
    generated_names = (
        [{"method_name": "T"}] * 19
        + [{"method_name": "<Worker>b__0"}] * 5
        + [{"method_name": "get_Value"}] * 5
        + [{"method_name": ".ctor"}] * 10
    )
    generated_assessment, generated_metrics = dotnet_analysis_module._obfuscation_assessment(
        generated_names
    )

    assert not _obfuscation_likelihood([{"method_name": name} for name in readable_korean])
    assert not _obfuscation_likelihood(below_half)
    assert _obfuscation_likelihood(exactly_half)
    assert not _obfuscation_likelihood(
        [{"method_name": name} for name in generic_names] + [{"method_name": ".ctor"}]
    )
    assert not generated_assessment
    assert generated_metrics["eligible_name_count"] == 19
    assert generated_metrics["excluded_special_name_count"] == 20


@pytest.mark.asyncio
async def test_methods_list_flags_blackwood_like_hangul_frequency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        f"{rid}: default void {'쉥' if rid <= 31 else f'LoadResource{rid}'} () "
        f"(param: 1 impl_flags: cil managed )"
        for rid in range(1, 208)
    ]

    async def fake_monodis(_payload, operations, _parameters, _settings, *, monodis_version):
        assert operations == ["--method"]
        return {
            "status": "SUCCEEDED",
            "version": "2.0",
            "stdout": "Method Table (1..207)\n########## Fixture\n" + "\n".join(rows),
        }

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)

    result = await run_dotnet_analysis(
        _parameters("methods", limit="200"),
        _MINIMAL_DOTNET_ASSEMBLY,
        Settings(_env_file=None),
        monodis_version=lambda: "2.0",
        extract_monodis_method_body=_extract_monodis_method_body,
    )

    assert result["obfuscation_assessment"] == "likely_renamed"
    assert result["total_items"] == 207
    assert result["records"][30]["method_name"] == "쉥"


@pytest.mark.asyncio
async def test_methods_list_assesses_blackwood_population_before_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _blackwood_method_names()
    rows = [
        f"{rid}: default void {name} () (param: 1 impl_flags: cil managed )"
        for rid, name in enumerate(names, start=1)
    ]

    async def fake_monodis(_payload, operations, _parameters, _settings, *, monodis_version):
        assert operations == ["--method"]
        return {
            "status": "SUCCEEDED",
            "version": "2.0",
            "stdout": "Method Table (1..130)\n########## Fixture\n" + "\n".join(rows),
        }

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)

    result = await run_dotnet_analysis(
        _parameters("methods", offset="110", limit="20"),
        _MINIMAL_DOTNET_ASSEMBLY,
        Settings(_env_file=None),
        monodis_version=lambda: "2.0",
        extract_monodis_method_body=_extract_monodis_method_body,
    )

    assert result["status"] == "OBFUSCATED"
    assert result["schema_version"] == "dotnet-analysis-result.v3"
    assert result["total_items"] == 130
    assert {record["method_name"] for record in result["records"]} == {".ctor"}
    assert result["obfuscation_assessment"] == "likely_renamed"
    assert result["obfuscation_metrics"]["single_character_identifier_count"] == 93
    assert result["obfuscation_metrics"]["excluded_special_name_count"] == 20


@pytest.mark.asyncio
async def test_methods_list_repairs_empty_monodis_names_from_ilspy_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    il = (
        ".class private auto ansi '<Module>'\n{\n} // end of class <Module>\n"
        ".class public auto ansi beforefieldinit Fixture\n{\n"
        "    .method public hidebysig specialname rtspecialname "
        "instance void .ctor () cil managed\n"
        "    {\n        IL_0000: ret\n"
        "    } // end of method Fixture::.ctor\n}\n} // end of class Fixture\n"
    )
    typedef = (
        "Typedef Table\n"
        "1: (null) (flist=1, mlist=1, flags=0x0, extends=0x0)\n"
        "2: Fixture (flist=1, mlist=1, flags=0x100181, extends=0x5)\n"
    )

    async def fake_monodis(_payload, operations, _parameters, _settings, *, monodis_version):
        if operations == ["--method"]:
            return {
                "status": "SUCCEEDED",
                "version": "2.0",
                "stdout": "Method Table (1..1)\n########## .\n1: <unparsed> (param: 1)\n",
            }
        assert operations == ["--typedef"]
        return {"status": "SUCCEEDED", "version": "2.0", "stdout": typedef}

    async def fake_subprocess(argv, **_kwargs):
        calls.append(list(argv))
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, il.encode(), b"")

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)
    monkeypatch.setattr(dotnet_analysis_module, "run_analysis_subprocess", fake_subprocess)
    monkeypatch.setattr(dotnet_analysis_module, "ilspycmd_provenance", lambda **_kwargs: object())
    monkeypatch.setattr(
        dotnet_analysis_module.shutil,
        "which",
        lambda name: "/usr/local/bin/ilspycmd" if name == "ilspycmd" else None,
    )

    result = await run_dotnet_analysis(
        _parameters("methods"),
        _MINIMAL_DOTNET_ASSEMBLY,
        Settings(_env_file=None),
        monodis_version=lambda: "2.0",
        extract_monodis_method_body=_extract_monodis_method_body,
    )

    assert result["engine"] == "ilspycmd"
    assert result["records"][0]["type_name"] == "Fixture"
    assert result["records"][0]["method_name"] == ".ctor"
    assert "unparsed" not in {record["method_name"] for record in result["records"]}
    assert any("--ilcode" in argv for argv in calls)


@pytest.mark.asyncio
async def test_decompile_constructor_rids_uses_ilspy_after_monodis_exit_139(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    il = (
        ".class private auto ansi '<Module>'\n{\n} // end of class <Module>\n"
        ".class public auto ansi beforefieldinit FixtureA\n{\n"
        "    .method public hidebysig specialname rtspecialname "
        "instance void .ctor () cil managed\n"
        "    {\n        IL_0000: ret\n"
        "    } // end of method FixtureA::.ctor\n"
        "    .method public hidebysig static void Helper1 () cil managed\n"
        "    {\n        IL_0000: ret\n"
        "    } // end of method FixtureA::Helper1\n"
        "    .method public hidebysig static void Helper2 () cil managed\n"
        "    {\n        IL_0000: ret\n"
        "    } // end of method FixtureA::Helper2\n}\n} // end of class FixtureA\n"
        ".class public auto ansi beforefieldinit FixtureB\n{\n"
        "    .method public hidebysig specialname rtspecialname "
        "instance void .ctor () cil managed\n"
        "    {\n        IL_0000: ldarg.0\n        IL_0001: ret\n"
        "    } // end of method FixtureB::.ctor\n"
        "    .method public hidebysig static void Helper3 () cil managed\n"
        "    {\n        IL_0000: ret\n"
        "    } // end of method FixtureB::Helper3\n}\n} // end of class FixtureB\n"
    )
    typedef = (
        "Typedef Table\n"
        "1: (null) (flist=1, mlist=1, flags=0x0, extends=0x0)\n"
        "2: FixtureA (flist=1, mlist=1, flags=0x100181, extends=0x5)\n"
        "3: FixtureB (flist=1, mlist=4, flags=0x100181, extends=0x5)\n"
    )

    async def fake_monodis(_payload, operations, _parameters, _settings, *, monodis_version):
        if operations == ["--typedef"]:
            return {"status": "SUCCEEDED", "version": "2.0", "stdout": typedef}
        return {
            "status": "FAILED",
            "available_reason": "monodis_metadata_failed",
            "version": "2.0",
            "exit_code": 139,
            "error_detail": "segmentation fault",
        }

    async def fake_subprocess(argv, **_kwargs):
        calls.append(list(argv))
        selected_type = argv[argv.index("--type") + 1] if "--type" in argv else None
        output = il
        if selected_type == "FixtureA":
            output = (
                ".class public FixtureA\n{\n.method instance void .ctor () cil managed\n"
                "{\nIL_0000: ret\n} // end of method FixtureA::.ctor\n"
                "} // end of class FixtureA\n"
            )
        elif selected_type == "FixtureB":
            output = (
                ".class public FixtureB\n{\n.method instance void .ctor () cil managed\n"
                "{\nIL_0000: ldarg.0\nIL_0001: ret\n} // end of method FixtureB::.ctor\n"
                "} // end of class FixtureB\n"
            )
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, output.encode(), b"")

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)
    monkeypatch.setattr(dotnet_analysis_module, "run_analysis_subprocess", fake_subprocess)
    monkeypatch.setattr(dotnet_analysis_module, "ilspycmd_provenance", lambda **_kwargs: object())
    monkeypatch.setattr(
        dotnet_analysis_module.shutil,
        "which",
        lambda name: "/usr/local/bin/ilspycmd" if name == "ilspycmd" else None,
    )
    settings = Settings(_env_file=None)

    for rid, type_name in ((1, "FixtureA"), (4, "FixtureB")):
        result = await run_dotnet_analysis(
            _parameters("decompile", method_rid=str(rid)),
            _MINIMAL_DOTNET_ASSEMBLY,
            settings,
            monodis_version=lambda: "2.0",
            extract_monodis_method_body=_extract_monodis_method_body,
        )
        assert result["status"] == "SUCCEEDED", {
            key: result.get(key)
            for key in (
                "available_reason",
                "ilspy_cause",
                "ilspy_metadata_cause",
                "monodis_cause",
                "monodis_exit_code",
                "type_name",
                "method_name",
            )
        }
        assert result["engine"] == "ilspycmd"
        assert result["type_name"] == type_name
        assert result["method_name"] == ".ctor"
        assert "IL_0000:" in result["stdout"]

    assert sum("--type" in argv for argv in calls) == 2


@pytest.mark.asyncio
async def test_decompile_failure_preserves_monodis_exit_139(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_monodis(_payload, operations, _parameters, _settings, *, monodis_version):
        if operations == ["--method"]:
            return {
                "status": "SUCCEEDED",
                "version": "2.0",
                "stdout": (
                    "Method Table (1..1)\n########## Fixture\n"
                    "1: instance default void .ctor ()  (param: 1 impl_flags: cil managed )\n"
                ),
            }
        assert operations == ["--show-method-tokens"]
        return {
            "status": "FAILED",
            "available_reason": "monodis_metadata_failed",
            "version": "2.0",
            "exit_code": 139,
            "error_detail": "segmentation fault",
        }

    async def fake_subprocess(_argv, **_kwargs):
        il_without_body = ".class public Fixture\n{\n} // end of class Fixture\n"
        return AnalysisSubprocessResult(
            AnalysisSubprocessStatus.SUCCEEDED, 0, il_without_body.encode(), b""
        )

    monkeypatch.setattr(dotnet_analysis_module, "_run_monodis_operations", fake_monodis)
    monkeypatch.setattr(dotnet_analysis_module, "run_analysis_subprocess", fake_subprocess)
    monkeypatch.setattr(dotnet_analysis_module, "ilspycmd_provenance", lambda **_kwargs: object())
    monkeypatch.setattr(
        dotnet_analysis_module.shutil,
        "which",
        lambda name: "/usr/local/bin/ilspycmd" if name == "ilspycmd" else None,
    )

    result = await run_dotnet_analysis(
        _parameters("decompile", method_rid="1"),
        _MINIMAL_DOTNET_ASSEMBLY,
        Settings(_env_file=None),
        monodis_version=lambda: "2.0",
        extract_monodis_method_body=_extract_monodis_method_body,
    )

    assert result["status"] == "FAILED"
    assert result["available_reason"] == "monodis_metadata_failed"
    assert result["ilspy_cause"] == "ilspycmd_method_body_missing"
    assert result["monodis_exit_code"] == 139


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
