from __future__ import annotations

from cti_app.domain.investigation import SampleAssociation, SampleOrigin
from tests.test_investigation_api import (
    INVESTIGATION_ID,
    SUBJECT_ID,
    _client,
    _start_body,
)


def test_g2_rejects_unavailable_provider_and_g3_writes_human_decision() -> None:
    client, store = _client()
    started = client.post(f"/api/subjects/{SUBJECT_ID}/investigations", json=_start_body())
    assert started.status_code == 200

    unavailable_provider = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/envelope/approve",
        json={
            "allowed_providers": ["shodan"],
            "budget_limits": {"bridge_turns": 12},
            "acquire_hits_allowed": False,
            "max_acquire_volume": 0,
            "reason": "Try to enable an unconfigured provider.",
            "correlation_id": "g2-provider-unavailable",
        },
        headers={"Idempotency-Key": "g2-provider-unavailable"},
    )
    assert unavailable_provider.status_code == 422
    assert unavailable_provider.json()["detail"]["code"] == "provider_unavailable"
    assert INVESTIGATION_ID not in store.envelopes.rows

    association = SampleAssociation(
        investigation_id=INVESTIGATION_ID,
        sample_sha256="b" * 64,
        origin=SampleOrigin.VT_PIVOT,
    )
    store.associations.append(association)
    qualified = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/samples/{association.id}/qualify",
        json={
            "expected_version": association.version,
            "qualification": "rejected",
            "reason": "The analyst reviewed the sample and rejected membership.",
            "evidence_refs": [],
            "correlation_id": "g3-human-reject",
        },
        headers={"Idempotency-Key": "g3-human-reject"},
    )
    assert qualified.status_code == 200, qualified.text
    assert qualified.json()["association"]["qualification"] == "rejected"
    change = qualified.json()["association"]["changes"][0]
    assert change["actor_id"] == "analyst:test"
    assert change["actor_kind"] == "human"
    assert [decision.decision_type.value for decision in store.decisions] == ["member_reject"]
