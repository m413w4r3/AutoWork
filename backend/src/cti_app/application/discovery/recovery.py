"""Background polling and recovery mechanics for discovery research.

Extracted from `DiscoveryService` (R27): everything about resuming a
durable/background ChatGPT research run, and about recovering one that
stalled (visible-DOM recovery, manual Markdown recovery, or asking ChatGPT
to complete its own unfinished response) lives here as an explicit
collaborator, `DiscoveryRecoveryCoordinator`.

`DiscoveryService` still owns the public API and the DiscoveryBatch/UoW
side of things — it holds a coordinator instance and delegates to it. This
module has no unit-of-work factory and no reference to `DiscoveryService`
itself: only the model-run/bridge dependencies it actually needs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery.contracts import DiscoverEditionParameters
from cti_app.application.discovery.ports import (
    BridgeCapabilitiesProvider,
    ModelOutputArchive,
)
from cti_app.application.discovery_report_parser import parse_discovery_report
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_gateway import (
    BackgroundResponsePendingError,
    ConversationContext,
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ResearchModel,
)
from cti_app.domain.model_runs import ModelRun, ModelRunStatus
from cti_app.logging import get_correlation_id

logger = logging.getLogger(__name__)


class DiscoveryRecoveryCoordinator:
    def __init__(
        self,
        research_model: ResearchModel,
        archive: ModelOutputArchive | None,
        *,
        bridge_capabilities_provider: BridgeCapabilitiesProvider | None = None,
        background_poll_interval_seconds: float = 5.0,
        background_waiter: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._research_model = research_model
        self._output_archive = archive
        self._bridge_capabilities_provider = bridge_capabilities_provider
        self._background_poll_interval_seconds = background_poll_interval_seconds
        self._background_waiter = background_waiter

    async def resume_recovery_child(
        self, parent: ModelRun, context: JobExecutionContext
    ) -> ModelExecution | None:
        if self._output_archive is None:
            return None
        raw_child_id = (parent.error_details or {}).get("recovery_child_model_run_id")
        if not isinstance(raw_child_id, str):
            return None
        try:
            child_id = UUID(raw_child_id)
        except ValueError:
            return None
        child = await self._output_archive.get_run(child_id)
        if child is None:
            raise ModelGatewayError("Recovery child ModelRun is unavailable")
        if child.status is ModelRunStatus.WAITING_BACKGROUND:
            execution = await self.poll_background_research(child.id, context)
        elif child.status is ModelRunStatus.SUCCEEDED:
            execution = await self.completed_execution_from_archive(child)
        elif child.status is ModelRunStatus.NEEDS_REVIEW:
            await self.wait_for_incomplete_review(child, context)
        else:
            raise ModelGatewayError(child.error_message or "Recovery child failed")
        if not execution.output_text:
            raise ModelGatewayError("Recovery child produced no final output")
        adopted = await self._output_archive.adopt_recovery_output(
            parent.id,
            execution.output_text.encode(),
            provenance="recovery_continuation",
            actor_id="system:recovery",
            source_model_run_id=child.id,
        )
        return ModelExecution(
            run=adopted,
            output_text=execution.output_text,
            metadata=execution.metadata,
        )

    async def preview_visible_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
    ) -> dict[str, Any]:
        if self._output_archive is None or self._bridge_capabilities_provider is None:
            raise ModelGatewayError("Recovery infrastructure is unavailable")
        parent = await self._output_archive.get_run(parent_run_id)

        # Un run peut être terminal (FAILED, WAITING_BACKGROUND) mais ChatGPT
        # a pu continuer et produire une réponse. Autoriser la récupération DOM.
        recoverable_statuses = {
            ModelRunStatus.NEEDS_REVIEW,
            ModelRunStatus.WAITING_BACKGROUND,
            ModelRunStatus.FAILED,
        }

        if (
            parent is None
            or not parent.response_id
            or (
                parent.status not in recoverable_statuses
                and not _has_recovery_provenance(parent, "visible_recovery")
            )
        ):
            raise ModelGatewayError("ModelRun is not waiting for recovery")
        recovered = await self._bridge_capabilities_provider.preview_visible_recovery(
            parent.response_id
        )
        text = recovered.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ModelGatewayError("No visible final response is recoverable")
        return {
            **self.preview_report(parameters, parent_run_id, text),
            "report_markdown": text,
        }

    async def preview_manual_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
    ) -> dict[str, Any]:
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        parent = await self._output_archive.get_run(parent_run_id)
        if parent is None or (
            parent.status
            not in {
                ModelRunStatus.NEEDS_REVIEW,
                ModelRunStatus.WAITING_BACKGROUND,
                ModelRunStatus.FAILED,
            }
            and not _has_recovery_provenance(parent, "manual_import")
        ):
            raise ModelGatewayError("ModelRun is not eligible for manual recovery")
        return self.preview_report(parameters, parent_run_id, text)

    async def adopt_visible_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        *,
        expected_sha256: str,
        actor_id: str,
    ) -> None:
        preview = await self.preview_visible_recovery(parameters, parent_run_id)
        text = preview.get("report_markdown")
        if not isinstance(text, str) or preview["sha256"] != expected_sha256:
            raise ValueError("Recovery preview no longer matches the confirmed report")
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        await self._output_archive.adopt_recovery_output(
            parent_run_id,
            text.encode(),
            provenance="visible_recovery",
            actor_id=actor_id,
        )

    def preview_report(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
    ) -> dict[str, Any]:
        parsed = parse_discovery_report(
            text,
            visible_citations=[],
            period_start=parameters.period_start,
            period_end=parameters.period_end,
            tlp=parameters.tlp,
            sensitivity=parameters.sensitivity,
            external_llm_allowed=parameters.external_llm_allowed,
            research_model_run_id=parent_run_id,
        )
        iocs = [ioc for candidate in parsed.candidates for ioc in candidate.provisional_iocs]
        counts: dict[str, int] = {}
        for ioc in iocs:
            counts[ioc.proposed_type.value] = counts.get(ioc.proposed_type.value, 0) + 1
        return {
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "subject_count": len(parsed.candidates),
            "publication_count": sum(
                len(candidate.sources) + len(candidate.incomplete_sources)
                for candidate in parsed.candidates
            ),
            "ioc_count": len(iocs),
            "ioc_type_counts": counts,
            "warnings": list(parsed.warnings),
            "subjects": [candidate.title for candidate in parsed.candidates],
        }

    async def adopt_recovery_report(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
        *,
        expected_sha256: str,
        provenance: str,
        actor_id: str,
    ) -> None:
        preview = self.preview_report(parameters, parent_run_id, text)
        if preview["sha256"] != expected_sha256:
            raise ValueError("Recovery preview no longer matches the confirmed report")
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        await self._output_archive.adopt_recovery_output(
            parent_run_id,
            text.encode(),
            provenance=provenance,
            actor_id=actor_id,
        )

    async def start_completion_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
    ) -> UUID:
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        parent = await self._output_archive.get_run(parent_run_id)
        details = parent.error_details if parent else None
        if parent is not None and _has_recovery_provenance(parent, "recovery_continuation"):
            recovery = (parent.error_details or {}).get("recovery")
            source_id = recovery.get("source_model_run_id") if isinstance(recovery, dict) else None
            if isinstance(source_id, str):
                return UUID(source_id)
        conversation = details.get("conversation") if isinstance(details, dict) else None
        if (
            parent is None
            or parent.status is not ModelRunStatus.NEEDS_REVIEW
            or not isinstance(conversation, dict)
            or not isinstance(conversation.get("id"), str)
            or not isinstance(conversation.get("external_locator"), str)
        ):
            raise ModelGatewayError("Verified discovery conversation is unavailable")
        child_id = uuid5(NAMESPACE_URL, f"{parent_run_id}:complete-initial-response:v1")
        request = ModelRequest(
            text=(
                "Ta réponse précédente ne contient pas de résultat final. Termine maintenant "
                "la mission initiale et fournis directement le rapport Markdown demandé, sans "
                "recommencer toute la recherche."
            ),
            prompt_template_id="monthly-cti-discovery-recovery",
            prompt_template_version="1.0",
            evidence_pack_hash=parent.evidence_pack_hash,
            external_llm_allowed=parameters.external_llm_allowed,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=parent.provider,
            sensitivity=parameters.sensitivity,
            parameters={
                "bridge_recovery": True,
                "recovery_parent_model_run_id": str(parent_run_id),
            },
            background=True,
            conversation=ConversationContext(
                mode="continue",
                id=UUID(conversation["id"]),
                external_locator=conversation["external_locator"],
            ),
            run_id=child_id,
        )
        child = await self._output_archive.get_run(child_id)
        if child is None:
            await self._research_model.research(request)
        elif child.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
            raise ModelGatewayError(child.error_message or "Recovery child failed")
        await self._output_archive.link_recovery_child(parent_run_id, child_id)
        return child_id

    async def poll_background_research(
        self,
        model_run_id: UUID,
        context: JobExecutionContext,
    ) -> ModelExecution:
        if self._output_archive is None:
            raise ModelGatewayError("Background research cannot be resumed")
        started = time.monotonic()
        polls = 0
        while True:
            await context.check_cancelled()
            await context.heartbeat()
            current = await self._output_archive.get_run(model_run_id)
            if current is None:
                raise ModelGatewayError(f"Model run {model_run_id} does not exist")
            if current.status is ModelRunStatus.SUCCEEDED:
                return await self.completed_execution_from_archive(current)
            if current.status is ModelRunStatus.NEEDS_REVIEW:
                await self.wait_for_incomplete_review(current, context)
            if current.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
                error = ModelGatewayError(current.error_message or "Research ModelRun failed")
                error.code = current.error_code or "research_failed"
                raise error
            try:
                execution = await self._output_archive.resume(model_run_id)
            except BackgroundResponsePendingError as exc:
                polls += 1
                await self._record_background_observation(
                    context,
                    model_run_id=model_run_id,
                    bridge_run_id=exc.response_id or current.response_id,
                    bridge_state=exc.background_status,
                    polls=polls,
                    elapsed_seconds=time.monotonic() - started,
                    progress=exc.progress or {},
                )
                await self._background_waiter(self._background_poll_interval_seconds)
                continue
            polls += 1
            await self._record_background_observation(
                context,
                model_run_id=model_run_id,
                bridge_run_id=execution.run.response_id or current.response_id,
                bridge_state="completed",
                polls=polls,
                elapsed_seconds=time.monotonic() - started,
                progress={},
            )
            if execution.run.status is not ModelRunStatus.SUCCEEDED:
                if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
                    await self.wait_for_incomplete_review(execution.run, context)
                raise ModelGatewayError("Background research returned a non-terminal result")
            if execution.output_text:
                return execution
            return await self.completed_execution_from_archive(execution.run)

    @staticmethod
    async def wait_for_incomplete_review(
        run: ModelRun,
        context: JobExecutionContext,
    ) -> NoReturn:
        details = {
            "phase": "chatgpt_incomplete",
            "reason": run.error_code or "no_final_answer",
            "model_run_id": str(run.id),
            "bridge_run_id": run.response_id,
            "correlation_id": get_correlation_id(),
            **(run.error_details or {}),
        }
        await context.wait_for_human(
            "ChatGPT s'est arrêté sans produire de réponse finale. "
            "La conversation a été conservée et peut être reprise.",
            details,
        )

    @staticmethod
    async def _record_background_observation(
        context: JobExecutionContext,
        *,
        model_run_id: UUID,
        bridge_run_id: str | None,
        bridge_state: str,
        polls: int,
        elapsed_seconds: float,
        progress: dict[str, Any],
    ) -> None:
        job_heartbeat_at = datetime.now(UTC).isoformat()
        correlation_id = get_correlation_id()
        await context.record_diagnostics(
            {
                "phase": "background_bridge_wait",
                "model_run_id": str(model_run_id),
                "bridge_run_id": bridge_run_id,
                "last_job_heartbeat": job_heartbeat_at,
                "bridge_state": bridge_state,
                "poll_count": polls,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "correlation_id": correlation_id,
                "chatgpt_phase": progress.get("phase"),
                "chatgpt_output_chars": progress.get("output_chars"),
                "chatgpt_stable_for_ms": progress.get("stable_for_ms"),
                "chatgpt_completion_signal": progress.get("completion_signal"),
            }
        )
        logger.info(
            "discovery_background_poll model_run_id=%s bridge_run_id=%s "
            "job_heartbeat_at=%s bridge_state=%s poll_count=%s elapsed_seconds=%.3f "
            "correlation_id=%s",
            model_run_id,
            bridge_run_id or "pending",
            job_heartbeat_at,
            bridge_state,
            polls,
            elapsed_seconds,
            correlation_id,
        )

    async def completed_execution_from_archive(self, run: ModelRun) -> ModelExecution:
        if self._output_archive is None or not run.output_references:
            raise ModelGatewayError("Completed research has no archived output")
        reference = run.raw_output_reference or run.output_references[-1]
        output = await self._output_archive.read_output(reference, max_bytes=10_000_000)
        text = output.decode("utf-8")
        if not text.strip():
            raise ModelGatewayError("Completed research output is empty")
        return ModelExecution(
            run=run,
            output_text=text,
            metadata={"visible_citations": list(run.visible_citations)},
        )


def _has_recovery_provenance(run: ModelRun, provenance: str) -> bool:
    recovery = (run.error_details or {}).get("recovery")
    return (
        run.status is ModelRunStatus.SUCCEEDED
        and isinstance(recovery, dict)
        and recovery.get("provenance") == provenance
    )
