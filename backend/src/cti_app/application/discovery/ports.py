from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from cti_app.application.model_gateway import ModelExecution
from cti_app.domain.model_runs import ModelRun


class BridgeCapabilitiesProvider(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...

    async def archive_conversation(self, conversation_id: UUID) -> None: ...

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, Any]: ...


class ModelOutputArchive(Protocol):
    async def get_run(self, run_id: UUID) -> ModelRun | None: ...

    async def read_output(self, reference: str, *, max_bytes: int = ...) -> bytes: ...

    async def archive_output(self, content: bytes, *, mime_type: str) -> str: ...

    async def resume(self, run_id: UUID) -> ModelExecution: ...

    async def adopt_recovery_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        provenance: str,
        actor_id: str,
        source_model_run_id: UUID | None = None,
    ) -> ModelRun: ...

    async def link_recovery_child(self, parent_run_id: UUID, child_run_id: UUID) -> None: ...

    async def record_output_diagnostics(
        self,
        run_id: UUID,
        *,
        normalized_reference: str | None,
        normalized_sha256: str | None,
        parser_stage: str,
        normalization_version: str | None,
        transformations: tuple[str, ...],
        validation_errors: tuple[dict[str, Any], ...],
        json_error_line: int | None = None,
        json_error_column: int | None = None,
    ) -> None: ...

    async def create_manual_research_output(
        self,
        run_id: UUID,
        content: bytes,
        *,
        evidence_pack_hash: str,
        actor_id: str,
        operation: str = "manual_import",
    ) -> ModelRun: ...
