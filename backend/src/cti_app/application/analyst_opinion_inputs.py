"""Shared validated input models for analyst opinion edits."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cti_app.domain.analyst_opinion import ImplantMaterialEditorialV1, ImplantMaterialType


class ImplantMaterialEdit(BaseModel):
    """Validated operator/API shape for one cited implant material."""

    model_config = ConfigDict(extra="forbid")

    type: ImplantMaterialType
    value: str = Field(min_length=1, max_length=4096)
    role: str = Field(min_length=1, max_length=300)
    sample_location: str = Field(min_length=1, max_length=500)
    sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_observation_id: UUID

    @field_validator("value", "role", "sample_location")
    @classmethod
    def validate_single_line_text(cls, value: str) -> str:
        if not value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("must be nonblank single-line text")
        return value

    def to_domain(self) -> ImplantMaterialEditorialV1:
        return ImplantMaterialEditorialV1(
            type=self.type,
            value=self.value,
            role=self.role,
            sample_location=self.sample_location,
            sample_sha256=self.sample_sha256,
            evidence_observation_id=self.evidence_observation_id,
        )
