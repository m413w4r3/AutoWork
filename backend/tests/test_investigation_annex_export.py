from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from cti_app.application.investigation_annex_export import (
    AnnexAsset,
    InvestigationAnnexSnapshot,
    write_investigation_annex,
)
from cti_app.domain.investigation import DetectionCandidateVersion, MotifProvenance
from cti_app.infrastructure.database.investigation_annex_reader import (
    _yara_version_origin_metadata,
)


def _read_manifest(directory: Path) -> dict[str, object]:
    raw = (directory / "manifest.json").read_bytes()
    expected = (directory / "manifest.sha256").read_text(encoding="ascii").split()[0]
    assert hashlib.sha256(raw).hexdigest() == expected
    return cast(dict[str, object], json.loads(raw))


def _file_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _assert_manifest_files(directory: Path, manifest: dict[str, object]) -> None:
    files = manifest["files"]
    assert isinstance(files, list)
    for entry in files:
        path = directory / entry["path"]
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert len(content) == entry["size_bytes"]


@pytest.mark.asyncio
async def test_yara_origin_metadata_uses_verified_observation_and_tool_invocation() -> None:
    investigation_id = uuid4()
    candidate_id = uuid4()
    candidate_version_id = uuid4()
    observation_id = uuid4()
    invocation_id = uuid4()
    blob_id = uuid4()
    payload = {
        "schema": "investigation-tool-observation.v1",
        "result": {
            "candidate_id": str(candidate_id),
            "version_id": str(candidate_version_id),
            "source_author": "bridge",
            "source_attribution": {
                "author": "bridge",
                "source_author": "bridge",
                "origin_author": "bridge",
                "change_kind": "bridge_yara_draft",
            },
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    observation = SimpleNamespace(
        id=observation_id,
        invocation_ref=invocation_id,
        blob_id=blob_id,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    invocation = SimpleNamespace(
        id=invocation_id,
        tool_name="rule.propose_bridge",
        response_refs=(observation_id,),
    )
    candidate = DetectionCandidateVersion(
        id=candidate_version_id,
        candidate_id=candidate_id,
        investigation_id=investigation_id,
        source="rule synthetic { condition: true }",
        source_sha256=hashlib.sha256(b"rule synthetic { condition: true }").hexdigest(),
        motif_provenance=(
            MotifProvenance(
                motif="synthetic marker",
                rationale="synthetic observation-backed provenance",
                observation_refs=(observation_id,),
            ),
        ),
    )

    class ArtifactReader:
        async def read_bytes(self, requested_blob_id, *, max_bytes=None):
            assert requested_blob_id == blob_id
            assert max_bytes is not None
            return raw

    metadata = await _yara_version_origin_metadata(
        candidate,
        observations=(observation,),
        invocations=(invocation,),
        artifact_store=ArtifactReader(),
    )

    assert metadata["author"] == "bridge"
    assert metadata["author_status"] == "recorded"
    assert metadata["origin"] == "bridge"
    assert metadata["origin_tool"] == "rule.propose_bridge"
    assert metadata["origin_invocation_id"] == str(invocation_id)
    assert metadata["source_observation_ids"] == (str(observation_id),)
    assert metadata["parent_version"] is None


@pytest.mark.asyncio
async def test_yara_origin_metadata_does_not_invent_missing_author_or_origin() -> None:
    source = "rule unattributed { condition: true }"
    candidate = DetectionCandidateVersion(
        investigation_id=uuid4(),
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        motif_provenance=(),
    )

    class UnusedArtifactReader:
        async def read_bytes(self, requested_blob_id, *, max_bytes=None):
            raise AssertionError("No canonical observation should be read")

    metadata = await _yara_version_origin_metadata(
        candidate,
        observations=(),
        invocations=(),
        artifact_store=UnusedArtifactReader(),
    )

    assert metadata["author"] is None
    assert metadata["author_status"] == "not_recorded"
    assert metadata["origin"] is None
    assert metadata["origin_tool"] is None
    assert metadata["origin_invocation_id"] is None
    assert metadata["source_observation_ids"] == ()
    assert metadata["parent_version"] is None


def test_annex_exports_are_reproducible_and_keep_every_rule_byte(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    candidate_id = str(uuid4())
    v1 = ('rule first { strings: $a = "first" condition: $a }\n' + "// v1\n" * 2_000).encode()
    v2 = ('rule second { strings: $a = "second" condition: $a }\n' + "// v2\n" * 2_500).encode()
    suricata = (
        'alert tls any any -> any any (msg:"Example"; tls.sni; content:"example.test"; '
        "sid:9900001; rev:1;)\n" + "# preserved\n" * 1_800
    ).encode()
    report = json.dumps({"schema": "test.v1", "cases": ["x" * 256] * 300}).encode()
    assets = (
        AnnexAsset(
            canonical_id="yara-v2",
            type="yara_rule",
            investigation_id=investigation_id,
            version=2,
            sha256=hashlib.sha256(v2).hexdigest(),
            source="detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            references=(candidate_id, "yara-report"),
            content=v2,
            public_distribution_allowed=True,
            metadata={
                "candidate_id": candidate_id,
                "candidate_status": "experimental",
                "author": None,
                "author_status": "not_recorded_in_canonical_candidate",
                "rule_kind": "yara",
                "test_refs": ("case-2",),
            },
        ),
        AnnexAsset(
            canonical_id="yara-v1",
            type="yara_rule",
            investigation_id=investigation_id,
            version=1,
            sha256=hashlib.sha256(v1).hexdigest(),
            source="detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            references=(candidate_id,),
            content=v1,
            public_distribution_allowed=True,
            metadata={
                "candidate_id": candidate_id,
                "candidate_status": "candidate",
                "author_status": "not_recorded_in_canonical_candidate",
                "rule_kind": "yara",
                "test_refs": ("case-1",),
            },
        ),
        AnnexAsset(
            canonical_id="suricata-v1",
            type="suricata_rule",
            investigation_id=investigation_id,
            version=1,
            sha256=hashlib.sha256(suricata).hexdigest(),
            source="advisor_call_payload.output.rule_source",
            origin="advisor.consult:suricata_draft",
            references=("suricata-observation",),
            content=suricata,
            public_distribution_allowed=True,
            metadata={
                "candidate_status": "experimental",
                "rule_kind": "suricata",
                "rule_syntax_validation": "not_tested",
                "replay_status": "not_tested",
            },
        ),
        AnnexAsset(
            canonical_id="yara-report",
            type="yara_test_report",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(report).hexdigest(),
            source="AutoWork RuleWorkbench",
            origin="detection_candidate_version",
            references=("yara-v1", "yara-v2", "case-1", "case-2"),
            content=report,
            public_distribution_allowed=True,
            metadata={"status": "ONE_RULE_NOT_VALIDATED", "rule_kind": "yara"},
        ),
    )
    snapshot = InvestigationAnnexSnapshot(investigation_id, assets)

    first = tmp_path / "first"
    second = tmp_path / "second"
    write_investigation_annex(snapshot, first)
    write_investigation_annex(
        InvestigationAnnexSnapshot(investigation_id, tuple(reversed(assets))), second
    )

    assert _file_bytes(first) == _file_bytes(second)
    assert (first / "yara/candidate-001-v1.yar").read_bytes() == v1
    assert (first / "yara/candidate-001-v2.yar").read_bytes() == v2
    assert (first / "suricata/candidate-001-v1.rules").read_bytes() == suricata
    assert (first / "yara/tests/report-001.json").read_bytes() == report
    manifest = _read_manifest(first)
    objects = manifest["objects"]
    assert isinstance(objects, list)
    rules = [item for item in objects if item["type"] == "yara_rule"]
    assert [item["version"] for item in rules] == [1, 2]
    assert [item["sha256"] for item in rules] == [
        hashlib.sha256(v1).hexdigest(),
        hashlib.sha256(v2).hexdigest(),
    ]
    assert all(item["origin"] == "canonical_detection_candidate_versions" for item in rules)
    assert all("author_status" in item["metadata"] for item in rules)
    assert b"case-1" in (first / "yara/tests/index.json").read_bytes()
    _assert_manifest_files(first, manifest)


def test_default_export_omits_all_restricted_bytes_and_records_policy(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    sample_bytes = bytes(range(256)) * 64
    capture_bytes = b"\xd4\xc3\xb2\xa1" + bytes(range(255)) * 128
    note = b'{"host":"sensitive.example","status":"documented_not_observed"}'
    figure = b'<svg xmlns="http://www.w3.org/2000/svg"><text>restricted figure</text></svg>'
    assets = (
        AnnexAsset(
            canonical_id="sample-id",
            type="sample",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(sample_bytes).hexdigest(),
            source="samples+blobs",
            origin="source_seed",
            content=sample_bytes,
            metadata={
                "archive_export_allowed": True,
                "external_llm_allowed": True,
                "license_restriction_present": False,
                "origin_kind": "source_seed",
                "sample_state": "validated",
                "tlp": "amber",
            },
        ),
        AnnexAsset(
            canonical_id="capture-id",
            type="network_capture",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(capture_bytes).hexdigest(),
            source="evidence_observations.blob_id",
            origin="network.quarantine",
            content=capture_bytes,
            metadata={"archive_export_allowed": True, "external_llm_allowed": True, "tlp": "amber"},
        ),
        AnnexAsset(
            canonical_id="note-id",
            type="protocol_note",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(note).hexdigest(),
            source="protocol_model_blob",
            origin="canonical_evidence_observation",
            content=note,
            filename="protocol-note.json",
            metadata={"archive_export_allowed": True, "status": "documented_not_observed"},
        ),
        AnnexAsset(
            canonical_id="figure-id",
            type="technical_figure",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(figure).hexdigest(),
            source="media_assets+blobs",
            origin="analyst_opinion.protocol_figures",
            content=figure,
            filename="opinion-figure.svg",
            metadata={"archive_export_allowed": True, "figure_kind": "message_flow"},
        ),
    )
    output = tmp_path / "public"
    write_investigation_annex(InvestigationAnnexSnapshot(investigation_id, assets), output)

    all_content = b"\n".join(_file_bytes(output).values())
    assert sample_bytes not in all_content
    assert capture_bytes not in all_content
    assert note not in all_content
    assert figure not in all_content
    assert not list(output.rglob("*.bin"))
    assert not list(output.rglob("*.pcap"))
    assert not list(output.rglob("*.svg"))
    manifest = _read_manifest(output)
    restricted = [
        item
        for item in manifest["objects"]
        if item["type"] in {"sample", "network_capture", "protocol_note", "technical_figure"}
    ]
    assert len(restricted) == 4
    assert all(item["availability"] == "restricted" for item in restricted)
    assert all(
        "explicit_restricted_archive_flag_required" in item["restrictions"] for item in restricted
    )


def test_public_export_redacts_distribution_restricted_sample_and_ioc(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    restricted_id = str(uuid4())
    restricted_bytes = b"restricted sample bytes"
    restricted_sha = hashlib.sha256(restricted_bytes).hexdigest()
    public_bytes = b"public sample with external LLM disabled"
    public_sha = hashlib.sha256(public_bytes).hexdigest()
    archive_only_sample = b"archive-only sample"
    archive_only_sha = hashlib.sha256(archive_only_sample).hexdigest()
    assets = (
        AnnexAsset(
            canonical_id=restricted_id,
            type="sample",
            investigation_id=investigation_id,
            sha256=restricted_sha,
            source="samples+blobs",
            origin="sensitive-acquisition-origin",
            references=("source-event-id",),
            availability="restricted",
            restrictions=("tlp_red",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=restricted_bytes,
            filename=f"{restricted_sha}.bin",
            metadata={
                "archive_export_allowed": False,
                "external_llm_allowed": True,
                "license_restriction_present": False,
                "origin_kind": "manual",
                "sample_state": "validated",
                "source_object_id": "sensitive-source-object",
                "source_service": "sensitive-source-service",
                "tlp": "red",
            },
        ),
        AnnexAsset(
            canonical_id="public-external-llm-disabled",
            type="sample",
            investigation_id=investigation_id,
            sha256=public_sha,
            source="samples+blobs",
            origin="source_seed",
            availability="present",
            restrictions=("sample_external_llm_not_allowed",),
            public_distribution_allowed=True,
            archive_export_allowed=False,
            metadata={
                "archive_export_allowed": False,
                "external_llm_allowed": False,
                "sample_state": "validated",
                "tlp": "amber",
            },
        ),
        AnnexAsset(
            canonical_id="restricted-ioc",
            type="observed_ioc",
            investigation_id=investigation_id,
            sha256="a" * 64,
            source="VirusTotal",
            origin="file",
            availability="restricted",
            restrictions=("g2_envelope_revoked",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            metadata={"ioc_type": "domain", "value": "restricted.example"},
        ),
        AnnexAsset(
            canonical_id="archive-only-ioc",
            type="observed_ioc",
            investigation_id=investigation_id,
            sha256="b" * 64,
            source="VirusTotal",
            origin="file",
            public_distribution_allowed=True,
            archive_export_allowed=False,
            metadata={"ioc_type": "domain", "value": "human-public.example"},
            restrictions=("external_llm_not_allowed",),
        ),
        AnnexAsset(
            canonical_id="missing-g2-ioc",
            type="observed_ioc",
            investigation_id=investigation_id,
            sha256="c" * 64,
            source="VirusTotal",
            origin="domain",
            availability="restricted",
            restrictions=("g2_envelope_unavailable",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            metadata={"ioc_type": "domain", "value": "missing-g2.example"},
        ),
        AnnexAsset(
            canonical_id="archive-authorized-ioc",
            type="observed_ioc",
            investigation_id=investigation_id,
            source="VirusTotal",
            origin="domain",
            restrictions=("sample_license_restricted",),
            public_distribution_allowed=False,
            archive_export_allowed=True,
            metadata={"ioc_type": "domain", "value": "archive-only.example"},
        ),
        AnnexAsset(
            canonical_id="archive-authorized-sample",
            type="sample",
            investigation_id=investigation_id,
            sha256=archive_only_sha,
            source="samples+blobs",
            origin="synthetic",
            availability="present",
            restrictions=("sample_license_restricted",),
            public_distribution_allowed=False,
            archive_export_allowed=True,
            content=archive_only_sample,
        ),
    )
    output = tmp_path / "public-redacted"
    write_investigation_annex(InvestigationAnnexSnapshot(investigation_id, assets), output)

    manifest = _read_manifest(output)
    restricted = next(
        item
        for item in manifest["objects"]
        if item["type"] == "sample" and "tlp_red" in item["restrictions"]
    )
    assert restricted["availability"] == "restricted"
    assert restricted["canonical_id"] != restricted_id
    assert restricted["sha256"] is None
    assert restricted["source"] == "restricted"
    assert restricted["origin"] == "restricted"
    assert restricted["references"] == []
    assert restricted["filename"] is None
    assert restricted["metadata"] == {"status": "restricted"}
    assert "tlp_red" in restricted["restrictions"]
    external_llm_restricted = next(
        item
        for item in manifest["objects"]
        if item["type"] == "sample" and "sample_external_llm_not_allowed" in item["restrictions"]
    )
    assert external_llm_restricted["availability"] == "restricted"
    assert external_llm_restricted["sha256"] is None
    assert external_llm_restricted["metadata"] == {"status": "restricted"}
    assert external_llm_restricted["canonical_id"] != "public-external-llm-disabled"

    inventory = list(csv.DictReader((output / "samples/sample-inventory.csv").open()))
    restricted_inventory = next(item for item in inventory if "tlp_red" in item["restrictions"])
    assert restricted_inventory["canonical_id"] != restricted_id
    assert restricted_inventory["sha256"] == ""
    assert restricted_inventory["availability"] == "restricted"
    assert "tlp_red" in restricted_inventory["restrictions"]
    assert restricted_sha not in (output / "samples/sample-inventory.csv").read_text()
    assert public_sha not in (output / "samples/sample-inventory.csv").read_text()

    with (output / "iocs/observed-iocs.csv").open(newline="", encoding="utf-8") as stream:
        ioc_rows = list(csv.DictReader(stream))
    ioc = next(
        item
        for item in ioc_rows
        if item["availability"] == "restricted" and "g2_envelope_revoked" in item["restrictions"]
    )
    assert ioc["value"] == ""
    assert ioc["availability"] == "restricted"
    assert "g2_envelope_revoked" in ioc["restrictions"]
    assert any(row["value"] == "human-public.example" for row in ioc_rows)
    assert any(row["value"] == "archive-only.example" for row in ioc_rows) is False
    assert b"restricted.example" not in _file_bytes(output)["manifest.json"]
    all_public_bytes = b"\n".join(_file_bytes(output).values())
    assert b"missing-g2.example" not in all_public_bytes
    assert b"sensitive-source-object" not in all_public_bytes
    assert b"sensitive-source-service" not in all_public_bytes
    assert b"sensitive-acquisition-origin" not in all_public_bytes
    assert restricted_id.encode() not in all_public_bytes
    assert b"restricted-ioc" not in all_public_bytes
    assert b"missing-g2-ioc" not in all_public_bytes
    assert b"public-external-llm-disabled" not in all_public_bytes
    assert b"archive-authorized-sample" not in all_public_bytes
    assert public_sha.encode() not in all_public_bytes
    assert restricted_sha.encode() not in b"\n".join(_file_bytes(output).values())
    assert archive_only_sha.encode() not in all_public_bytes

    archive_output = tmp_path / "archive-only"
    write_investigation_annex(
        InvestigationAnnexSnapshot(investigation_id, assets),
        archive_output,
        include_restricted_archive=True,
    )
    with (archive_output / "iocs/observed-iocs.csv").open(newline="", encoding="utf-8") as stream:
        archive_iocs = list(csv.DictReader(stream))
    archive_ioc = next(row for row in archive_iocs if row["observation_id"] == "archive-only-ioc")
    assert archive_ioc["value"] == ""
    assert archive_ioc["availability"] == "restricted"
    archive_authorized_ioc = next(
        row for row in archive_iocs if row["observation_id"] == "archive-authorized-ioc"
    )
    assert archive_authorized_ioc["value"] == "archive-only.example"
    archive_bytes = b"\n".join(_file_bytes(archive_output).values())
    assert b"archive-only sample" in archive_bytes
    archive_manifest = _read_manifest(archive_output)
    archive_sample = next(
        item
        for item in archive_manifest["objects"]
        if item["canonical_id"] == "archive-authorized-sample"
    )
    assert archive_sample["sha256"] == archive_only_sha
    assert archive_sample["filename"] is not None
    assert _file_bytes(archive_output)[archive_sample["filename"]] == archive_only_sample


def test_public_export_restricts_detection_sources_under_red_policy(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    yara_source = b'rule red_policy { strings: $a = "private" condition: $a }'
    report = b'{"schema":"synthetic-test-report.v1","private":"report details"}'
    public_rule = b'alert http any any -> any any (msg:"public rule"; sid:1; rev:1;)'
    assets = (
        AnnexAsset(
            canonical_id="restricted-yara-rule",
            type="yara_rule",
            investigation_id=investigation_id,
            version=1,
            sha256=hashlib.sha256(yara_source).hexdigest(),
            source="detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            availability="present",
            restrictions=("tlp_red",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=yara_source,
            metadata={"candidate_id": "candidate", "rule_kind": "yara"},
        ),
        AnnexAsset(
            canonical_id="restricted-yara-report",
            type="yara_test_report",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(report).hexdigest(),
            source="AutoWork RuleWorkbench",
            origin="detection_candidate_version",
            availability="present",
            restrictions=("tlp_red",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=report,
        ),
        AnnexAsset(
            canonical_id="public-rule-external-llm-disabled",
            type="suricata_rule",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(public_rule).hexdigest(),
            source="advisor_call_payload.output.rule_source",
            origin="advisor.consult:suricata_draft",
            restrictions=("external_llm_not_allowed",),
            public_distribution_allowed=True,
            archive_export_allowed=False,
            content=public_rule,
        ),
    )
    output = tmp_path / "red-detection"
    write_investigation_annex(InvestigationAnnexSnapshot(investigation_id, assets), output)

    files = _file_bytes(output)
    assert yara_source not in b"\n".join(files.values())
    assert report not in b"\n".join(files.values())
    assert public_rule in b"\n".join(files.values())
    manifest = _read_manifest(output)
    restricted = [item for item in manifest["objects"] if item["availability"] == "restricted"]
    assert {item["type"] for item in restricted} == {"yara_rule", "yara_test_report"}
    assert all(item["canonical_id"].startswith("restricted-public-") for item in restricted)
    yara_manifest = next(item for item in restricted if item["type"] == "yara_rule")
    assert yara_manifest["sha256"] is None
    assert yara_manifest["references"] == []
    assert hashlib.sha256(yara_source).hexdigest().encode() not in b"\n".join(files.values())
    assert all("tlp_red" in item["restrictions"] for item in restricted)
    assert b"restricted-yara-rule" not in b"\n".join(files.values())
    assert b"restricted-yara-report" not in b"\n".join(files.values())
    assert any(
        item["canonical_id"] == "public-rule-external-llm-disabled"
        and item["availability"] == "present"
        for item in manifest["objects"]
    )


def test_public_export_fails_closed_when_distribution_policy_is_unresolved(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    sensitive_types = (
        "yara_rule",
        "suricata_rule",
        "yara_test_report",
        "suricata_test_report",
        "observed_ioc",
        "sample",
        "network_capture",
        "evidence_observation",
        "protocol_note",
        "technical_note",
        "technical_figure",
        "discovered_sample",
    )
    assets = (
        *(
            AnnexAsset(
                canonical_id=f"unresolved-{asset_type}",
                type=asset_type,
                investigation_id=investigation_id,
                version=1,
                sha256=hashlib.sha256(f"PRIVATE_CONTENT_{asset_type}".encode()).hexdigest(),
                source="private-evidence-source",
                origin="private-evidence-origin",
                references=(f"derived-evidence-id-{asset_type}",),
                content=f"PRIVATE_CONTENT_{asset_type}".encode(),
                filename=f"private-{asset_type}.bin",
                metadata={
                    "value": f"private-value-{asset_type}",
                    "candidate_id": "private-candidate",
                },
            )
            for asset_type in sensitive_types
        ),
        AnnexAsset(
            canonical_id="absent-unresolved-candidate-id",
            type="yara_test_report",
            investigation_id=investigation_id,
            source="canonical-test-query",
            origin="detection_candidate_version",
            availability="absent",
            references=("absent-candidate-version-id",),
            metadata={"candidate_id": "absent-candidate-id", "test_refs": ["private-ref"]},
        ),
    )
    output = tmp_path / "unresolved-policy"
    write_investigation_annex(InvestigationAnnexSnapshot(investigation_id, assets), output)

    files = _file_bytes(output)
    serialized_files = b"\n".join(files.values())
    manifest = _read_manifest(output)
    for asset_type in sensitive_types:
        entry = next(
            item
            for item in manifest["objects"]
            if item["type"] == asset_type
            and "distribution_policy_unresolved" in item["restrictions"]
        )
        assert entry["availability"] == "restricted"
        assert "distribution_policy_unresolved" in entry["restrictions"]
        assert entry["sha256"] is None
        assert entry["references"] == []
        assert entry["source"] == entry["origin"] == "restricted"
        assert entry["filename"] is None
        assert entry["metadata"] == {"status": "restricted"}
        assert f"PRIVATE_CONTENT_{asset_type}".encode() not in serialized_files
        assert f"private-value-{asset_type}".encode() not in serialized_files
        assert f"derived-evidence-id-{asset_type}".encode() not in serialized_files
        assert (
            hashlib.sha256(f"PRIVATE_CONTENT_{asset_type}".encode()).hexdigest().encode()
            not in serialized_files
        )
        assert f"unresolved-{asset_type}".encode() not in serialized_files
    absent_reports = [
        item
        for item in manifest["objects"]
        if item["type"] == "yara_test_report"
        and "distribution_policy_unresolved" in item["restrictions"]
    ]
    assert len(absent_reports) == 2
    assert all(item["availability"] == "restricted" for item in absent_reports)
    assert all(item["references"] == [] for item in absent_reports)
    assert b"absent-unresolved-candidate-id" not in serialized_files
    assert b"absent-candidate-version-id" not in serialized_files
    assert b"absent-candidate-id" not in serialized_files
    assert b"private-evidence-source" not in serialized_files
    assert b"private-evidence-origin" not in serialized_files


def test_restricted_archive_requires_explicit_flag_and_policy_eligibility(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    allowed = b"sample bytes behind policy gate"
    denied = b"license-restricted sample bytes"
    capture = b"sensitive capture bytes"
    figure = b'<svg xmlns="http://www.w3.org/2000/svg"><text>canonical</text></svg>'
    assets = (
        AnnexAsset(
            canonical_id="allowed",
            type="sample",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(allowed).hexdigest(),
            source="samples+blobs",
            origin="vt_seed",
            content=allowed,
            metadata={"archive_export_allowed": True, "external_llm_allowed": True},
        ),
        AnnexAsset(
            canonical_id="denied",
            type="sample",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(denied).hexdigest(),
            source="samples+blobs",
            origin="manual",
            content=denied,
            restrictions=(
                "sample_license_restricted",
                "sample_external_llm_not_allowed",
                "tlp_red",
                "sample_rejected_or_revoked",
            ),
            metadata={"archive_export_allowed": False, "external_llm_allowed": False},
        ),
        AnnexAsset(
            canonical_id="capture",
            type="network_capture",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(capture).hexdigest(),
            source="network.quarantine",
            origin="network.quarantine",
            content=capture,
            metadata={"archive_export_allowed": True, "external_llm_allowed": True},
        ),
        AnnexAsset(
            canonical_id="figure",
            type="technical_figure",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(figure).hexdigest(),
            source="media_assets+blobs",
            origin="analyst_opinion.protocol_figures",
            content=figure,
            filename="opinion-figure.svg",
            metadata={"archive_export_allowed": True, "figure_kind": "message_flow"},
        ),
    )
    output = tmp_path / "restricted"
    write_investigation_annex(
        InvestigationAnnexSnapshot(investigation_id, assets),
        output,
        include_restricted_archive=True,
    )

    assert (
        output / "samples/restricted/" / f"{hashlib.sha256(allowed).hexdigest()}.bin"
    ).read_bytes() == allowed
    assert (
        output / "network/restricted/captures/" / f"{hashlib.sha256(capture).hexdigest()}.pcap"
    ).read_bytes() == capture
    assert (output / "technical/figures/restricted/opinion-figure.svg").read_bytes() == figure
    assert not list(output.glob("samples/restricted/*license*"))
    for path in output.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(output).as_posix()
        expected_mode = (
            0o400
            if relative.startswith(("samples/restricted/", "network/restricted/captures/"))
            else 0o600
        )
        assert path.stat().st_mode & 0o777 == expected_mode
    manifest = _read_manifest(output)
    denied_entry = next(item for item in manifest["objects"] if item["canonical_id"] == "denied")
    assert denied_entry["availability"] == "restricted"
    assert "canonical_policy_denied" in denied_entry["restrictions"]
    assert {
        "sample_license_restricted",
        "sample_external_llm_not_allowed",
        "tlp_red",
        "sample_rejected_or_revoked",
    } <= set(denied_entry["restrictions"])


def test_manifest_lists_missing_collections_and_verifies_generated_files(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    observation_id = str(uuid4())
    assets = (
        AnnexAsset(
            canonical_id="candidate-without-test-report",
            type="yara_rule",
            investigation_id=investigation_id,
            version=1,
            source="detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            content=b"rule candidate { condition: true }\n",
            public_distribution_allowed=True,
            metadata={
                "candidate_id": "candidate-identity",
                "candidate_status": "candidate",
                "rule_kind": "yara",
                "test_refs": (),
            },
        ),
        AnnexAsset(
            canonical_id=observation_id,
            type="observed_ioc",
            investigation_id=investigation_id,
            sha256="a" * 64,
            source="VirusTotal",
            origin="file",
            references=(observation_id,),
            public_distribution_allowed=True,
            metadata={"ioc_type": "file", "value": "b" * 64, "relation": "observed"},
        ),
        AnnexAsset(
            canonical_id="candidate",
            type="discovered_sample",
            investigation_id=investigation_id,
            sha256="c" * 64,
            source="VirusTotal",
            origin="search_candidate",
            public_distribution_allowed=True,
            metadata={
                "value": "c" * 64,
                "status": "not_acquired",
                "origin_kind": "virustotal_observation",
                "qualification": "unknown",
                "relation": "virustotal_candidate_not_family_proof",
                "pivot_criteria": "canonical_virustotal_summary_candidate",
            },
        ),
    )
    output = tmp_path / "empty-rules"
    write_investigation_annex(InvestigationAnnexSnapshot(investigation_id, assets), output)

    manifest = _read_manifest(output)
    _assert_manifest_files(output, manifest)
    objects = manifest["objects"]
    assert isinstance(objects, list)
    absent = {item["type"] for item in objects if item["availability"] == "absent"}
    assert {"yara_test_report", "suricata_rule", "suricata_test_report"} <= absent
    yara_index = json.loads((output / "yara/tests/index.json").read_bytes())
    assert yara_index[0]["test_report_ids"] == []
    with (output / "iocs/observed-iocs.csv").open(encoding="utf-8", newline="") as stream:
        ioc_rows = list(csv.DictReader(stream))
    assert ioc_rows == [
        {
            "type": "file",
            "value": "b" * 64,
            "observation_id": observation_id,
            "source": "VirusTotal",
            "observation_sha256": "a" * 64,
            "relation": "observed",
            "availability": "present",
            "restrictions": "",
        }
    ]
    with (output / "iocs/discovered-samples.csv").open(encoding="utf-8", newline="") as stream:
        discovered = list(csv.DictReader(stream))
    assert discovered[0]["relation"] == "virustotal_candidate_not_family_proof"
    assert "family" not in discovered[0]["relation"].replace("not_family_proof", "")


def test_hostile_names_are_sanitized_and_collisions_are_disambiguated(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    first = b"first protocol note"
    second = b"second protocol note"
    assets = (
        AnnexAsset(
            canonical_id="protocol-1",
            type="protocol_note",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(first).hexdigest(),
            source="protocol_model_blob",
            origin="canonical_evidence_observation",
            content=first,
            filename="../../../../same.json",
            metadata={"archive_export_allowed": True},
        ),
        AnnexAsset(
            canonical_id="protocol-2",
            type="protocol_note",
            investigation_id=investigation_id,
            sha256=hashlib.sha256(second).hexdigest(),
            source="protocol_model_blob",
            origin="canonical_evidence_observation",
            content=second,
            filename="../same.json",
            metadata={"archive_export_allowed": True},
        ),
    )
    output = tmp_path / "hostile"
    write_investigation_annex(
        InvestigationAnnexSnapshot(investigation_id, assets),
        output,
        include_restricted_archive=True,
    )

    note_paths = sorted((output / "network/protocol-notes/restricted").glob("*.json"))
    assert len(note_paths) == 2
    assert len({path.name for path in note_paths}) == 2
    assert sorted(path.read_bytes() for path in note_paths) == sorted((first, second))
    assert not (tmp_path / "same.json").exists()


def test_destination_is_not_overwritten_without_explicit_flag(tmp_path: Path) -> None:
    investigation_id = str(uuid4())
    snapshot = InvestigationAnnexSnapshot(investigation_id)
    replacement_snapshot = InvestigationAnnexSnapshot(
        investigation_id,
        (
            AnnexAsset(
                canonical_id="new-ioc",
                type="observed_ioc",
                investigation_id=investigation_id,
                source="canonical_observation",
                origin="test",
                metadata={"ioc_type": "domain", "value": "new.example"},
                public_distribution_allowed=True,
            ),
        ),
    )
    destination = tmp_path / "export"
    write_investigation_annex(snapshot, destination)
    before = _file_bytes(destination)

    with pytest.raises(FileExistsError):
        write_investigation_annex(replacement_snapshot, destination)
    assert _file_bytes(destination) == before
    write_investigation_annex(replacement_snapshot, destination, overwrite=True)
    assert _file_bytes(destination) != before
    manifest = _read_manifest(destination)
    assert any(item["canonical_id"] == "new-ioc" for item in manifest["objects"])
