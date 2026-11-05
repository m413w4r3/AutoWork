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
from cti_app.domain.investigation import (
    DetectionCandidateProvenance,
    DetectionCandidateVersion,
    MotifProvenance,
)
from cti_app.infrastructure.database import investigation_annex_reader
from cti_app.infrastructure.database.investigation_annex_reader import (
    _provenance_event_public_restrictions_by_sha,
    _public_distribution_policy,
    _resolve_public_distribution_for_assets,
    _suricata_candidate_metadata,
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
    assert metadata["origin"] == "not_recorded"
    assert metadata["origin_tool"] == "not_recorded"
    assert metadata["origin_invocation_id"] == "not_recorded"
    assert metadata["source_observation_ids"] == ()
    assert metadata["parent_version"] is None


@pytest.mark.asyncio
async def test_yara_origin_metadata_uses_candidate_provenance_without_motifs() -> None:
    source = "rule unattributed { condition: true }"
    investigation_id = uuid4()
    candidate_id = uuid4()
    candidate_version_id = uuid4()
    source_observation_id = uuid4()
    invocation_id = uuid4()
    candidate = DetectionCandidateVersion(
        id=candidate_version_id,
        candidate_id=candidate_id,
        investigation_id=investigation_id,
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        motif_provenance=(),
        provenance=DetectionCandidateProvenance(
            kind="bridge_yara_draft",
            metadata={
                "author": "bridge",
                "change_kind": "bridge_yara_draft",
                "bridge_invocation_id": str(invocation_id),
            },
            observation_refs=(source_observation_id,),
        ),
    )

    class UnusedArtifactReader:
        async def read_bytes(self, requested_blob_id, *, max_bytes=None):
            raise AssertionError("Explicit candidate provenance needs no blob read")

    metadata = await _yara_version_origin_metadata(
        candidate,
        observations=(),
        invocations=(),
        artifact_store=UnusedArtifactReader(),
    )

    assert metadata["author"] == "bridge"
    assert metadata["author_status"] == "recorded"
    assert metadata["origin"] == "bridge"
    assert metadata["origin_invocation_id"] == str(invocation_id)
    assert metadata["source_observation_ids"] == (str(source_observation_id),)
    assert metadata["parent_version"] is None


def test_suricata_candidate_metadata_projects_import_provenance_and_statuses() -> None:
    observation_id = uuid4()
    source_observation_id = uuid4()
    bridge_invocation_id = uuid4()
    local_invocation_id = uuid4()
    metadata = _suricata_candidate_metadata(
        {
            "candidate_id": "bridge-candidate-7",
            "candidate_version": 7,
            "candidate_provenance": {
                "candidate_id": "bridge-candidate-7",
                "candidate_version": 7,
                "source_observation_id": str(source_observation_id),
                "bridge_invocation_id": str(bridge_invocation_id),
                "local_tool_invocation_id": str(local_invocation_id),
            },
            "rule_syntax_validation": "failed",
            "rule_syntax_validation_error": "synthetic syntax failure",
            "pcap_test_status": "not_tested",
        },
        observation_id=observation_id,
        candidate_version=7,
        fact_refs=("fact-1",),
        imported=True,
        envelope_external_llm_allowed=True,
        envelope_allowed=False,
        envelope_tlp="GREEN",
        research_permissive=True,
    )

    assert metadata["author"] == "bridge"
    assert metadata["origin"] == "bridge"
    assert metadata["origin_tool"] == "rule.import_suricata_bridge_candidate"
    assert metadata["origin_invocation_id"] == str(bridge_invocation_id)
    assert metadata["local_tool_invocation_id"] == str(local_invocation_id)
    assert metadata["candidate_version"] == 7
    assert metadata["source_observation_ids"] == (
        str(source_observation_id),
        str(observation_id),
    )
    assert metadata["fact_refs"] == ("fact-1",)
    assert metadata["rule_syntax_validation"] == "failed"
    assert metadata["rule_syntax_validation_error"] == "synthetic syntax failure"
    assert metadata["pcap_test_status"] == "not_tested"
    assert metadata["external_llm_allowed"] is True


def test_suricata_candidate_metadata_marks_missing_provenance_not_recorded() -> None:
    metadata = _suricata_candidate_metadata(
        {},
        observation_id=uuid4(),
        candidate_version=None,
        fact_refs=(),
        imported=False,
        envelope_external_llm_allowed=False,
        envelope_allowed=False,
        envelope_tlp="AMBER",
        research_permissive=False,
    )

    assert metadata["author"] is None
    assert metadata["author_status"] == "not_recorded"
    assert metadata["origin_tool"] == "advisor.consult"
    assert metadata["origin_invocation_id"] == "not_recorded"
    assert metadata["parent_version"] == "not_recorded"


@pytest.mark.parametrize(
    ("case", "candidate_sha", "global_restrictions", "sample_restrictions", "allowed"),
    (
        ("unacquired", "a" * 64, (), {"b" * 64: {"tlp_red"}}, True),
        ("shared-red-sample", "b" * 64, (), {"b" * 64: {"tlp_red"}}, False),
        (
            "revoked-envelope",
            "a" * 64,
            ("g2_envelope_revoked",),
            {"b" * 64: {"tlp_red"}},
            False,
        ),
    ),
)
def test_discovered_sample_distribution_uses_its_own_policy_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    candidate_sha: str,
    global_restrictions: tuple[str, ...],
    sample_restrictions: dict[str, set[str]],
    allowed: bool,
) -> None:
    if case == "revoked-envelope":
        monkeypatch.setattr(
            investigation_annex_reader,
            "g2_envelope_authorization_state",
            lambda envelope, decisions: SimpleNamespace(revoked=True),
        )
        policy_allowed, resolved_restrictions = _public_distribution_policy(
            SimpleNamespace(disclosure=SimpleNamespace(tlp="green")), None, ()
        )
        assert policy_allowed is False
        assert resolved_restrictions == global_restrictions

    investigation_id = str(uuid4())
    observation_id = str(uuid4())
    pivot_sha = "b" * 64
    candidate = AnnexAsset(
        canonical_id=f"discovery:{case}",
        type="discovered_sample",
        investigation_id=investigation_id,
        sha256=candidate_sha,
        source="VirusTotal",
        origin="file",
        references=(observation_id,),
        availability="present",
        public_distribution_allowed=True,
        metadata={"value": candidate_sha, "status": "not_acquired"},
    )
    restricted_observation = AnnexAsset(
        canonical_id=observation_id,
        type="evidence_observation",
        investigation_id=investigation_id,
        source="evidence_observations",
        origin="file",
        availability="restricted",
        restrictions=("tlp_red",),
        public_distribution_allowed=False,
        metadata={"status": "restricted"},
    )
    pivot_observation = SimpleNamespace(
        id=observation_id,
        object_id=pivot_sha,
        object_type="file",
    )

    resolved = _resolve_public_distribution_for_assets(
        (restricted_observation, candidate),
        public_distribution_allowed=not global_restrictions,
        public_distribution_restrictions=global_restrictions,
        sample_public_restrictions_by_sha=sample_restrictions,
        known_sample_shas={pivot_sha},
        observations=(pivot_observation,),
    )
    resolved_candidate = next(asset for asset in resolved if asset.type == "discovered_sample")
    assert resolved_candidate.public_distribution_allowed is allowed

    output = tmp_path / case
    write_investigation_annex(InvestigationAnnexSnapshot(investigation_id, tuple(resolved)), output)
    files = _file_bytes(output)
    manifest = _read_manifest(output)
    entry = next(item for item in manifest["objects"] if item["type"] == "discovered_sample")
    if allowed:
        assert entry["availability"] == "present"
        assert entry["sha256"] == candidate_sha
        assert entry["metadata"]["status"] == "not_acquired"
        assert candidate_sha.encode() in b"\n".join(files.values())
    else:
        assert entry["availability"] == "restricted"
        assert entry["sha256"] is None
        assert entry["metadata"] == {"status": "restricted"}
        assert candidate_sha.encode() not in b"\n".join(files.values())


def test_red_provenance_event_restricts_only_its_sample_hash(tmp_path: Path) -> None:
    sample_id = uuid4()
    red_sample = SimpleNamespace(id=sample_id)
    red_event = SimpleNamespace(tlp="RED")
    unrelated_sample = SimpleNamespace(id=uuid4())
    green_event = SimpleNamespace(tlp="GREEN")

    restrictions = _provenance_event_public_restrictions_by_sha(
        {
            "a" * 64: (red_sample,),
            "b" * 64: (unrelated_sample,),
        },
        {
            sample_id: (red_event,),
            unrelated_sample.id: (green_event,),
        },
        red_event_sample_shas=("c" * 64,),
    )

    assert restrictions == {
        "a" * 64: {"provenance_event_tlp_red"},
        "c" * 64: {"provenance_event_tlp_red"},
    }
    candidate = AnnexAsset(
        canonical_id="red-event-candidate",
        type="discovered_sample",
        investigation_id=str(uuid4()),
        sha256="c" * 64,
        source="VirusTotal",
        origin="file",
        public_distribution_allowed=True,
        metadata={"status": "not_acquired", "value": "c" * 64},
    )
    resolved = _resolve_public_distribution_for_assets(
        (candidate,),
        public_distribution_allowed=True,
        public_distribution_restrictions=(),
        sample_public_restrictions_by_sha=restrictions,
        known_sample_shas={"c" * 64},
        observations=(),
    )
    assert resolved[0].public_distribution_allowed is False
    assert "provenance_event_tlp_red" in resolved[0].restrictions

    output = tmp_path / "red-provenance"
    write_investigation_annex(
        InvestigationAnnexSnapshot(candidate.investigation_id, tuple(resolved)), output
    )
    manifest = _read_manifest(output)
    entry = next(item for item in manifest["objects"] if item["type"] == "discovered_sample")
    assert entry["availability"] == "restricted"
    assert entry["sha256"] is None
    assert ("c" * 64).encode() not in b"\n".join(_file_bytes(output).values())


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
    archive_ioc = next(
        row
        for row in archive_iocs
        if row["availability"] == "restricted" and "canonical_policy_denied" in row["restrictions"]
    )
    assert archive_ioc["value"] == ""
    assert archive_ioc["availability"] == "restricted"
    assert archive_ioc["observation_id"] != "archive-only-ioc"
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
            or relative.startswith(("restricted/samples/", "restricted/network/captures/"))
            else 0o600
        )
        assert path.stat().st_mode & 0o777 == expected_mode
    manifest = _read_manifest(output)
    denied_entry = next(
        item
        for item in manifest["objects"]
        if item["type"] == "sample" and "canonical_policy_denied" in item["restrictions"]
    )
    assert denied_entry["availability"] == "restricted"
    assert "canonical_policy_denied" in denied_entry["restrictions"]
    assert denied_entry["canonical_id"] != "denied"
    assert b'"canonical_id": "denied"' not in b"\n".join(_file_bytes(output).values())
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


def test_restricted_archive_masks_every_policy_denied_sensitive_asset_deterministically(
    tmp_path: Path,
) -> None:
    investigation_id = str(uuid4())
    sensitive_types = (
        "sample",
        "network_capture",
        "discovered_sample",
        "technical_note",
        "technical_figure",
        "protocol_note",
        "observed_ioc",
        "yara_rule",
        "suricata_rule",
        "yara_test_report",
        "suricata_test_report",
    )
    denied_assets = []
    denied_values: dict[str, tuple[str, bytes, str]] = {}
    for asset_type in sensitive_types:
        content = f"private-content-{asset_type}".encode()
        digest = hashlib.sha256(content).hexdigest()
        canonical_id = digest if asset_type == "sample" else f"private-canonical-id-{asset_type}"
        filename = f"{digest}.bin"
        denied_values[asset_type] = (canonical_id, content, digest)
        denied_assets.append(
            AnnexAsset(
                canonical_id=canonical_id,
                type=asset_type,
                investigation_id=investigation_id,
                version=7,
                sha256=digest,
                source=f"private-source-{asset_type}",
                origin=f"private-origin-{asset_type}",
                references=(f"private-proof-reference-{asset_type}",),
                availability="present",
                restrictions=("canonical_policy_denied",),
                public_distribution_allowed=True,
                archive_export_allowed=(
                    None if asset_type in {"technical_figure", "protocol_note"} else False
                ),
                content=content,
                filename=filename,
                metadata={
                    "archive_export_allowed": False,
                    "candidate_id": f"private-candidate-{asset_type}",
                    "ioc_type": "domain",
                    "value": f"private-value-{asset_type}",
                    "related_to": digest,
                    "source_object_id": f"private-object-{asset_type}",
                },
            )
        )
    allowed_rule = b'rule archive_allowed { strings: $a = "allowed" condition: $a }'
    allowed_rule_sha = hashlib.sha256(allowed_rule).hexdigest()
    allowed_ioc_value = "archive-allowed.example"
    authorized_assets = (
        AnnexAsset(
            canonical_id="archive-authorized-rule",
            type="yara_rule",
            investigation_id=investigation_id,
            version=1,
            sha256=allowed_rule_sha,
            source="canonical_detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            public_distribution_allowed=True,
            archive_export_allowed=True,
            content=allowed_rule,
            metadata={"archive_export_allowed": True, "rule_kind": "yara"},
        ),
        AnnexAsset(
            canonical_id="archive-authorized-ioc",
            type="observed_ioc",
            investigation_id=investigation_id,
            source="canonical_observation",
            origin="domain",
            public_distribution_allowed=True,
            archive_export_allowed=True,
            metadata={"ioc_type": "domain", "value": allowed_ioc_value},
        ),
    )
    snapshot = InvestigationAnnexSnapshot(investigation_id, (*denied_assets, *authorized_assets))
    first_output = tmp_path / "restricted-archive-first"
    second_output = tmp_path / "restricted-archive-second"
    write_investigation_annex(snapshot, first_output, include_restricted_archive=True)
    write_investigation_annex(snapshot, second_output, include_restricted_archive=True)

    files = _file_bytes(first_output)
    serialized = b"\n".join(files.values())
    manifest = _read_manifest(first_output)
    _assert_manifest_files(first_output, manifest)
    assert files == _file_bytes(second_output)
    second_manifest = _read_manifest(second_output)
    _assert_manifest_files(second_output, second_manifest)
    assert manifest == second_manifest

    objects = manifest["objects"]
    for asset_type, (canonical_id, content, digest) in denied_values.items():
        entry = next(
            item
            for item in objects
            if item["type"] == asset_type and item["availability"] == "restricted"
        )
        assert entry["availability"] == "restricted", f"{asset_type}: {entry}"
        assert "canonical_policy_denied" in entry["restrictions"]
        assert entry["canonical_id"] != canonical_id
        assert entry["canonical_id"].startswith(f"restricted-archive-{asset_type}-")
        assert entry["sha256"] is None
        assert entry["source"] == entry["origin"] == "restricted"
        assert entry["references"] == []
        assert entry["filename"] is None
        assert entry["metadata"]["status"] == "restricted"
        assert entry["metadata"].get("value") is None
        assert entry["metadata"].get("related_to") is None
        assert canonical_id.encode() not in serialized
        assert content not in serialized
        assert digest.encode() not in serialized
        assert f"{digest}.bin".encode() not in serialized
        assert f"private-source-{asset_type}".encode() not in serialized
        assert f"private-origin-{asset_type}".encode() not in serialized
        assert f"private-proof-reference-{asset_type}".encode() not in serialized
        assert f"private-value-{asset_type}".encode() not in serialized
        assert f"private-candidate-{asset_type}".encode() not in serialized
        assert f"private-object-{asset_type}".encode() not in serialized

    allowed_entry = next(
        item for item in objects if item["canonical_id"] == "archive-authorized-rule"
    )
    assert allowed_entry["availability"] == "present"
    assert allowed_entry["sha256"] == allowed_rule_sha
    assert allowed_rule in serialized
    allowed_ioc_entry = next(
        item for item in objects if item["canonical_id"] == "archive-authorized-ioc"
    )
    assert allowed_ioc_entry["availability"] == "present"
    assert allowed_ioc_value.encode() in serialized


def test_related_authorized_asset_cannot_reveal_withheld_sha_in_either_mode(
    tmp_path: Path,
) -> None:
    investigation_id = str(uuid4())
    withheld_content = b"policy-withheld sample"
    withheld_sha = hashlib.sha256(withheld_content).hexdigest()
    authorized_sha = "b" * 64
    assets = (
        AnnexAsset(
            canonical_id="withheld-sample-id",
            type="sample",
            investigation_id=investigation_id,
            sha256=withheld_sha,
            source="private-sample-source",
            origin="private-sample-origin",
            references=("private-sample-proof",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=withheld_content,
            metadata={"archive_export_allowed": False},
        ),
        AnnexAsset(
            canonical_id="authorized-related-discovery",
            type="discovered_sample",
            investigation_id=investigation_id,
            sha256=authorized_sha,
            source="canonical_discovery_source",
            origin="analyst_pivot",
            references=(withheld_sha,),
            public_distribution_allowed=True,
            archive_export_allowed=True,
            metadata={
                "archive_export_allowed": True,
                "value": authorized_sha,
                "related_to": withheld_sha,
                "relation": "reported_related_to_not_family_proof",
            },
        ),
    )
    snapshot = InvestigationAnnexSnapshot(investigation_id, assets)

    for mode, include_restricted_archive in (("public", False), ("archive", True)):
        output = tmp_path / mode
        write_investigation_annex(
            snapshot,
            output,
            include_restricted_archive=include_restricted_archive,
        )
        serialized = b"\n".join(_file_bytes(output).values())
        manifest = _read_manifest(output)
        assert withheld_sha.encode() not in serialized
        allowed_entry = next(
            item
            for item in manifest["objects"]
            if item["canonical_id"] == "authorized-related-discovery"
        )
        assert allowed_entry["availability"] == "present"
        assert allowed_entry["sha256"] == authorized_sha
        assert allowed_entry["references"] == []
        assert allowed_entry["metadata"]["related_to"] is None
        with (output / "iocs/discovered-samples.csv").open(encoding="utf-8", newline="") as stream:
            discovered = list(csv.DictReader(stream))
        assert all(row["related_to"] != withheld_sha for row in discovered)


def test_restricted_archive_fails_closed_for_sensitive_assets_without_policy(
    tmp_path: Path,
) -> None:
    investigation_id = str(uuid4())
    content = b"rule with unresolved archive policy"
    digest = hashlib.sha256(content).hexdigest()
    asset = AnnexAsset(
        canonical_id="unresolved-archive-rule-id",
        type="yara_rule",
        investigation_id=investigation_id,
        sha256=digest,
        source="private-rule-source",
        origin="private-rule-origin",
        references=("private-rule-reference",),
        public_distribution_allowed=True,
        content=content,
        metadata={"candidate_id": "private-rule-candidate"},
    )
    output = tmp_path / "unresolved-archive-policy"
    write_investigation_annex(
        InvestigationAnnexSnapshot(investigation_id, (asset,)),
        output,
        include_restricted_archive=True,
    )

    files = _file_bytes(output)
    serialized = b"\n".join(files.values())
    manifest = _read_manifest(output)
    entry = next(item for item in manifest["objects"] if item["type"] == "yara_rule")
    assert entry["availability"] == "restricted"
    assert "archive_export_policy_unresolved" in entry["restrictions"]
    assert entry["canonical_id"] != "unresolved-archive-rule-id"
    assert entry["sha256"] is None
    assert entry["references"] == []
    assert entry["source"] == entry["origin"] == "restricted"
    assert content not in serialized
    assert digest.encode() not in serialized
    assert b"private-rule-source" not in serialized
    assert b"private-rule-origin" not in serialized
    assert b"private-rule-reference" not in serialized
    assert b"private-rule-candidate" not in serialized


def test_m14_research_profile_writes_private_blobs_and_keeps_public_metadata(
    tmp_path: Path,
) -> None:
    investigation_id = str(uuid4())
    sample = b"synthetic acquired sample bytes"
    sample_sha = hashlib.sha256(sample).hexdigest()
    capture = b"synthetic pcap bytes"
    capture_sha = hashlib.sha256(capture).hexdigest()
    yara_v1 = b'rule m14_candidate { strings: $a = "old" condition: $a }\n'
    yara_v2 = b'rule m14_candidate { strings: $a = "new" condition: $a }\n'
    suricata = b'alert http any any -> any any (msg:"synthetic"; sid:1234567;)\n'
    yara_report = b'{"status":"not_tested","matches":[]}\n'
    assets = (
        AnnexAsset(
            canonical_id="sample-acquisition-1",
            type="sample",
            investigation_id=investigation_id,
            source="samples+blobs",
            origin="virustotal",
            sha256=sample_sha,
            references=("sample-association-1", "observation-1"),
            archive_export_allowed=False,
            public_distribution_allowed=False,
            content=sample,
            metadata={
                "tlp": "red",
                "license_restriction_present": True,
                "license_restriction": "qualification-only",
                "external_llm_allowed": False,
                "do_not_submit": True,
                "sample_state": "quarantined",
            },
        ),
        AnnexAsset(
            canonical_id="capture-acquisition-1",
            type="network_capture",
            investigation_id=investigation_id,
            source="network.quarantine",
            origin="network_capture",
            sha256=capture_sha,
            references=("capture-observation-1", sample_sha),
            archive_export_allowed=False,
            public_distribution_allowed=False,
            content=capture,
            filename="capture.pcap",
            metadata={"tlp": "red", "external_llm_allowed": False, "do_not_submit": True},
        ),
        AnnexAsset(
            canonical_id="candidate-v1",
            type="yara_rule",
            investigation_id=investigation_id,
            source="detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            version=1,
            sha256=hashlib.sha256(yara_v1).hexdigest(),
            references=("candidate-id",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=yara_v1,
            metadata={"candidate_id": "candidate-id", "candidate_status": "experimental"},
        ),
        AnnexAsset(
            canonical_id="candidate-v2",
            type="yara_rule",
            investigation_id=investigation_id,
            source="detection_candidate_versions.source",
            origin="canonical_detection_candidate_versions",
            version=2,
            sha256=hashlib.sha256(yara_v2).hexdigest(),
            references=("candidate-id", "candidate-v1"),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=yara_v2,
            metadata={"candidate_id": "candidate-id", "candidate_status": "tested"},
        ),
        AnnexAsset(
            canonical_id="candidate-test-1",
            type="yara_test_report",
            investigation_id=investigation_id,
            source="canonical_test_observation",
            origin="yara_test",
            sha256=hashlib.sha256(yara_report).hexdigest(),
            references=("candidate-v2",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=yara_report,
        ),
        AnnexAsset(
            canonical_id="suricata-rule-1",
            type="suricata_rule",
            investigation_id=investigation_id,
            source="advisor_call_payload.output.rule_source",
            origin="advisor.consult:suricata_draft",
            version=1,
            sha256=hashlib.sha256(suricata).hexdigest(),
            references=("capture-observation-1", sample_sha),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            content=suricata,
            metadata={
                "candidate_id": "suricata-candidate",
                "candidate_status": "experimental",
                "replay_status": "not_tested",
            },
        ),
        AnnexAsset(
            canonical_id="observed-ioc-1",
            type="observed_ioc",
            investigation_id=investigation_id,
            source="virustotal",
            origin="domain",
            sha256=hashlib.sha256(b"ioc observation").hexdigest(),
            references=("observation-1", sample_sha),
            availability="restricted",
            restrictions=("tlp_red",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            metadata={"ioc_type": "domain", "value": "example.invalid"},
        ),
        AnnexAsset(
            canonical_id="discovered-sample-1",
            type="discovered_sample",
            investigation_id=investigation_id,
            source="virustotal",
            origin="summary",
            sha256=capture_sha,
            references=("observation-1", sample_sha),
            availability="restricted",
            restrictions=("tlp_red",),
            public_distribution_allowed=False,
            archive_export_allowed=False,
            metadata={
                "value": capture_sha,
                "status": "acquired_during_investigation",
                "related_to": sample_sha,
                "relation": "candidate_not_family_proof",
            },
        ),
    )
    snapshot = InvestigationAnnexSnapshot(investigation_id, assets)

    public_output = tmp_path / "research-public"
    write_investigation_annex(
        snapshot,
        public_output,
        profile="m14_research_permissive",
        opinion_preview_unavailable_reason="indisponible: aucun avis brouillon disponible",
    )
    public_files = _file_bytes(public_output)
    public_bytes = b"\n".join(
        content
        for path, content in public_files.items()
        if not path.startswith("restricted/") and not path.startswith("technical-archive.tar.gz")
    )
    assert sample not in public_bytes
    assert capture not in public_bytes
    assert sample_sha.encode() in public_bytes
    assert capture_sha.encode() in public_bytes
    assert capture_sha.encode() in public_files["network/captures/index.csv"]
    assert sample_sha.encode() in public_files["samples/sample-inventory.csv"]
    public_manifest = _read_manifest(public_output)
    assert public_manifest["profile"] == "m14_research_permissive"
    assert public_manifest["opinion_preview"] == {
        "status": "indisponible",
        "reason": "indisponible: aucun avis brouillon disponible",
    }
    public_objects = public_manifest["objects"]
    sample_object = next(
        item for item in public_objects if item["canonical_id"] == "sample-acquisition-1"
    )
    assert sample_object["sha256"] == sample_sha
    assert sample_object["references"] == ["observation-1", "sample-association-1"]
    assert sample_object["metadata"]["tlp"] == "red"
    assert sample_object["metadata"]["license_restriction"] == "qualification-only"
    assert sample_object["metadata"]["external_llm_allowed"] is False
    assert sample_object["metadata"]["do_not_submit"] is True
    assert public_files["yara/candidate-001-v1.yar"] == yara_v1
    assert public_files["yara/candidate-001-v2.yar"] == yara_v2
    assert public_files["yara/tests/report-001.json"] == yara_report
    assert public_files["suricata/candidate-001-v1.rules"] == suricata
    assert json.loads(public_files["suricata/tests/status-001.json"])["status"] == (
        "non testée sur PCAP"
    )
    assert b"example.invalid" in public_files["iocs/observed-iocs.csv"]
    assert capture_sha.encode() in public_files["iocs/discovered-samples.csv"]

    archive_output = tmp_path / "research-private"
    preview_pdf = b"%PDF-1.4 synthetic draft preview\n"
    opinion_id = str(uuid4())
    write_investigation_annex(
        snapshot,
        archive_output,
        profile="m14_research_permissive",
        include_restricted_archive=True,
        opinion_preview_pdf=preview_pdf,
        opinion_preview_metadata={
            "opinion_id": opinion_id,
            "opinion_version": 3,
            "opinion_sha256": "a" * 64,
            "base_publication_sha256": "b" * 64,
            "lineage_sha256": "c" * 64,
        },
    )
    archive_files = _file_bytes(archive_output)
    assert archive_files["opinion/opinion-draft.pdf"] == preview_pdf
    assert archive_files[f"restricted/samples/{sample_sha}.bin"] == sample
    assert archive_files[f"restricted/network/captures/{capture_sha}.pcap"] == capture
    assert capture_sha.encode() in archive_files["network/captures/index.csv"]
    tar_content = archive_files["technical-archive.tar.gz"]
    assert archive_files["technical-archive.tar.gz.sha256"] == (
        f"{hashlib.sha256(tar_content).hexdigest()}  technical-archive.tar.gz\n".encode()
    )
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(tar_content), mode="r:gz") as archive:
        members = {
            member.name: archive.extractfile(member).read() for member in archive.getmembers()
        }
    assert members[f"restricted/samples/{sample_sha}.bin"] == sample
    assert members[f"restricted/network/captures/{capture_sha}.pcap"] == capture
    archive_manifest = _read_manifest(archive_output)
    yara_versions = [
        item
        for item in archive_manifest["objects"]
        if item["type"] == "yara_rule" and item["canonical_id"] in {"candidate-v1", "candidate-v2"}
    ]
    assert {item["metadata"]["candidate_status"] for item in yara_versions} == {
        "experimental",
        "tested",
    }
    assert archive_manifest["opinion_preview"] == {
        "status": "present",
        "path": "opinion/opinion-draft.pdf",
        "sha256": hashlib.sha256(preview_pdf).hexdigest(),
        "opinion_id": opinion_id,
        "opinion_version": 3,
        "opinion_sha256": "a" * 64,
        "base_publication_sha256": "b" * 64,
        "lineage_sha256": "c" * 64,
    }
    _assert_manifest_files(archive_output, archive_manifest)


def test_m14_research_profile_fails_when_present_private_bytes_are_not_written(
    tmp_path: Path,
) -> None:
    investigation_id = str(uuid4())
    digest = hashlib.sha256(b"canonical sample bytes").hexdigest()
    snapshot = InvestigationAnnexSnapshot(
        investigation_id,
        (
            AnnexAsset(
                canonical_id="missing-canonical-sample",
                type="sample",
                investigation_id=investigation_id,
                source="samples+blobs",
                origin="canonical_sample_row",
                sha256=digest,
                content=None,
            ),
        ),
    )

    with pytest.raises(ValueError, match="Expected canonical sample bytes were not written"):
        write_investigation_annex(
            snapshot,
            tmp_path / "incomplete-private-export",
            profile="m14_research_permissive",
            include_restricted_archive=True,
        )
