from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class ModelProvider(StrEnum):
    OPENAI = "openai"
    QWEN = "qwen"
    FAKE = "fake"


class ModelRole(StrEnum):
    RESEARCH = "research"
    STRUCTURED_EXTRACTION = "structured_extraction"
    DRAFTING = "drafting"
    CRITIC = "critic"


class ModelRunStatus(StrEnum):
    RUNNING = "running"
    WAITING_BACKGROUND = "waiting_background"
    NEEDS_REVIEW = "needs_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise ValueError("Token usage cannot be negative")
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ValueError("Total token usage is inconsistent")

    def snapshot(self) -> dict[str, int | bool]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
        }


@dataclass(slots=True)
class ModelRun:
    provider: ModelProvider
    model_role: ModelRole
    requested_model: str
    prompt_template_id: str
    prompt_template_version: str
    authorized_input_hash: str
    evidence_pack_hash: str
    parameters: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    actual_model_version: str | None = None
    duration_ms: int | None = None
    usage: ModelUsage | None = None
    status: ModelRunStatus = ModelRunStatus.RUNNING
    response_id: str | None = None
    output_references: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    raw_output_reference: str | None = None
    raw_output_sha256: str | None = None
    raw_output_chars: int | None = None
    normalized_output_reference: str | None = None
    normalized_output_sha256: str | None = None
    parser_stage: str | None = None
    serializer_version: str | None = None
    normalization_version: str | None = None
    json_error_line: int | None = None
    json_error_column: int | None = None
    validation_errors: tuple[dict[str, Any], ...] = ()
    transformations: tuple[str, ...] = ()
    citation_count: int = 0
    extracted_url_count: int = 0
    visible_citations: tuple[dict[str, Any], ...] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.requested_model.strip():
            raise ValueError("Requested model must not be empty")
        if not self.prompt_template_id.strip() or not self.prompt_template_version.strip():
            raise ValueError("Prompt template identity and version are required")
        for name, value in (
            ("authorized_input_hash", self.authorized_input_hash),
            ("evidence_pack_hash", self.evidence_pack_hash),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} must be a lowercase SHA-256")

    def wait_for_background(
        self,
        *,
        response_id: str,
        actual_model_version: str | None,
        usage: ModelUsage | None,
        now: datetime | None = None,
    ) -> None:
        self._require_active()
        if not response_id:
            raise ValueError("A background response id is required")
        self.status = ModelRunStatus.WAITING_BACKGROUND
        self.response_id = response_id
        self.actual_model_version = actual_model_version
        self.usage = usage
        self.updated_at = now or datetime.now(UTC)

    def succeed(
        self,
        *,
        actual_model_version: str | None,
        duration_ms: int,
        usage: ModelUsage,
        output_references: tuple[str, ...],
        response_id: str | None,
        now: datetime | None = None,
    ) -> None:
        self._require_active()
        if duration_ms < 0 or not output_references:
            raise ValueError("A completed run requires duration and stored output")
        timestamp = now or datetime.now(UTC)
        self.status = ModelRunStatus.SUCCEEDED
        self.actual_model_version = actual_model_version
        self.duration_ms = duration_ms
        self.usage = usage
        self.response_id = response_id or self.response_id
        self.output_references = output_references
        self.error_code = None
        self.error_message = None
        self.error_details = None
        self.finished_at = timestamp
        self.updated_at = timestamp

    def fail(
        self,
        code: str,
        public_message: str,
        *,
        blocked: bool = False,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._require_active()
        timestamp = now or datetime.now(UTC)
        self.status = ModelRunStatus.BLOCKED if blocked else ModelRunStatus.FAILED
        self.error_code = code[:64]
        self.error_message = " ".join(public_message.replace("\x00", "").split())[:500]
        self.error_details = details
        self.finished_at = timestamp
        self.updated_at = timestamp

    def require_review(
        self,
        code: str,
        public_message: str,
        *,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self._require_active()
        timestamp = now or datetime.now(UTC)
        self.status = ModelRunStatus.NEEDS_REVIEW
        self.error_code = code[:64]
        self.error_message = " ".join(public_message.replace("\x00", "").split())[:500]
        self.error_details = details
        self.finished_at = timestamp
        self.updated_at = timestamp

    def adopt_recovery(
        self,
        *,
        output_reference: str,
        output_sha256: str,
        output_chars: int,
        provenance: str,
        actor_id: str,
        source_model_run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> None:
        allowed = {ModelRunStatus.NEEDS_REVIEW}
        if provenance in {"manual_import", "visible_recovery"}:
            allowed |= {
                ModelRunStatus.WAITING_BACKGROUND,
                ModelRunStatus.FAILED,
            }
        if self.status not in allowed:
            raise ValueError("Model run is not eligible for this recovery")
        timestamp = now or datetime.now(UTC)
        previous = dict(self.error_details or {})
        previous["recovery"] = {
            "provenance": provenance,
            "actor_id": actor_id,
            "adopted_at": timestamp.isoformat(),
            "source_model_run_id": str(source_model_run_id) if source_model_run_id else None,
        }
        self.status = ModelRunStatus.SUCCEEDED
        self.output_references = (*self.output_references, output_reference)
        self.raw_output_reference = output_reference
        self.raw_output_sha256 = output_sha256
        self.raw_output_chars = output_chars
        self.error_code = None
        self.error_message = None
        self.error_details = previous
        self.duration_ms = max(0, int((timestamp - self.started_at).total_seconds() * 1000))
        self.usage = self.usage or ModelUsage(estimated=True)
        self.finished_at = timestamp
        self.updated_at = timestamp

    def succeed_manual_import(
        self,
        *,
        output_reference: str,
        output_sha256: str,
        output_chars: int,
        actor_id: str,
        now: datetime | None = None,
    ) -> None:
        """Clôture un run synthétique représentant un Markdown fourni par un humain.

        Aucun modèle n'a été appelé : l'usage est marqué estimé et la provenance
        est conservée dans ``error_details["recovery"]`` — nom historique, format
        attendu par ``_has_recovery_provenance``.
        """
        if self.status is not ModelRunStatus.RUNNING:
            raise ValueError("A manual import can only complete a running run")
        timestamp = now or datetime.now(UTC)
        self.status = ModelRunStatus.SUCCEEDED
        self.output_references = (*self.output_references, output_reference)
        self.raw_output_reference = output_reference
        self.raw_output_sha256 = output_sha256
        self.raw_output_chars = output_chars
        self.duration_ms = 0
        self.usage = ModelUsage(estimated=True)
        self.error_code = None
        self.error_message = None
        self.error_details = {
            "recovery": {
                "provenance": "manual_import",
                "actor_id": actor_id,
                "adopted_at": timestamp.isoformat(),
                "source_model_run_id": None,
            }
        }
        self.finished_at = timestamp
        self.updated_at = timestamp

    def _require_active(self) -> None:
        if self.status not in {ModelRunStatus.RUNNING, ModelRunStatus.WAITING_BACKGROUND}:
            raise ValueError(f"Model run is already terminal: {self.status.value}")


@dataclass(frozen=True, slots=True)
class ModelOutputRejection:
    model_run_id: UUID
    path: tuple[str, ...]
    error_type: str
    value_sha256: str
    raw_output_reference: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
