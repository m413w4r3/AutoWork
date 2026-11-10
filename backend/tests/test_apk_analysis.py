from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import zipfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.apk_analysis import (
    APK_ANALYSIS_RESULT_SCHEMA,
    JADX_VERSION,
    JADX_ZIP_SHA256,
    ApkAnalysisOperation,
    ApkAnalysisParameters,
    ApkAnalysisResult,
    ApkAnalysisStatus,
    ApkClassItem,
    ApkPage,
)
from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.config import Settings
from cti_app.workers.apk_analysis import (
    _fit_result_budget,
    _run_operation,
    _validate_apk_archive,
    run_apk_analysis,
)
from cti_app.workers.confined_analysis import _execute_tool

_FIXTURE_PATH = Path(__file__).parent / "fixtures/apk/autowork-apk-fixture.apk"
_FIXTURE_SHA256 = "c97fb8885d1f65c7521b9ab6e052b1fdb0b3920488237d619f28f209706bd29e"
_JADX_BINARY = os.environ.get("AUTOWORK_JADX_BINARY")
_JADX_UNAVAILABLE_REASON = (
    "set AUTOWORK_JADX_BINARY to the locally verified jadx-1.5.6 executable "
    "and provide a supported Java runtime"
)


def _parameters(
    analysis_parameters: dict[str, str],
    *,
    artifact_sha256: str = _FIXTURE_SHA256,
) -> ConfinedAnalysisJobParameters:
    return ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=artifact_sha256,
        operation_key="confined-analysis:" + "a" * 64,
        tool=ConfinedAnalysisTool.APK_JADX,
        timeout_seconds=30,
        max_output_bytes=1_000_000,
        artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
        association_id=uuid4(),
        sample_id=uuid4(),
        analysis_parameters=analysis_parameters,
    )


def test_apk_contract_round_trips_and_enforces_operation_bounds() -> None:
    parameters = ApkAnalysisParameters(
        operation=ApkAnalysisOperation.SOURCE_SEARCH,
        offset=2,
        limit=25,
        pattern="AUTOWORK_APK_FIXTURE",
        context_lines=1,
    )
    assert (
        ApkAnalysisParameters.from_worker_parameters(parameters.to_worker_parameters())
        == parameters
    )

    with pytest.raises(ValueError, match="page size"):
        ApkAnalysisParameters(operation=ApkAnalysisOperation.CLASSES_LIST, limit=201)
    with pytest.raises(ValueError, match="source path"):
        ApkAnalysisParameters(
            operation=ApkAnalysisOperation.SOURCE_READ,
            source_path="sources/../../outside.java",
        )
    with pytest.raises(ValueError, match="repetition operators"):
        ApkAnalysisParameters(
            operation=ApkAnalysisOperation.SOURCE_SEARCH,
            pattern="a.*b.*c",
            regex=True,
        )
    with pytest.raises(ValueError, match="exactly one"):
        ApkAnalysisParameters(operation=ApkAnalysisOperation.SOURCE_READ)


def test_confined_request_accepts_only_associated_sample_and_typed_apk_operation() -> None:
    request = ConfinedAnalysisRequest(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        sha256=_FIXTURE_SHA256,
        tool=ConfinedAnalysisTool.APK_JADX,
        timeout_seconds=30,
        max_output_bytes=100_000,
        association_id=uuid4(),
        sample_id=uuid4(),
        analysis_parameters={"operation": ApkAnalysisOperation.MANIFEST.value},
    )
    assert request.analysis_parameters["operation"] == "apk.manifest"
    with pytest.raises(ValueError, match="associated sample"):
        ConfinedAnalysisRequest(
            investigation_id=uuid4(),
            artifact_id=uuid4(),
            sha256=_FIXTURE_SHA256,
            tool=ConfinedAnalysisTool.APK_JADX,
            timeout_seconds=30,
            max_output_bytes=100_000,
            artifact_kind=ConfinedAnalysisArtifactKind.CORPUS_SAMPLE,
            analysis_parameters={"operation": "apk.manifest"},
        )


def test_fixture_hash_and_archive_contract_are_stable() -> None:
    payload = _FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == _FIXTURE_SHA256
    assert _validate_apk_archive(payload) is None


def test_apk_archive_zip_slip_and_invalid_hash_are_rejected_before_jadx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _FIXTURE_PATH.read_bytes()
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("AndroidManifest.xml", "manifest")
        archive.writestr("classes.dex", b"dex")
        archive.writestr("../escape", b"x")
    assert _validate_apk_archive(malicious.getvalue()) == "apk_archive_path_invalid"

    monkeypatch.setattr(
        "cti_app.workers.apk_analysis.resolve_jadx_binary",
        lambda: pytest.fail("invalid APK must be rejected before jadx lookup"),
    )
    invalid = asyncio.run(
        run_apk_analysis(
            _parameters({"operation": "apk.manifest"}, artifact_sha256=_FIXTURE_SHA256),
            # The malformed archive has its own identity, so validation reaches ZIP path checks.
            malicious.getvalue(),
            Settings(_env_file=None),
        )
    )
    assert invalid["status"] == "POLICY_DENIED"
    assert invalid["data"]["available_reason"] == "apk_sample_hash_mismatch"

    invalid_archive = asyncio.run(
        run_apk_analysis(
            _parameters(
                {"operation": "apk.manifest"},
                artifact_sha256=hashlib.sha256(malicious.getvalue()).hexdigest(),
            ),
            malicious.getvalue(),
            Settings(_env_file=None),
        )
    )
    assert invalid_archive["status"] == "FAILED"
    assert invalid_archive["data"]["available_reason"] == "apk_archive_path_invalid"

    mismatch = asyncio.run(
        run_apk_analysis(
            _parameters({"operation": "apk.manifest"}, artifact_sha256="0" * 64),
            payload,
            Settings(_env_file=None),
        )
    )
    assert mismatch["status"] == "POLICY_DENIED"
    assert mismatch["data"]["available_reason"] == "apk_sample_hash_mismatch"


def test_apk_worker_dispatches_typed_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_run(parameters, payload, settings):
        calls.append((parameters, payload, settings))
        return {"schema_version": APK_ANALYSIS_RESULT_SCHEMA, "status": "SUCCEEDED"}

    monkeypatch.setattr("cti_app.workers.confined_analysis.run_apk_analysis", fake_run)
    parameters = _parameters({"operation": "apk.manifest"})
    settings = Settings(_env_file=None)
    result = asyncio.run(
        _execute_tool(
            parameters,
            sample=object(),
            payload=b"synthetic fixture input",
            blob_id=uuid4(),
            blobs=None,  # type: ignore[arg-type]
            uow_factory=lambda: None,  # type: ignore[arg-type]
            settings=settings,
        )
    )
    assert result == {"schema_version": APK_ANALYSIS_RESULT_SCHEMA, "status": "SUCCEEDED"}
    assert calls == [(parameters, b"synthetic fixture input", settings)]


def test_apk_operation_results_have_canonical_hash_and_no_host_paths(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    source = output_dir / "sources/org/autowork/fixture/MainActivity.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        'package org.autowork.fixture;\n'
        'public class MainActivity {\n'
        '  String url = "https://fixture.example.invalid/";\n'
        '  String marker = "AUTOWORK_APK_FIXTURE";\n'
        '}\n',
        encoding="utf-8",
    )
    receiver = output_dir / "sources/org/autowork/fixture/FixtureReceiver.java"
    receiver.write_text(
        "package org.autowork.fixture;\npublic class FixtureReceiver {}\n",
        encoding="utf-8",
    )
    manifest = output_dir / "resources/AndroidManifest.xml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
        'package="org.autowork.fixture"><uses-sdk android:minSdkVersion="21" '
        'android:targetSdkVersion="28"/><uses-permission '
        'android:name="android.permission.INTERNET"/><application android:debuggable="false" '
        'android:allowBackup="true" android:usesCleartextTraffic="false">'
        '<activity android:name=".MainActivity" android:exported="true"/>'
        '<receiver android:name=".FixtureReceiver" android:exported="false"/>'
        '</application></manifest>',
        encoding="utf-8",
    )

    cases = (
        ApkAnalysisParameters(operation=ApkAnalysisOperation.MANIFEST),
        ApkAnalysisParameters(operation=ApkAnalysisOperation.CLASSES_LIST, limit=1),
        ApkAnalysisParameters(operation=ApkAnalysisOperation.STRINGS_EXTRACT, limit=10),
        ApkAnalysisParameters(
            operation=ApkAnalysisOperation.SOURCE_READ,
            class_name="org.autowork.fixture.MainActivity",
            line_count=20,
        ),
        ApkAnalysisParameters(
            operation=ApkAnalysisOperation.SOURCE_SEARCH,
            pattern="AUTOWORK_APK_FIXTURE",
            context_lines=1,
        ),
    )
    outputs = [_run_operation(item, output_dir) for item in cases]
    assert outputs[0].package_name == "org.autowork.fixture"
    assert outputs[0].permissions == ("android.permission.INTERNET",)
    assert outputs[0].components[0].exported is True
    assert outputs[0].components[1].exported is False
    assert outputs[1].total == 2 and len(outputs[1].items) == 1
    next_page = _run_operation(
        ApkAnalysisParameters(
            operation=ApkAnalysisOperation.CLASSES_LIST,
            offset=1,
            limit=1,
        ),
        output_dir,
    )
    assert next_page.total == 2 and next_page.items[0].class_name == "MainActivity"
    assert any(item.kind == "url" for item in outputs[2].items)
    assert outputs[3].source_path == "sources/org/autowork/fixture/MainActivity.java"
    assert "AUTOWORK_APK_FIXTURE" in outputs[3].content
    assert outputs[4].total == 1 and outputs[4].items[0].line == 4
    manifest_json = json.dumps(asdict(outputs[0]), sort_keys=True, separators=(",", ":"))
    repeated_manifest_json = json.dumps(
        asdict(_run_operation(cases[0], output_dir)), sort_keys=True, separators=(",", ":")
    )
    assert hashlib.sha256(manifest_json.encode()).hexdigest() == hashlib.sha256(
        repeated_manifest_json.encode()
    ).hexdigest()
    assert "/tmp/" not in json.dumps(asdict(outputs[3]))


def test_apk_result_is_reduced_to_the_requested_output_budget() -> None:
    result = ApkAnalysisResult(
        operation=ApkAnalysisOperation.CLASSES_LIST,
        status=ApkAnalysisStatus.SUCCEEDED,
        sample_sha256="a" * 64,
        jadx_version=JADX_VERSION,
        jadx_zip_sha256=JADX_ZIP_SHA256,
        jre_version="17.0.12",
        data=ApkPage(
            offset=0,
            limit=200,
            total=100,
            items=tuple(
                ApkClassItem(
                    package_name="org." + "p" * 100,
                    class_name="C" + "c" * 100,
                    source_path="sources/" + "x" * 200 + ".java",
                )
                for _ in range(100)
            ),
        ),
    )

    payload = _fit_result_budget(result, 2048)

    assert len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()) <= 2048
    assert payload["status"] == "PARTIAL"
    assert payload["data"]["truncated"] is True
    assert len(payload["data"]["items"]) < 100


@pytest.mark.skipif(
    not _JADX_BINARY or not Path(_JADX_BINARY).is_file() or shutil.which("java") is None,
    reason=_JADX_UNAVAILABLE_REASON,
)
def test_real_jadx_decompiles_only_the_synthetic_offline_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOWORK_JADX_BINARY", str(_JADX_BINARY))
    payload = _FIXTURE_PATH.read_bytes()
    settings = Settings(_env_file=None)

    def run(operation: str, **parameters: str) -> dict:
        return asyncio.run(
            run_apk_analysis(
                _parameters({"operation": operation, **parameters}),
                payload,
                settings,
            )
        )

    manifest = run("apk.manifest")
    classes = run("apk.classes.list", offset="0", limit="10")
    strings = run("apk.strings.extract", offset="0", limit="100")
    source = run(
        "apk.source.read",
        class_name="org.autowork.fixture.MainActivity",
        start_line="1",
        line_count="100",
    )
    search = run(
        "apk.source.search",
        offset="0",
        limit="10",
        pattern="AUTOWORK_APK_FIXTURE",
        regex="false",
        context_lines="1",
    )

    assert manifest["status"] == "SUCCEEDED", manifest
    assert manifest["data"]["package_name"] == "org.autowork.fixture"
    assert manifest["data"]["permissions"] == ["android.permission.INTERNET"]
    assert {item["exported"] for item in manifest["data"]["components"]} == {True, False}
    assert classes["status"] == "SUCCEEDED", classes
    assert {item["class_name"] for item in classes["data"]["items"]} >= {
        "MainActivity",
        "FixtureReceiver",
    }
    assert strings["status"] == "SUCCEEDED", strings
    assert any(
        item["value"] == "https://fixture.example.invalid/"
        for item in strings["data"]["items"]
    )
    assert any(item["value"] == "AUTOWORK_APK_FIXTURE" for item in strings["data"]["items"])
    assert source["status"] == "SUCCEEDED", source
    assert "AUTOWORK_APK_FIXTURE" in source["data"]["content"]
    assert source["data"]["source_path"].startswith("sources/")
    assert search["status"] == "SUCCEEDED", search
    assert search["data"]["items"][0]["match"] == "AUTOWORK_APK_FIXTURE"
    assert all(item["source_path"].startswith("sources/") for item in search["data"]["items"])
    assert manifest["sample_sha256"] == _FIXTURE_SHA256
    assert manifest["jadx_version"] == JADX_VERSION
    assert manifest["jadx_zip_sha256"] == JADX_ZIP_SHA256
