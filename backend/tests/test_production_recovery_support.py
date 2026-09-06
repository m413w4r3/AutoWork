"""Shared fixtures for historical Q2 recovery tests.

The archive fakes here answer reads from what was persisted and raise loudly
if anything tries to run a model, so "zero provider calls" is enforced by the
double rather than merely asserted afterwards.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any
from uuid import UUID

MODEL_RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
SOURCE_URL = "https://example.test/report/"
INDIVIDUAL_REFERENCE = "model://q2/individual"
BATCH_REFERENCE = "model://q2/batch"

# Not a reserved RFC 2606 name: the deterministic IOC validation drops those,
# so a documentation domain could never be projected back into an extraction.
DOMAIN = "redkitten-c2.security-lab.io"
IP = "203.0.113.9"
URL = "https://redkitten-c2.security-lab.io/panel/login"
EMAIL = "operator@redkitten-c2.security-lab.io"
FILE_HASH = "b" * 64
YARA_BODY = 'rule RedKitten { strings: $a = "RedKitten" condition: $a }'

Q2_INDIVIDUAL_OUTPUT = f"""FACT malware
- RedKitten dropper

IOC confirmed domain
- {DOMAIN}

IOC confirmed ip
- {IP}

IOC confirmed url
- {URL}

IOC confirmed email
- {EMAIL}

IOC confirmed sha256
- {FILE_HASH}

RULE yara: RedKitten
```yara
{YARA_BODY}
```
"""

S1_DOMAIN = "s1.security-lab.io"
S2_DOMAIN = "s2.security-lab.io"
S3_DOMAIN = "s3.security-lab.io"
Q2_BATCH_OUTPUT = f"""@@Q2:B1@@
IOC confirmed domain
- {S1_DOMAIN}

@@Q2:B2@@
IOC confirmed domain
- {S2_DOMAIN}

@@Q2:B3@@
IOC confirmed domain
- {S3_DOMAIN}
"""


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RecordingArchive:
    """A model archive that reads archived output and never calls a provider."""

    def __init__(self, runs: dict[UUID, Any], outputs: dict[str, bytes]) -> None:
        self._runs = runs
        self._outputs = outputs
        self.reads: list[str] = []
        self.run_lookups: list[UUID] = []

    async def get_run(self, run_id: UUID) -> Any | None:
        self.run_lookups.append(run_id)
        return self._runs.get(run_id)

    async def read_output(self, reference: str, *, max_bytes: int = 10_000_000) -> bytes:
        del max_bytes
        self.reads.append(reference)
        if reference not in self._outputs:
            raise FileNotFoundError(reference)
        return self._outputs[reference]

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("Historical recovery must never execute a model")

    async def research(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("Historical recovery must never call a provider")


def individual_archive(
    *, output: str = Q2_INDIVIDUAL_OUTPUT, reference: str = INDIVIDUAL_REFERENCE
) -> RecordingArchive:
    run = SimpleNamespace(
        id=MODEL_RUN_ID,
        raw_output_reference=reference,
        output_references=(reference,),
        parameters={"q2_execution_kind": "individual"},
    )
    return RecordingArchive({MODEL_RUN_ID: run}, {reference: output.encode("utf-8")})


def batch_archive() -> RecordingArchive:
    run = SimpleNamespace(
        id=MODEL_RUN_ID,
        raw_output_reference=BATCH_REFERENCE,
        output_references=(BATCH_REFERENCE,),
        parameters={
            "q2_execution_kind": "batch",
            "q2_batch_sources": [
                {"batch_id": "B1", "canonical_url": "https://s1.example.test/report"},
                {"batch_id": "B2", "canonical_url": SOURCE_URL},
                {"batch_id": "B3", "canonical_url": "https://s3.example.test/report"},
            ],
        },
    )
    return RecordingArchive({MODEL_RUN_ID: run}, {BATCH_REFERENCE: Q2_BATCH_OUTPUT.encode("utf-8")})


def legacy_entry(
    *,
    value: str | None,
    value_hash: str,
    artifact_type: str,
    proposal_kind: str = "artifact",
    proposal_index: int | None = None,
    batch_id: str | None = None,
    model_run_id: UUID | None = MODEL_RUN_ID,
) -> dict[str, Any]:
    """One pre-LOT18 ``q2_source_evidence_rejections`` diagnostic entry."""
    entry: dict[str, Any] = {
        "source_id": "S1",
        "source_url": SOURCE_URL,
        "batch_id": batch_id,
        "model_run_id": str(model_run_id) if model_run_id else None,
        "proposal_kind": proposal_kind,
        "artifact_type": artifact_type,
        "reason_code": "source_evidence_missing",
        "value_hash": value_hash,
    }
    if value is not None:
        entry["value"] = value
    if proposal_index is not None:
        entry["proposal_index"] = proposal_index
    return entry


def legacy_verification() -> dict[str, Any]:
    """The pre-LOT18 extraction diagnostics: hashes, but no exact values."""
    return {
        "q2_rejected_ioc_count": 1,
        "q2_source_evidence_rejections": [
            legacy_entry(value=None, value_hash=sha256(DOMAIN), artifact_type="domain"),
            legacy_entry(
                value=YARA_BODY[:32],
                value_hash=sha256(YARA_BODY),
                artifact_type="yara",
                proposal_kind="rule",
            ),
        ],
    }
