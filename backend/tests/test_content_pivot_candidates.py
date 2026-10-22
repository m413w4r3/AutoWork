from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from cti_app.application.content_pivot_candidates import (
    candidate_frontier_limit,
    generate_content_pivot_candidates,
    verify_content_pattern_candidates,
)
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation


def _observation(
    *,
    source: str = "autowork.floss",
    object_type: str | None = None,
    object_id: str | None = None,
) -> EvidenceObservation:
    sample_sha256 = object_id or hashlib.sha256(uuid4().bytes).hexdigest()
    normalized_source = source.strip().casefold()
    is_dotnet = normalized_source.startswith("dotnet.")
    is_local = normalized_source == "autowork local analysis" or normalized_source.startswith(
        "local."
    )
    return EvidenceObservation(
        investigation_id=uuid4(),
        kind=(
            EvidenceKind.BINARY_ANALYSIS if is_dotnet or is_local else EvidenceKind.BINARY_STRING
        ),
        blob_id=uuid4(),
        sha256=hashlib.sha256((source + sample_sha256).encode()).hexdigest(),
        source=source,
        source_version="measured-test-v1",
        observed_at=datetime(2026, 10, 20, tzinfo=UTC),
        object_type=object_type
        or ("dotnet_analysis" if is_dotnet else "file" if is_local else "sample"),
        object_id=sample_sha256,
    )


def _floss_payload(sample_sha256: str, records: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema": "sample-obfuscated-strings.v1",
        "sample_sha256": sample_sha256,
        "status": "SUCCEEDED",
        "strings": records,
    }


def _string(value: str, *, offset: int = 0, **extra: object) -> dict[str, object]:
    return {
        "value": value,
        "kind": "static",
        "encoding": "ascii",
        "offset": offset,
        "truncated_fields": [],
        **extra,
    }


def _measured_base64_prefix(length: int) -> str:
    start = "SSSeAZSwuSm2gjJK0xcg1rU5KfFKlZHrtKvRyGr7eb9xANupo2+s9F1KRipCMmg1migbxl+s3aJ8hCC5"
    continuation = base64.b64encode(bytes(range(256)) * 2).decode("ascii")
    return (start + continuation)[:length]


def _local_xxd_payload(sample_sha256: str, content: bytes, *, offset: int) -> dict[str, object]:
    lines = []
    for index in range(0, len(content), 16):
        chunk = content[index : index + 16]
        hex_column = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_column = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset + index:08x}: {hex_column:<47}  {ascii_column}")
    output_bytes = "\n".join(lines).encode("utf-8")
    encoded = base64.b64encode(output_bytes).decode("ascii")
    return {
        "schema": "local-command-execution.v1",
        "command_id": "xxd",
        "command": ["xxd", "-g", "1", "-s", str(offset), "-l", str(len(content))],
        "sample_sha256": sample_sha256,
        "output_encoding": "base64-utf8",
        "output_byte_length": len(output_bytes),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "output_chunks": [
            encoded[index : index + 4_000] for index in range(0, len(encoded), 4_000)
        ],
        "output_truncated": False,
        "untrusted_data": True,
    }


def test_generates_traceable_query_from_typed_floss_observations() -> None:
    value = _measured_base64_prefix(447)
    observations = (_observation(), _observation())
    payloads = {
        str(observation.id): _floss_payload(
            observation.object_id,
            [
                _string(
                    value,
                    offset=0,
                    goodware_occurrences=0,
                    evidence_path="sample.conf",
                )
            ],
        )
        for observation in observations
    }

    (candidate,) = generate_content_pivot_candidates(observations, payloads)

    assert candidate.query == f"content:{{{value.encode('ascii').hex()}}}"
    assert candidate.byte_length == len(value)
    assert candidate.evidence_refs == tuple(sorted(str(item.id) for item in observations))
    assert "2 échantillon(s)" in candidate.justification
    assert len(candidate.measurements) == 2
    assert {item.offset for item in candidate.measurements} == {0}
    assert {item.length for item in candidate.measurements} == {len(value)}
    assert {item.encoding for item in candidate.measurements} == {"ascii"}
    assert {item.bridge_kind for item in candidate.measurements} == {"string"}
    assert {item.source_file for item in candidate.measurements} == {"sample.conf"}


def test_dotnet_candidates_require_measured_file_offset_and_source_file() -> None:
    sample_sha256 = hashlib.sha256(uuid4().bytes).hexdigest()
    observation = _observation(
        source="dotnet.ilspycmd",
        object_type="dotnet_analysis",
        object_id=f"{sample_sha256}:ilspycmd:request",
    )
    payload = {
        "schema": "dotnet-analysis-observation.v1",
        "sample_sha256": sample_sha256,
        "status": "complet",
        "result": {
            "members": [
                {
                    "value": "Blackwood.DynamicDllLoader",
                    "encoding": "ascii",
                    "offset": 0x814914,
                    "offset_kind": "file",
                    "source_file": "Blackwood.Cheese.dll",
                },
                {
                    "value": "Blackwood.ConfigReader",
                    "encoding": "ascii",
                    "offset": 0x814A14,
                    "offset_kind": "file",
                },
            ]
        },
    }

    (candidate,) = generate_content_pivot_candidates((observation,), {str(observation.id): payload})

    measurement = candidate.measurements[0]
    assert measurement.offset == 0x814914
    assert measurement.source_file == "Blackwood.Cheese.dll"
    assert measurement.bridge_kind == "string"


def test_generic_goodware_secret_url_and_host_path_measurements_are_excluded() -> None:
    observation = _observation()
    values = (
        _string("System.Collections.Generic.List`1", offset=0),
        _string("Authorization=Bearer abcdefghijklmnop", offset=100),
        _string("https://example.test/path?X-Amz-Signature=abcdef", offset=200),
        _string("/home/nill/private/host-secret-path", offset=300),
        _string("AAAAAAAAAAAAAAAAAAAA", offset=350),
        _string("Blackwood.Cheese.DynamicLoader", offset=400, goodware_occurrences=2),
        _string("Short", offset=500),
        {
            "value": "NoEncodingGuessPlease",
            "kind": "static",
            "offset": 600,
        },
    )
    payload = _floss_payload(observation.object_id, list(values))

    candidates = generate_content_pivot_candidates((observation,), {str(observation.id): payload})

    assert candidates == ()


def test_identical_patterns_are_deduplicated_across_observations() -> None:
    value = "Blackwood.DynamicDllLoader"
    observations = (_observation(), _observation())
    payloads = {
        str(observation.id): _floss_payload(
            observation.object_id,
            [_string(value, offset=0x100, goodware_occurrences=0)],
        )
        for observation in observations
    }

    candidates = generate_content_pivot_candidates(observations, payloads)

    assert len(candidates) == 1
    assert len(candidates[0].evidence_refs) == 2
    assert len({item.sample_sha256 for item in candidates[0].measurements}) == 2


def test_bounded_xxd_observation_preserves_raw_offset_and_hex_measurement() -> None:
    observation = _observation(source="local.run_command", object_type="command")
    measured_bytes = bytes.fromhex("ed c7 c7 cb cf c3 f0 cc 94 96 c2 93 91 92 c1 88")
    payload = _local_xxd_payload(observation.object_id, measured_bytes, offset=8472852)

    (candidate,) = generate_content_pivot_candidates((observation,), {str(observation.id): payload})

    measurement = candidate.measurements[0]
    expected_hex = measured_bytes.hex()
    assert candidate.hex_value == expected_hex
    assert measurement.offset == 8472852
    assert measurement.length == 16
    assert measurement.encoding == "raw"
    assert measurement.bridge_kind == "hex"
    assert measurement.bridge_value == candidate.hex_value


def test_xxd_observations_share_exact_bounded_prefix_segments() -> None:
    prefix = _measured_base64_prefix(447).encode("ascii")
    sample_bytes = (prefix + b"A" * 73, prefix + b"B" * 77)
    observations = (
        _observation(source="AutoWork local analysis"),
        _observation(source="AutoWork local analysis"),
    )
    payloads: dict[str, dict[str, object]] = {}
    for observation, content in zip(observations, sample_bytes, strict=True):
        payloads[str(observation.id)] = _local_xxd_payload(
            observation.object_id,
            content,
            offset=0,
        )

    candidates = generate_content_pivot_candidates(observations, payloads)

    (shared_tail,) = [
        candidate for candidate in candidates if candidate.hex_value == prefix[256:].hex()
    ]
    assert shared_tail.byte_length == 191
    assert {item.offset for item in shared_tail.measurements} == {256}
    assert {item.sample_sha256 for item in shared_tail.measurements} == {
        observation.object_id for observation in observations
    }


def test_xxd_candidate_requires_intact_persisted_output_chunks() -> None:
    observation = _observation(source="AutoWork local analysis")
    payload = _local_xxd_payload(
        observation.object_id,
        b"Blackwood.Unique.Marker",
        offset=128,
    )
    payload["output_sha256"] = "0" * 64

    candidates = generate_content_pivot_candidates((observation,), {str(observation.id): payload})

    assert candidates == ()


def test_xxd_candidate_rejects_explicit_response_truncation() -> None:
    observation = _observation(source="AutoWork local analysis")
    payload = _local_xxd_payload(
        observation.object_id,
        b"Blackwood.Unique.Marker",
        offset=128,
    )
    payload["output_truncated_by_response"] = True

    candidates = generate_content_pivot_candidates((observation,), {str(observation.id): payload})

    assert candidates == ()


def test_shared_verifier_keeps_xxd_candidate_ids_and_absolute_offsets_stable() -> None:
    observation = _observation(source="AutoWork local analysis")
    measured = bytes.fromhex("ed c7 c7 cb cf c3 f0 cc 94 96 c2 93 91 92 c1 88")
    payload = _local_xxd_payload(observation.object_id, measured, offset=8472852)
    payloads = {str(observation.id): payload}

    pivot_candidates = verify_content_pattern_candidates((observation,), payloads)
    assert pivot_candidates
    selected = verify_content_pattern_candidates(
        (observation,), payloads, candidate_ids=[pivot_candidates[0].id]
    )

    assert selected == (pivot_candidates[0],)
    identity = {
        "schema": "content-pattern-candidate.v2",
        "octets_hex": pivot_candidates[0].hex_value,
        "locations": [[observation.object_id, 8472852]],
    }
    expected_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    assert pivot_candidates[0].id == expected_id
    assert pivot_candidates[0].id != hashlib.sha256(measured).hexdigest()
    assert pivot_candidates[0].measurements[0].offset == 8472852
    assert pivot_candidates[0].measurements[0].encoding == "raw"


def test_content_candidate_frontier_page_is_bounded_at_128() -> None:
    assert candidate_frontier_limit(500, has_content_candidates=True) == 128
    assert candidate_frontier_limit(500, has_content_candidates=False) == 500


def test_shared_verifier_rejects_a_tampered_multi_chunk_xxd_dump() -> None:
    observation = _observation(source="AutoWork local analysis")
    alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    measured = (alphabet * 132)[:8_192]
    payload = _local_xxd_payload(observation.object_id, measured, offset=0x4000)
    assert len(payload["output_chunks"]) > 1
    candidate_ids = [
        item.id
        for item in verify_content_pattern_candidates(
            (observation,), {str(observation.id): payload}
        )
    ]
    assert candidate_ids

    output_chunks = payload["output_chunks"]
    assert isinstance(output_chunks, list)
    chunks = list(output_chunks)
    chunks[0] = ("A" if chunks[0][0] != "A" else "B") + chunks[0][1:]
    payload["output_chunks"] = chunks

    assert (
        verify_content_pattern_candidates(
            (observation,), {str(observation.id): payload}, candidate_ids=candidate_ids
        )
        == ()
    )


def test_candidate_limit_is_bounded_and_order_is_stable() -> None:
    observations = tuple(_observation() for _ in range(10))
    payloads = {
        str(observation.id): _floss_payload(
            observation.object_id,
            [_string(f"BlackwoodMarker{index:02d}Distinctive", goodware_occurrences=0)],
        )
        for index, observation in enumerate(observations)
    }

    candidates = generate_content_pivot_candidates(
        observations,
        payloads,
        maximum_candidates=5,
    )

    assert len(candidates) == 5
    all_candidates = generate_content_pivot_candidates(observations, payloads)
    assert len(all_candidates) == 10
    assert all_candidates[0].rank_key <= all_candidates[-1].rank_key
