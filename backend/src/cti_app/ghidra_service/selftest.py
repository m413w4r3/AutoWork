from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cti_app.ghidra_service.contract import AnalysisRequest
from cti_app.ghidra_service.engine import HeadlessGhidraEngine, create_minimal_elf
from cti_app.ghidra_service.service import GhidraReadOnlyService


def run_real_headless_selftest() -> None:
    install_dir = Path(os.environ.get("GHIDRA_INSTALL_DIR", "/opt/ghidra"))
    if not install_dir.is_dir():
        raise RuntimeError("GHIDRA_INSTALL_DIR must identify the installed Ghidra distribution")
    expected_version = os.environ.get("GHIDRA_EXPECTED_VERSION", "12.1.4")
    expected_java_version = os.environ.get("GHIDRA_EXPECTED_JAVA_VERSION", "21.0.12.1")
    timeout_seconds = int(os.environ.get("GHIDRA_SERVICE_JOB_TIMEOUT_SECONDS", "120"))
    workspace = Path(os.environ.get("GHIDRA_WORKSPACE", "/work"))
    workspace.mkdir(parents=True, exist_ok=True)
    engine = HeadlessGhidraEngine(install_dir, timeout_seconds=timeout_seconds)
    service = GhidraReadOnlyService(
        engine,
        expected_version=expected_version,
        expected_java_version=expected_java_version,
        maximum_sample_bytes=1024 * 1024,
        timeout_seconds=timeout_seconds,
        workspace_root=workspace,
    )
    sample = create_minimal_elf()
    digest = hashlib.sha256(sample).hexdigest()
    result = service.analyze(
        AnalysisRequest("list_functions", digest),
        sample,
    )
    data = result["data"]
    if not isinstance(data, dict):
        raise RuntimeError("Ghidra did not return an object for the generated ELF")
    functions = data.get("functions")
    if not isinstance(functions, list):
        raise RuntimeError("Ghidra did not return a function list for the generated ELF")
    if not any(
        isinstance(item, dict)
        and isinstance(item.get("address"), str)
        and int(item["address"].removeprefix("0x"), 16) == 0x400078
        for item in functions
    ):
        raise RuntimeError("Ghidra did not identify the generated ELF entry point")
    print(
        f"PASS real Ghidra {engine.ghidra_version} / Java {engine.java_version}: "
        f"list_functions on generated ELF ({digest})"
    )
    imports_result = service.analyze(AnalysisRequest("list_imports", digest), sample)
    imports_data = imports_result["data"]
    if not isinstance(imports_data, dict) or not isinstance(imports_data.get("imports"), list):
        raise RuntimeError("Ghidra did not return an import list for the generated ELF")
    print(
        f"PASS real Ghidra {engine.ghidra_version} / Java {engine.java_version}: "
        f"list_imports on generated ELF ({len(imports_data['imports'])} imports)"
    )
    decompile_result = service.analyze(
        AnalysisRequest("decompile_function", digest, address="400078", limit=1), sample
    )
    decompile_data = decompile_result["data"]
    if (
        not isinstance(decompile_data, dict)
        or decompile_data.get("status") != "succeeded"
        or not isinstance(decompile_data.get("function"), dict)
        or not isinstance(decompile_data["function"].get("decompilation"), str)
        or not decompile_data["function"]["decompilation"].strip()
    ):
        raise RuntimeError("Ghidra did not decompile the generated ELF entry point")
    print(
        f"PASS real Ghidra {engine.ghidra_version} / Java {engine.java_version}: "
        "decompile_function on generated ELF entry point"
    )
    unsupported_result = service.analyze(
        AnalysisRequest("decompile_function", digest, address="1", limit=1), sample
    )
    unsupported_data = unsupported_result["data"]
    if (
        not isinstance(unsupported_data, dict)
        or unsupported_data.get("status") != "unsupported_function"
        or unsupported_data.get("reason") != "function_not_found_at_address"
    ):
        raise RuntimeError("Ghidra did not classify an absent ELF function precisely")
    print(
        f"PASS real Ghidra {engine.ghidra_version} / Java {engine.java_version}: "
        "unsupported_function on absent ELF address"
    )


if __name__ == "__main__":
    run_real_headless_selftest()
