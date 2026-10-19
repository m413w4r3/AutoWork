from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleState
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.investigation import (
    EvidenceObservation,
    SampleAssociation,
)
from cti_app.domain.investigation import (
    SampleOrigin as AssociationSampleOrigin,
)
from cti_app.domain.network_protocol import NetworkCaptureFormat
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


def _minimal_pcap() -> bytes:
    frame = b"\x00" * 14
    global_header = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
    packet_header = struct.pack("<IIII", 1, 0, len(frame), len(frame))
    return global_header + packet_header + frame


@dataclass
class _State:
    investigation_id: UUID
    subject_id: UUID
    blobs: dict[UUID, BlobRecord] = field(default_factory=dict)
    samples: list[Sample] = field(default_factory=list)
    associations: list[SampleAssociation] = field(default_factory=list)
    provenance: list[ProvenanceEvent] = field(default_factory=list)
    evidence: list[EvidenceObservation] = field(default_factory=list)
    commits: int = 0


class _Blobs:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def add(self, blob: BlobRecord) -> None:
        self.state.blobs[blob.id] = blob

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.state.blobs.get(blob_id)

    async def get_by_address(self, bucket: str, sha256: str) -> BlobRecord | None:
        return next(
            (
                item
                for item in self.state.blobs.values()
                if item.descriptor.logical_bucket == bucket and item.descriptor.sha256 == sha256
            ),
            None,
        )


class _Samples:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def get_by_subject_and_blob(self, subject_id: UUID, blob_id: UUID) -> Sample | None:
        return next(
            (
                item
                for item in self.state.samples
                if item.subject_id == subject_id and item.blob_id == blob_id
            ),
            None,
        )

    async def add(self, sample: Sample) -> None:
        self.state.samples.append(sample)


class _SampleAssociations:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            item for item in self.state.associations if item.investigation_id == investigation_id
        )

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation:
        assert expected_version == 0
        self.state.associations.append(association)
        return association


class _Provenance:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: UUID
    ) -> tuple[ProvenanceEvent, ...]:
        return tuple(
            item
            for item in self.state.provenance
            if item.aggregate_type == aggregate_type and item.aggregate_id == aggregate_id
        )

    async def append(self, event: ProvenanceEvent) -> None:
        self.state.provenance.append(event)


class _Evidence:
    def __init__(self, state: _State) -> None:
        self.state = state

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return next((item for item in self.state.evidence if item.id == observation_id), None)

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        existing = await self.get(observation.id)
        if existing is not None:
            return existing
        self.state.evidence.append(observation)
        return observation

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item for item in self.state.evidence if item.investigation_id == investigation_id
        )


class _UnitOfWork:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.blobs = _Blobs(state)
        self.samples = _Samples(state)
        self.sample_associations = _SampleAssociations(state)
        self.provenance = _Provenance(state)
        self.evidence_observations = _Evidence(state)
        self.analyst_investigations = SimpleNamespace(get=self._investigation)
        self.subjects = SimpleNamespace(get=self._subject)

    async def _investigation(self, investigation_id: UUID) -> Any:
        if investigation_id != self._state.investigation_id:
            return None
        return SimpleNamespace(subject_id=self._state.subject_id)

    async def _subject(self, subject_id: UUID) -> Any:
        if subject_id != self._state.subject_id:
            return None
        return SimpleNamespace(id=subject_id, tlp=TLP.GREEN)

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def commit(self) -> None:
        self._state.commits += 1


@pytest.mark.asyncio
async def test_vt_sandbox_pcap_is_quarantined_associated_and_idempotent(tmp_path: Path) -> None:
    investigation_id = uuid4()
    subject_id = uuid4()
    state = _State(investigation_id=investigation_id, subject_id=subject_id)
    content = _minimal_pcap()
    sha256 = hashlib.sha256(content).hexdigest()
    service = VirusTotalObservationService(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), lambda: _UnitOfWork(state)),
        lambda: _UnitOfWork(state),
    )
    observed_at = datetime.now(UTC)
    arguments = {
        "investigation_id": investigation_id,
        "file_hash": "a" * 64,
        "sandbox_id": "sandbox-report-123",
        "expected_sha256": sha256,
        "max_bytes": 1024 * 1024,
        "observed_at": observed_at,
        "idempotency_key": "pcap-operation-1",
    }

    first = await service.store_behaviour_pcap(content, **arguments)
    second = await service.store_behaviour_pcap(content, **arguments)

    assert first == second
    assert first.capture_ref.format is NetworkCaptureFormat.PCAP
    assert first.capture_ref.sha256 == sha256
    assert first.capture_ref.size_bytes == len(content)
    assert state.blobs[first.capture_ref.blob_id].descriptor.logical_bucket == (
        "network-captures-quarantine"
    )
    assert len(state.samples) == 1
    sample = state.samples[0]
    assert sample.state is SampleState.QUARANTINED
    assert sample.origin == "vt_sandbox_pcap"
    assert sample.origin_kind is EntitySampleOrigin.VT_HUNT_HIT
    assert sample.do_not_submit is True
    assert sample.external_llm_allowed is False
    assert len(state.associations) == 1
    assert state.associations[0].origin is AssociationSampleOrigin.BEHAVIOR
    assert len(state.provenance) == 1
    assert state.provenance[0].payload["provenance"] == "vt_sandbox_pcap"
    assert state.provenance[0].event_type == "sample_imported_from_vt_sandbox_pcap"
    assert len(state.evidence) == 1
    assert any(
        "pas une observation d'un C2 actif" in item for item in state.evidence[0].limitations
    )
