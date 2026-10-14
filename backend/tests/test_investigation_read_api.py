from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation import router
from cti_app.domain.investigation import EvidenceKind, FindingAssessment
from cti_app.domain.investigation_state import Hypothesis, HypothesisStatus

INVESTIGATION_ID = UUID("10000000-0000-4000-8000-000000000001")
OTHER_INVESTIGATION_ID = UUID("10000000-0000-4000-8000-000000000002")
SUBJECT_ID = UUID("20000000-0000-4000-8000-000000000001")
OTHER_SUBJECT_ID = UUID("20000000-0000-4000-8000-000000000002")


class _BlobRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, SimpleNamespace] = {}

    async def get(self, blob_id: UUID):
        return self.rows.get(blob_id)

    async def get_by_address(self, logical_bucket: str, sha256: str):
        return next(
            (
                row
                for row in self.rows.values()
                if row.descriptor.logical_bucket == logical_bucket
                and row.descriptor.sha256 == sha256
            ),
            None,
        )


class _Observations:
    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)


class _States:
    def __init__(self) -> None:
        self.rows: dict[UUID, SimpleNamespace] = {}

    async def get(self, investigation_id: UUID):
        return self.rows.get(investigation_id)


class _Investigations:
    def __init__(self, rows: dict[UUID, SimpleNamespace]) -> None:
        self.rows = rows

    async def get(self, investigation_id: UUID):
        return self.rows.get(investigation_id)


class _Reservations:
    async def count_for_subject(self, subject_id: UUID) -> int:
        return 1 if subject_id == SUBJECT_ID else 0

    async def list_for_subject(self, subject_id: UUID):
        return (
            (
                SimpleNamespace(
                    id=uuid4(),
                    investigation_id=INVESTIGATION_ID,
                    slot_number=1,
                    state=SimpleNamespace(value="submitted"),
                    version=2,
                    updated_at=datetime.now(UTC),
                    provider_job_id="provider-job-hidden",
                ),
            )
            if subject_id == SUBJECT_ID
            else ()
        )


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.analyst_investigations = _Investigations(store.investigations)
        self.evidence_observations = store.observations
        self.investigation_states = store.states
        self.investigation_findings = store.findings
        self.blobs = store.blobs
        self.retrohunt_reservations = _Reservations()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Items:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)


class _Store:
    def __init__(self) -> None:
        self.investigations = {
            INVESTIGATION_ID: SimpleNamespace(id=INVESTIGATION_ID, subject_id=SUBJECT_ID),
            OTHER_INVESTIGATION_ID: SimpleNamespace(
                id=OTHER_INVESTIGATION_ID, subject_id=OTHER_SUBJECT_ID
            ),
        }
        self.observations = _Observations()
        self.states = _States()
        self.findings = _Items([])
        self.blobs = _BlobRepository()
        self.artifacts: dict[UUID, dict[str, object]] = {}
        self.artifact_reads: list[UUID] = []

    def uow(self) -> _Uow:
        return _Uow(self)


class _ArtifactStore:
    def __init__(self, store: _Store) -> None:
        self.store = store

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        self.store.artifact_reads.append(blob_id)
        return self.store.artifacts[blob_id]


def _blob(
    store: _Store,
    *,
    investigation_id: UUID,
    logical_bucket: str,
    sha256: str,
    payload: dict[str, object],
    object_type: str,
) -> UUID:
    blob_id = uuid4()
    store.blobs.rows[blob_id] = SimpleNamespace(
        id=blob_id,
        descriptor=SimpleNamespace(
            logical_bucket=logical_bucket,
            sha256=sha256,
            size=1,
        ),
    )
    store.artifacts[blob_id] = payload
    store.observations.rows.append(
        SimpleNamespace(
            id=uuid4(),
            investigation_id=investigation_id,
            object_type=object_type,
            object_id=sha256,
            sha256=sha256,
        )
    )
    return blob_id


def _client(store: _Store) -> _Client:
    app = FastAPI()
    app.include_router(router)
    app.state.uow_factory = store.uow
    app.state.production_artifact_store = _ArtifactStore(store)
    return _Client(app)


class _Client:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def get(self, path: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            return await client.get(path)


@pytest.mark.asyncio
async def test_sample_comparison_detail_is_bounded_and_owned_by_the_investigation() -> None:
    store = _Store()
    blob_id = _blob(
        store,
        investigation_id=INVESTIGATION_ID,
        logical_bucket="investigation-sample-similarity",
        sha256="a" * 64,
        object_type="family_feature_comparison",
        payload={
            "schema_version": "family-feature-comparison-v1",
            "investigation_id": str(INVESTIGATION_ID),
            "corpus_snapshot_sha256": "b" * 64,
            "comparison_sample_sha256s": ["c" * 64],
            "status": "insufficient_comparative_support",
            "features": [
                {
                    "feature_key": "d" * 64,
                    "kind": "string",
                    "value": "sample feature",
                    "feature_class": "optional",
                    "rank": 1,
                    "support": {
                        "positive": 1,
                        "positive_sample_count": 1,
                        "corpus": 1,
                        "corpus_sample_count": 1,
                        "goodware_occurrence_count": None,
                        "goodware_prevalence_status": "unmeasured",
                        "comparison_exclusive": True,
                    },
                    "sample_presence": [],
                    "rejection_reasons": [],
                    "rationale": [],
                    "signature_candidates": [],
                }
            ],
            "limits": {"selected_samples": 1, "corpus_samples": 1, "features": 1},
            "extractor_versions": [],
            "warnings": [],
            "recommended_next_tests": [],
        },
    )
    client = _client(store)

    listing = await client.get(f"/api/investigations/{INVESTIGATION_ID}/sample-comparisons")
    detail = await client.get(
        f"/api/investigations/{INVESTIGATION_ID}/sample-comparisons/{blob_id}?feature_limit=1"
    )

    assert listing.status_code == 200
    assert listing.json()["comparisons"][0]["status"] == "insufficient_comparative_support"
    assert listing.json()["comparisons"][0]["sha256"] == "a" * 64
    assert detail.status_code == 200
    assert detail.json()["features"][0]["support"]["goodware_prevalence_status"] == "unmeasured"

    foreign_blob = _blob(
        store,
        investigation_id=OTHER_INVESTIGATION_ID,
        logical_bucket="investigation-sample-similarity",
        sha256="e" * 64,
        object_type="family_feature_comparison",
        payload={"investigation_id": str(OTHER_INVESTIGATION_ID)},
    )
    reads_before = len(store.artifact_reads)
    foreign = await client.get(
        f"/api/investigations/{INVESTIGATION_ID}/sample-comparisons/{foreign_blob}"
    )
    assert foreign.status_code == 404
    assert len(store.artifact_reads) == reads_before


@pytest.mark.asyncio
async def test_hypothesis_api_keeps_linked_refutations_and_retrohunt_slots_subject_scoped() -> None:
    store = _Store()
    finding_id = uuid4()
    observation_id = uuid4()
    store.states.rows[INVESTIGATION_ID] = SimpleNamespace(
        hypotheses=(
            Hypothesis(
                id="hypothesis-c2",
                statement="Le client contacte le serveur observé.",
                status=HypothesisStatus.OPEN,
                evidence_refs=(observation_id,),
                finding_refs=(finding_id,),
            ),
        )
    )
    store.findings = _Items(
        [
            SimpleNamespace(
                id=finding_id,
                investigation_id=INVESTIGATION_ID,
                kind=SimpleNamespace(value="network_behaviour"),
                assessment=FindingAssessment.REFUTED,
                statement="La trace de contrôle ne montre pas cette connexion.",
                observation_refs=(observation_id,),
                limitations=("Une capture supplémentaire est nécessaire.",),
                supersedes=None,
            )
        ]
    )
    store.observations.rows.append(
        SimpleNamespace(
            id=observation_id,
            investigation_id=INVESTIGATION_ID,
            kind=EvidenceKind.NEGATIVE_RESULT,
            sha256="f" * 64,
            source="passive_pcap",
            source_version="1",
            observed_at=datetime.now(UTC),
            limitations=("Capture partielle.",),
        )
    )
    client = _client(store)

    hypotheses = await client.get(f"/api/investigations/{INVESTIGATION_ID}/hypotheses")
    retrohunt = await client.get(f"/api/investigations/{INVESTIGATION_ID}/retrohunt")

    assert hypotheses.status_code == 200
    item = hypotheses.json()["hypotheses"][0]
    assert item["status"] == "open"
    assert item["contradictions"][0]["assessment"] == "refuted"
    assert item["observations"][0]["kind"] == "negative_result"
    assert retrohunt.status_code == 200
    assert retrohunt.json()["consumed_slots"] == 1
    assert retrohunt.json()["remaining_slots"] == 1
    assert retrohunt.json()["jobs"][0]["status"] == "submitted"
    assert "provider_job_id" not in retrohunt.json()["jobs"][0]


@pytest.mark.asyncio
async def test_protocol_model_detail_never_reads_a_blob_from_another_investigation() -> None:
    store = _Store()
    blob_id = _blob(
        store,
        investigation_id=OTHER_INVESTIGATION_ID,
        logical_bucket="network-protocol-models",
        sha256="1" * 64,
        object_type="protocol_model",
        payload={"not": "parsed unless the investigation owns the blob"},
    )
    client = _client(store)

    response = await client.get(f"/api/investigations/{INVESTIGATION_ID}/protocol-models/{blob_id}")

    assert response.status_code == 404
    assert store.artifact_reads == []
