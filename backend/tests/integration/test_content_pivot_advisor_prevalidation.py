from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.application.content_pivot_candidates import (
    generate_content_pivot_candidates,
    verify_content_pattern_candidates,
)
from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation

_INVESTIGATION_ID = UUID("5f66ebfb-1653-45e6-83cd-a29af6f87868")
_B1_SAMPLE = "76273382e4252c1f60a2251141e108942494409c759358320735891762c0682e"
_R1_SAMPLE = "53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402"
_B1 = bytes.fromhex(
    "edc7c7cbcfc3f0cc9496c2939192c1888b8c8e89938bd98dd79ed1d584d2998"
    "1b3b2b0b1efece8bee3b6b5afe2ebe8ab"
)
_B2 = bytes.fromhex(
    "97b4bcbcadbaaeabb7a1f0a2b3a5a2b0580b5b4c5c59495f7d4b4f5252070908"
    "52544b4d1e4c594f4456426e46584641"
)
_R1 = bytes.fromhex(
    "eccadcc8c28fc9d5c1c6d0d5cfc8caf6f5fdeceeffedf9e1ffdad3c3d9c4dbd3"
    "fed7dfe0e0ebe3faf1dfc3f4f4f5e1eb"
)
_R2 = bytes.fromhex(
    "19444a0a010118080c17425b46454a4e45595a0a161e5e4752515e5a49555659683820286c756e6f3f0e29223428372a"
)


def _legacy_xxd_observation(
    *,
    sample_sha256: str,
    observation_id: str,
    offset: int,
    value: bytes,
    output_limit: int | None = None,
) -> tuple[EvidenceObservation, dict[str, object]]:
    lines = []
    for index in range(0, len(value), 16):
        chunk = value[index : index + 16]
        hex_column = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_column = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset + index:08x}: {hex_column:<47}  {ascii_column}")
    output = "\n".join(lines)
    if output_limit is not None:
        output = output[:output_limit]
    payload: dict[str, object] = {
        "schema": "local-command-execution.v1",
        "sample_sha256": sample_sha256,
        "command_id": "xxd",
        "command": ["xxd", "-g", "1", "-s", str(offset), "-l", str(len(value)), "sample.bin"],
        "output": output,
        "output_truncated": False,
        "raw_output_bytes": 0,
        "raw_output_sha256": None,
        "raw_output_complete": False,
    }
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    observation = EvidenceObservation(
        investigation_id=_INVESTIGATION_ID,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=hashlib.sha256(raw_payload).hexdigest(),
        source="AutoWork local analysis",
        source_version="xxd-live-shape-v1",
        observed_at=datetime(2026, 10, 20, tzinfo=UTC),
        object_type="file",
        object_id=sample_sha256,
        id=UUID(observation_id),
    )
    return observation, payload


@pytest.mark.integration
def test_live_shape_b1_r1_patterns_are_generated_and_pass_advisor_prevalidation() -> None:
    observations_and_payloads = (
        _legacy_xxd_observation(
            sample_sha256=_B1_SAMPLE,
            observation_id="30b9153a-e9f6-5c17-b186-e49b134b4d67",
            offset=8_472_852,
            value=(
                _B1
                + bytes((index * 37 + 13) % 256 for index in range(64))
                + _B2
                + bytes((index * 29 + 7) % 256 for index in range(964 - 160))
            ),
            output_limit=4_000,
        ),
        _legacy_xxd_observation(
            sample_sha256=_R1_SAMPLE,
            observation_id="13096aec-2cc7-5a97-9eaa-5214db7ac1f9",
            offset=14_784,
            value=_R1,
        ),
        _legacy_xxd_observation(
            sample_sha256=_R1_SAMPLE,
            observation_id="8a353f4d-7d5d-5f34-a35f-fea003601dbe",
            offset=15_232,
            value=_R2,
        ),
    )
    observations = tuple(item[0] for item in observations_and_payloads)
    payloads = {str(observation.id): payload for observation, payload in observations_and_payloads}

    candidates = generate_content_pivot_candidates(observations, payloads)
    by_value = {item.hex_value: item for item in candidates}
    b1 = by_value[_B1.hex()]
    b2 = by_value[_B2.hex()]
    r1 = by_value[_R1.hex()]
    r2 = by_value[_R2.hex()]
    assert b1.measurements[0].offset == 8_472_852
    assert b2.measurements[0].offset == 8_472_964
    assert r1.measurements[0].offset == 14_784
    assert r2.measurements[0].offset == 15_232
    assert b1.id != hashlib.sha256(_B1).hexdigest()
    assert r1.id != hashlib.sha256(_R1).hexdigest()

    patterns = {}
    for candidate in (b1, b2, r1, r2):
        patterns[candidate.id] = [
            InvestigationAdvisorTool._advisor_measurement_row(
                measurement,
                blob_id=None,
                evidence_id=measurement.observation_id,
                evidence_sha256=measurement.observation_sha256,
                extractor_version=measurement.source_version,
            )
            for measurement in candidate.measurements
        ]
    accepted = InvestigationAdvisorTool._select_verified_measurements(
        [],
        [
            {"content_pattern_id": b1.id},
            {"content_pattern_id": b2.id},
            {"content_pattern_id": r1.id},
            {"content_pattern_id": r2.id},
        ],
        content_patterns=patterns,
    )
    assert [(item["value"], item["offset"]) for item in accepted] == [
        (_B1.hex(), 8_472_852),
        (_B2.hex(), 8_472_964),
        (_R1.hex(), 14_784),
        (_R2.hex(), 15_232),
    ]

    reversed_candidates = verify_content_pattern_candidates(tuple(reversed(observations)), payloads)
    assert {item.hex_value: item.id for item in reversed_candidates}[_B1.hex()] == b1.id
    assert {item.hex_value: item.id for item in reversed_candidates}[_B2.hex()] == b2.id
    assert {item.hex_value: item.id for item in reversed_candidates}[_R1.hex()] == r1.id
    assert {item.hex_value: item.id for item in reversed_candidates}[_R2.hex()] == r2.id
