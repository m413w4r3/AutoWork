"""API contract for M24 investigation coordination creation (ADR-0011 D4/E3).

Only the start/mode surface belongs to lot 04A1; the plan, task and timeline
routes arrive with their own lots.
"""

from __future__ import annotations

from cti_app.domain.production import ProseAuthority
from tests.test_investigation_api import (
    INVESTIGATION_ID,
    SUBJECT_ID,
    _client,
    _start_body,
)


def test_bridge_only_bridge_led_creation_refused() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    refused = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "bridge_led"},
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "bridge_only_requires_operator_led"
    assert INVESTIGATION_ID not in store.states.rows


def test_bridge_only_operator_led_creation_succeeds() -> None:
    client, store = _client()
    store.investigations[INVESTIGATION_ID].prose_authority = ProseAuthority.BRIDGE_ONLY

    accepted = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "operator_led"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["investigation"]["execution_mode"] == "operator_led"


def test_legacy_investigation_keeps_bridge_led_behaviour() -> None:
    client, _store = _client()

    accepted = client.post(
        f"/api/subjects/{SUBJECT_ID}/investigations",
        json={**_start_body(), "execution_mode": "bridge_led"},
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["investigation"]["execution_mode"] == "bridge_led"
