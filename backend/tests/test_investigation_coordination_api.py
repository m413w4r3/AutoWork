"""API contract for M24 investigation coordination creation (ADR-0011 D4/E3).

Only the start/mode surface belongs to lot 04A1; the plan, task and timeline
routes arrive with their own lots.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from cti_app.api import investigation as investigation_api
from cti_app.domain.classification import TLP
from cti_app.domain.production import AnalystInvestigationStatus, ProseAuthority
from tests.test_investigation_api import (
    INVESTIGATION_ID,
    RUN_ID,
    SUBJECT_ID,
    SYNTHESIS_ID,
    _client,
    _CompositionFactory,
    _SnapshotPort,
    _start_body,
    _Store,
)


def test_bridge_only_bridge_led_creation_refused() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={
            **_start_body(),
            "creation_profile": "m24_workshop",
            "execution_mode": "bridge_led",
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert INVESTIGATION_ID not in store.states.rows


def test_workshop_creation_refuses_bridge_led_without_creating_the_row() -> None:
    client, store = _client()
    store.investigations.clear()

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={
            **_start_body(),
            "creation_profile": "m24_workshop",
            "execution_mode": "bridge_led",
        },
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert store.investigations == {}
    assert not store.states.rows


def test_workshop_creation_never_infers_operator_led_from_the_profile() -> None:
    client, store = _client()
    store.investigations.clear()

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "creation_profile": "m24_workshop"},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert store.investigations == {}


def test_bridge_only_operator_led_creation_succeeds() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    accepted = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={
            **_start_body(),
            "creation_profile": "m24_workshop",
            "execution_mode": "operator_led",
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["investigation"]["execution_mode"] == "operator_led"


def test_legacy_request_cannot_reensure_an_existing_bridge_only_investigation() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "operator_led"},
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "prose_authority_conflict"
    assert INVESTIGATION_ID not in store.states.rows


def test_legacy_investigation_keeps_bridge_led_behaviour() -> None:
    client, _store = _client()

    accepted = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "bridge_led"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["investigation"]["execution_mode"] == "bridge_led"


def test_workshop_creation_requests_bridge_only_from_the_handoff(monkeypatch) -> None:
    store = _Store()
    store.investigations.clear()
    store.input_snapshots[RUN_ID] = SimpleNamespace(subject_tlp=TLP.AMBER, core_sources=())
    payload = {
        "subject_id": str(SUBJECT_ID),
        "title": "Verified synthesis for the workshop",
    }
    synthesis_sha256 = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    captured: dict[str, Any] = {}

    class _ArtifactStore:
        async def read_json(self, _blob_id: UUID) -> dict[str, Any]:
            return payload

        @staticmethod
        def canonical_json_bytes(value: dict[str, Any]) -> bytes:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

    class _Handoff:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def ensure_for_verified_synthesis(self, *, run, synthesis, policy, **kwargs):
            assert run.id == RUN_ID and synthesis.id == SYNTHESIS_ID
            assert policy.tlp == TLP.AMBER.value
            captured["prose_authority"] = kwargs.get("prose_authority")
            store.investigations[INVESTIGATION_ID] = SimpleNamespace(
                id=INVESTIGATION_ID,
                subject_id=SUBJECT_ID,
                production_run_id=RUN_ID,
                synthesis_artifact_id=SYNTHESIS_ID,
                budget=SimpleNamespace(max_cycles=4),
                status=AnalystInvestigationStatus.QUEUED,
                version=1,
                updated_at=datetime.now(UTC),
                prose_authority=kwargs.get("prose_authority"),
            )
            return SimpleNamespace(investigation_id=INVESTIGATION_ID)

    monkeypatch.setattr(investigation_api, "AnalystPostSynthesisService", _Handoff)
    snapshot_port = _SnapshotPort()
    snapshot_port.sha256 = synthesis_sha256
    factory = _CompositionFactory(snapshot_port)
    factory.artifact_store = _ArtifactStore()
    factory.settings = SimpleNamespace(
        investigation_max_cycles=4,
        investigation_max_pivot_runs=0,
        investigation_max_hits_acquired=0,
        investigation_max_new_samples=0,
        investigation_max_vt_read_units=0,
    )
    client, store = _client(store=store, factory=factory)
    body = {
        **_start_body(),
        "synthesis_sha256": synthesis_sha256,
        "creation_profile": "m24_workshop",
        "execution_mode": "operator_led",
    }

    response = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=body)

    assert response.status_code == 200, response.text
    assert captured["prose_authority"] is ProseAuthority.BRIDGE_ONLY
    assert response.json()["investigation"]["execution_mode"] == "operator_led"


def test_creation_profile_validates_unknown_values() -> None:
    client, store = _client()

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "creation_profile": "m24_anything"},
    )

    assert refused.status_code == 422, refused.text
    assert INVESTIGATION_ID not in store.states.rows
