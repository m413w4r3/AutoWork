"""Bounded canonical observation excerpt route oracles."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation_observation_excerpts import router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.opinion_note_proposals import MAX_CANONICAL_CODE_SOURCE_BYTES
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import CanonicalCodeNoteBlockV1
from cti_app.domain.opinion_note_proposal import NoteTargetBlockType
from tests.test_opinion_note_proposals import (
    LEAD_ANCHOR,
    _Fixture,
    _sha256,
)
from tests.test_opinion_note_proposals import (
    _append_observation as _append_observation_sync,
)


async def _append_observation(fixture: _Fixture, raw: bytes, **kwargs: Any) -> Any:
    """The shared fixture helper drives its own loop; run it off this one."""
    return await asyncio.to_thread(_append_observation_sync, fixture, raw, **kwargs)


def _app(fixture: _Fixture) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.uow_factory = fixture.uow_factory
    app.state.production_artifact_store = fixture.harness.store
    app.state.identity_provider = LocalIdentityProvider("analyst:excerpt-test")
    app.state.settings = Settings(_env_file=None)
    return app


def _path(fixture: _Fixture, observation_id: UUID) -> str:
    return f"/api/investigations/{fixture.investigation.id}/observations/{observation_id}/excerpt"


@pytest.mark.asyncio
async def test_excerpt_text_and_hex_share_canonical_code_verification() -> None:
    fixture = _Fixture()
    raw = b"prefix call\ntail"
    observation = await _append_observation(fixture, raw)
    path = _path(fixture, observation.id)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(fixture)), base_url="http://test"
    ) as client:
        text = await client.get(path, params={"offset": 7, "length": 5, "view": "text"})
        hex_view = await client.get(path, params={"offset": 7, "length": 4, "view": "hex"})

    assert text.status_code == 200
    text_payload = text.json()
    excerpt = raw[7:12]
    assert text_payload == {
        "view": "text",
        "offset": 7,
        "length": 5,
        "total_size": len(raw),
        "blob_sha256": observation.sha256,
        "excerpt_sha256": _sha256(excerpt),
        "untrusted_data": True,
        "text": "call\n",
    }
    assert {"blob_id", "blob_key", "storage_key", "url"}.isdisjoint(text_payload)

    assert hex_view.status_code == 200
    hex_payload = hex_view.json()
    assert hex_payload["hex"] == "00000007  63 61 6c 6c"
    assert hex_payload["excerpt_sha256"] == _sha256(raw[7:11])
    assert {"blob_id", "blob_key", "storage_key", "url"}.isdisjoint(hex_payload)

    # Proposal request runs the existing resolve_note_content_blocks verifier
    # over the exact offset and digest returned by the read route.
    block = CanonicalCodeNoteBlockV1(
        observation_id=observation.id,
        blob_sha256=text_payload["blob_sha256"],
        offset=text_payload["offset"],
        length=text_payload["length"],
        excerpt_sha256=text_payload["excerpt_sha256"],
        language="text",
    )
    result = await fixture.request(
        fixture.advisor(),
        anchor=LEAD_ANCHOR,
        target_block_type=NoteTargetBlockType.LEAD,
        content_blocks=(block,),
    )
    assert result.proposal.content_blocks == (block,)


@pytest.mark.asyncio
async def test_excerpt_rejects_unsafe_text_and_binary_remains_available_as_hex() -> None:
    fixture = _Fixture()
    observation = await _append_observation(fixture, b"\x00\xffA")
    path = _path(fixture, observation.id)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(fixture)), base_url="http://test"
    ) as client:
        text = await client.get(path, params={"offset": 0, "length": 3, "view": "text"})
        hex_view = await client.get(path, params={"offset": 0, "length": 3, "view": "hex"})

    assert text.status_code == 422
    assert text.json()["detail"]["code"] == "excerpt_not_safe_text"
    assert hex_view.status_code == 200
    assert hex_view.json()["hex"] == "00000000  00 ff 41"
    assert hex_view.json()["excerpt_sha256"] == hashlib.sha256(b"\x00\xffA").hexdigest()


@pytest.mark.asyncio
async def test_excerpt_rejects_oversized_range_and_out_of_bounds_offset() -> None:
    fixture = _Fixture()
    observation = await _append_observation(fixture, b"small")
    path = _path(fixture, observation.id)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(fixture)), base_url="http://test"
    ) as client:
        oversized = await client.get(path, params={"offset": 0, "length": 4097, "view": "hex"})
        out_of_bounds = await client.get(path, params={"offset": 4, "length": 2, "view": "hex"})

    assert oversized.status_code == 422
    assert out_of_bounds.status_code == 422
    assert out_of_bounds.json()["detail"]["code"] == "excerpt_range_out_of_bounds"


@pytest.mark.asyncio
async def test_excerpt_hides_cross_investigation_observations_and_unavailable_blobs() -> None:
    fixture = _Fixture()
    foreign = await _append_observation(
        fixture,
        b"foreign",
        investigation_id=uuid4(),
    )
    missing = await _append_observation(fixture, b"missing")
    del fixture.harness.store.blobs[missing.blob_id]
    path_foreign = _path(fixture, foreign.id)
    path_missing = _path(fixture, missing.id)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(fixture)), base_url="http://test"
    ) as client:
        foreign_response = await client.get(
            path_foreign, params={"offset": 0, "length": 2, "view": "hex"}
        )
        missing_response = await client.get(
            path_missing, params={"offset": 0, "length": 2, "view": "hex"}
        )

    assert foreign_response.status_code == 404
    assert foreign_response.json()["detail"]["code"] == "observation_not_found"
    assert missing_response.status_code == 409
    assert missing_response.json()["detail"]["code"] == "note_code_ref_stale"


@pytest.mark.asyncio
async def test_excerpt_rejects_observation_blob_over_shared_source_limit() -> None:
    fixture = _Fixture()
    observation = await _append_observation(fixture, b"x" * (MAX_CANONICAL_CODE_SOURCE_BYTES + 1))
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(fixture)), base_url="http://test"
    ) as client:
        response = await client.get(
            _path(fixture, observation.id), params={"offset": 0, "length": 1}
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "note_code_ref_stale"
