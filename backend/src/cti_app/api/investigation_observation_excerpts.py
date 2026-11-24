"""Bounded reads of exact byte ranges from canonical investigation observations."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from cti_app.api.investigation import (
    _artifact_store,
    _http_error,
    _require_verified_human_operator,
    _uow_factory,
)
from cti_app.application.opinion_note_proposals import (
    MAX_CANONICAL_CODE_SOURCE_BYTES,
    CanonicalCodeSourceUnavailableError,
    canonical_code_excerpt_bytes,
    resolve_canonical_code_observation_blob,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.analyst_opinion import MAX_CANONICAL_CODE_EXCERPT_BYTES

router = APIRouter(prefix="/api/investigations", tags=["investigation-observation-excerpts"])


class ObservationExcerptMetadataV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int
    length: int
    total_size: int
    blob_sha256: str
    excerpt_sha256: str
    untrusted_data: Literal[True] = True


class ObservationExcerptTextV1(ObservationExcerptMetadataV1):
    view: Literal["text"]
    text: str


class ObservationExcerptHexV1(ObservationExcerptMetadataV1):
    view: Literal["hex"]
    hex: str


ObservationExcerptResponseV1 = Annotated[
    ObservationExcerptTextV1 | ObservationExcerptHexV1,
    Field(discriminator="view"),
]


def _hex_dump(raw: bytes, *, offset: int) -> str:
    return "\n".join(
        f"{offset + index:08x}  " + " ".join(f"{byte:02x}" for byte in raw[index : index + 16])
        for index in range(0, len(raw), 16)
    )


def _safe_ascii_text(raw: bytes) -> str:
    """Require one printable ASCII character per byte to preserve byte alignment."""
    if any(byte > 0x7E or (byte < 0x20 and byte not in {0x09, 0x0A, 0x0D}) for byte in raw):
        raise _http_error(422, "excerpt_not_safe_text")
    return raw.decode("ascii")


@router.get(
    "/{investigation_id}/observations/{observation_id}/excerpt",
    response_model=ObservationExcerptResponseV1,
)
async def get_investigation_observation_excerpt(
    investigation_id: UUID,
    observation_id: UUID,
    request: Request,
    offset: int = Query(..., ge=0, le=MAX_CANONICAL_CODE_SOURCE_BYTES),
    length: int = Query(..., ge=1, le=MAX_CANONICAL_CODE_EXCERPT_BYTES),
    view: Literal["text", "hex"] = Query(default="text"),
) -> ObservationExcerptTextV1 | ObservationExcerptHexV1:
    """Read a bounded, inert excerpt from an observation admissible to canonical_code."""
    await _require_verified_human_operator(request)
    async with cast(UnitOfWorkFactory, _uow_factory(request))() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        observation = await uow.evidence_observations.get(observation_id)
        if observation is None or observation.investigation_id != investigation_id:
            raise _http_error(404, "observation_not_found")
        try:
            source = await resolve_canonical_code_observation_blob(
                artifact_store=_artifact_store(request),
                investigation_id=investigation_id,
                observation=observation,
            )
        except CanonicalCodeSourceUnavailableError as exc:
            raise _http_error(503, "observation_excerpt_unavailable") from exc
        if source is None:
            raise _http_error(409, "note_code_ref_stale")
        if offset > source.total_size or length > source.total_size - offset:
            raise _http_error(422, "excerpt_range_out_of_bounds")
        try:
            excerpt = canonical_code_excerpt_bytes(
                source.raw,
                offset=offset,
                length=length,
                enforce_read_limits=True,
            )
        except ValueError as exc:
            raise _http_error(422, "excerpt_range_out_of_bounds") from exc

        metadata = {
            "offset": offset,
            "length": len(excerpt),
            "total_size": source.total_size,
            "blob_sha256": source.blob_sha256,
            "excerpt_sha256": hashlib.sha256(excerpt).hexdigest(),
            "untrusted_data": True,
        }
        if view == "text":
            return ObservationExcerptTextV1(view="text", text=_safe_ascii_text(excerpt), **metadata)
        return ObservationExcerptHexV1(
            view="hex", hex=_hex_dump(excerpt, offset=offset), **metadata
        )


__all__ = ["router"]
