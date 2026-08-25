from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    discovery_request_hash,
)
from cti_app.application.discovery.ports import (
    BridgeCapabilitiesProvider,
    ModelOutputArchive,
)
from cti_app.application.discovery.prompts import (
    PROMPT_TEMPLATE_ID,
    PROMPT_TEMPLATE_VERSION,
    _research_prompt,
)
from cti_app.application.discovery.recovery import DiscoveryRecoveryCoordinator
from cti_app.application.discovery_report_parser import (
    PARSER_VERSION,
    ParsedDiscoveryReport,
    ReportParsingError,
    parse_discovery_report,
)
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.model_gateway import (
    ConversationContext,
    ConversationLifecycleSpec,
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ResearchModel,
)
from cti_app.application.persistence import DiscoveryUnitOfWorkFactory
from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryContribution,
    DiscoverySourceMode,
    SourceCandidate,
    SourceVerificationStatus,
)
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.model_conversations import (
    ConversationLifecycle,
    ConversationPolicy,
    ConversationReleaseOutcome,
)
from cti_app.domain.model_runs import ModelProvider, ModelRunStatus
from cti_app.logging import get_correlation_id

logger = logging.getLogger(__name__)


def _wrap_candidates_as_contributions(
    candidates: list[CandidateTopic],
    status: ContributionStatus = ContributionStatus.PENDING,
) -> list[DiscoveryContribution]:
    now = datetime.now(UTC)
    return [
        DiscoveryContribution(
            candidate=candidate,
            status=status,
            created_at=now,
            accepted_at=now if status == ContributionStatus.ACCEPTED else None,
        )
        for candidate in candidates
    ]


class SourceCandidateNotFoundError(LookupError):
    pass


class DiscoveryService:
    def __init__(
        self,
        uow_factory: DiscoveryUnitOfWorkFactory,
        research_model: ResearchModel,
        archive: ModelOutputArchive | None,
        *,
        bridge_capabilities: Mapping[str, object] | None = None,
        bridge_capabilities_provider: BridgeCapabilitiesProvider | None = None,
        after_persisted_batch: Callable[
            [DiscoveryBatch, DiscoveryInputMode, str], Awaitable[object]
        ]
        | None = None,
        background_poll_interval_seconds: float = 5.0,
        background_waiter: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._uow_factory = uow_factory
        self._research_model = research_model
        self._output_archive = archive
        self._bridge_capabilities_provider = bridge_capabilities_provider
        self._after_persisted_batch = after_persisted_batch
        self._recovery = DiscoveryRecoveryCoordinator(
            research_model,
            archive,
            bridge_capabilities_provider=bridge_capabilities_provider,
            background_poll_interval_seconds=background_poll_interval_seconds,
            background_waiter=background_waiter,
        )
        self._bridge_capabilities = dict(
            bridge_capabilities
            or {
                "transport": "chatgpt_web_ui",
                "web_search": "prompt_instructed",
                "structured_output": "prompt_and_client_validation",
                "background": "memory_only",
                "native_usage": False,
                "native_sources": False,
            }
        )

    async def discover_edition(
        self, parameters: DiscoverEditionParameters, context: JobExecutionContext
    ) -> DiscoveryBatch:
        request_hash = discovery_request_hash(parameters)
        async with self._uow_factory() as uow:
            existing = await uow.discovery_batches.get_by_request_hash(
                parameters.edition_id, request_hash
            )
            if existing is not None:
                return existing

        await context.report_progress(1, 4, "Préparation de la recherche sourcée")
        bridge_capabilities = await self._capabilities_snapshot()
        research_run_id = uuid5(NAMESPACE_URL, f"cti-discovery-model-run:{request_hash}")
        fresh_conversation_id = uuid5(NAMESPACE_URL, f"cti-discovery-conversation:{request_hash}")

        conversation_lifecycle = ConversationLifecycle(
            id=fresh_conversation_id,
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )
        async with self._uow_factory() as uow:
            await uow.conversation_lifecycles.add(conversation_lifecycle)
            await uow.commit()

        research_request = ModelRequest(
            text=_research_prompt(parameters),
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            evidence_pack_hash=request_hash,
            external_llm_allowed=parameters.external_llm_allowed,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=ModelProvider.OPENAI,
            sensitivity=parameters.sensitivity,
            metadata={
                "edition_id": str(parameters.edition_id),
                "tlp": parameters.tlp.value,
                "source_profile_id": parameters.source_profile,
                "collected_at": datetime.now(UTC).isoformat(),
            },
            parameters={"reasoning": {"effort": "high"}},
            background=True,
            conversation=ConversationContext(mode="fresh", id=fresh_conversation_id),
            conversation_lifecycle=ConversationLifecycleSpec(
                policy=ConversationPolicy.DELETE_ON_SUCCESS,
            ),
            run_id=research_run_id,
        )
        await context.report_progress(2, 4, "ChatGPT recherche et analyse les sources")
        research = await self._research_or_resume(research_request, context)
        if not research.output_text:
            raise ModelGatewayError("Research model returned no text")

        await context.report_progress(3, 4, "Analyse locale du rapport archivé")
        try:
            parsed = parse_discovery_report(
                research.output_text,
                visible_citations=research.metadata.get("visible_citations", []),
                period_start=parameters.period_start,
                period_end=parameters.period_end,
                tlp=parameters.tlp,
                sensitivity=parameters.sensitivity,
                external_llm_allowed=parameters.external_llm_allowed,
                research_model_run_id=research.run.id,
            )
        except ReportParsingError as exc:
            exc.research_model_run_id = research.run.id
            raise
        await self._record_parser_diagnostics(research.run.id, parsed)
        batch = _parsed_to_domain_batch(
            parameters,
            request_hash,
            parsed,
            research.run.id,
            bridge_capabilities,
        )
        async with self._uow_factory() as uow:
            inserted = await uow.discovery_batches.add_if_absent(batch)
            if not inserted:
                existing = await uow.discovery_batches.get_by_request_hash(
                    parameters.edition_id, request_hash
                )
                if existing is None:
                    raise RuntimeError("Discovery conflict without canonical batch")
                batch = existing
            await uow.commit()

        async with self._uow_factory() as uow:
            lifecycle = await uow.conversation_lifecycles.get(fresh_conversation_id)
            if lifecycle is not None:
                lifecycle.release(outcome=ConversationReleaseOutcome.SUCCESS)
                await uow.conversation_lifecycles.save(lifecycle)
                await uow.commit()
        # DELETE_ON_SUCCESS only means something if something acts on it: the
        # lifecycle row above records the policy, this call is what actually
        # closes the ChatGPT-side conversation now that it succeeded.
        await self._archive_ephemeral_conversation(fresh_conversation_id)

        if self._after_persisted_batch is not None:
            await self._after_persisted_batch(
                batch, DiscoveryInputMode.BRIDGE_RESEARCH, "system:discovery"
            )
        await context.report_progress(4, 4, "Candidats proposés — vérification humaine requise")
        return batch

    async def _research_or_resume(
        self,
        request: ModelRequest,
        context: JobExecutionContext,
    ) -> ModelExecution:
        """Submit once, then durably poll the persisted background ModelRun."""
        if request.run_id is None:
            raise ModelGatewayError("Discovery research requires a stable ModelRun id")
        existing = (
            await self._output_archive.get_run(request.run_id)
            if self._output_archive is not None
            else None
        )
        if existing is not None:
            if existing.status is ModelRunStatus.SUCCEEDED:
                return await self._recovery.completed_execution_from_archive(existing)
            if existing.status is ModelRunStatus.WAITING_BACKGROUND:
                return await self._recovery.poll_background_research(existing.id, context)
            if existing.status is ModelRunStatus.NEEDS_REVIEW:
                recovered = await self._recovery.resume_recovery_child(existing, context)
                if recovered is not None:
                    return recovered
                await self._recovery.wait_for_incomplete_review(existing, context)
            if existing.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
                error = ModelGatewayError(existing.error_message or "Research ModelRun failed")
                error.code = existing.error_code or "research_failed"
                raise error

        # RUNNING signifie que l'identité a pu être persistée avant une réponse
        # HTTP incertaine. Le POST idempotent avec le même run id peut alors
        # uniquement rejoindre le run bridge ; il ne produit jamais un second clic.
        execution = await self._research_model.research(request)
        if execution.run.status is ModelRunStatus.WAITING_BACKGROUND:
            return await self._recovery.poll_background_research(execution.run.id, context)
        if execution.run.status is ModelRunStatus.SUCCEEDED and not execution.output_text:
            return await self._recovery.completed_execution_from_archive(execution.run)
        return execution

    async def preview_visible_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
    ) -> dict[str, Any]:
        return await self._recovery.preview_visible_recovery(parameters, parent_run_id)

    async def preview_manual_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        text: str,
    ) -> dict[str, Any]:
        return await self._recovery.preview_manual_recovery(parameters, parent_run_id, text)

    async def adopt_visible_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
        *,
        expected_sha256: str,
        actor_id: str,
    ) -> None:
        await self._recovery.adopt_visible_recovery(
            parameters,
            parent_run_id,
            expected_sha256=expected_sha256,
            actor_id=actor_id,
        )

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
        await self._recovery.adopt_recovery_report(
            parameters,
            parent_run_id,
            text,
            expected_sha256=expected_sha256,
            provenance=provenance,
            actor_id=actor_id,
        )

    async def start_completion_recovery(
        self,
        parameters: DiscoverEditionParameters,
        parent_run_id: UUID,
    ) -> UUID:
        return await self._recovery.start_completion_recovery(parameters, parent_run_id)

    async def preview_standalone_import(
        self,
        parameters: DiscoverEditionParameters,
        markdown: str,
    ) -> dict[str, Any]:
        """Preview only: no persistence, so the caller can confirm content before import."""
        digest = hashlib.sha256(markdown.encode()).hexdigest()
        manual_run_id = uuid5(
            NAMESPACE_URL,
            f"cti-discovery-manual-import:{parameters.edition_id}:{digest}",
        )
        return self._recovery.preview_report(parameters, manual_run_id, markdown)

    async def import_standalone_report(
        self,
        parameters: DiscoverEditionParameters,
        markdown: str,
        *,
        expected_sha256: str,
        actor_id: str,
    ) -> tuple[DiscoveryBatch, bool, UUID | None]:
        """Import a standalone ChatGPT Markdown report as a self-contained contribution."""
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")

        digest = hashlib.sha256(markdown.encode()).hexdigest()
        manual_run_id = uuid5(
            NAMESPACE_URL,
            f"cti-discovery-manual-import:{parameters.edition_id}:{digest}",
        )
        manual_request_hash = hashlib.sha256(
            f"manual-import:v1:{parameters.edition_id}:{digest}".encode()
        ).hexdigest()

        preview = self._recovery.preview_report(parameters, manual_run_id, markdown)
        if preview["sha256"] != expected_sha256:
            raise ValueError("Import preview no longer matches the confirmed report")

        async with self._uow_factory() as uow:
            existing_batch = await uow.discovery_batches.get_by_request_hash(
                parameters.edition_id, manual_request_hash
            )
            if existing_batch is not None:
                return existing_batch, True, None

        await self._output_archive.create_manual_research_output(
            manual_run_id,
            markdown.encode(),
            # Le hash de requête manuelle tient lieu d'empreinte d'entrée : il est
            # déterministe pour (édition, contenu) et satisfait l'invariant SHA-256.
            evidence_pack_hash=manual_request_hash,
            actor_id=actor_id,
        )

        parsed = parse_discovery_report(
            markdown,
            visible_citations=[],
            period_start=parameters.period_start,
            period_end=parameters.period_end,
            tlp=parameters.tlp,
            sensitivity=parameters.sensitivity,
            external_llm_allowed=parameters.external_llm_allowed,
            research_model_run_id=manual_run_id,
        )

        await self._record_parser_diagnostics(manual_run_id, parsed)

        batch = _parsed_to_domain_batch(
            parameters,
            manual_request_hash,
            parsed,
            manual_run_id,
            bridge_capabilities={
                "transport": "manual_import",
                "web_search": "performed_outside_autowork",
                "native_sources": False,
                "native_usage": False,
                "snapshot_available": False,
            },
            source_mode=DiscoverySourceMode.MANUAL_IMPORT,
        )

        # Une insertion concurrente du même Markdown partage le même request_hash :
        # on adopte le batch canonique.
        async with self._uow_factory() as uow:
            inserted = await uow.discovery_batches.add_if_absent(batch)
            if not inserted:
                existing = await uow.discovery_batches.get_by_request_hash(
                    parameters.edition_id, manual_request_hash
                )
                if existing is None:
                    raise RuntimeError("Discovery conflict without canonical batch")
                await uow.commit()
                return existing, True, None
            await uow.commit()

        # Ceci ne fait que soumettre et dispatcher un job de réconciliation
        # ASYNCHRONE : la consolidation (fusion en sujets) n'est pas terminée
        # quand cet appel revient. Le job id est renvoyé pour que l'appelant
        # puisse suivre son achèvement au lieu de rafraîchir l'état trop tôt.
        reconciliation_job_id: UUID | None = None
        if self._after_persisted_batch is not None:
            job = await self._after_persisted_batch(
                batch, DiscoveryInputMode.MANUAL_IMPORT, actor_id
            )
            job_id = getattr(job, "id", None)
            if isinstance(job_id, UUID):
                reconciliation_job_id = job_id

        return batch, False, reconciliation_job_id

    async def read_archived_report(self, edition_id: UUID, research_run_id: UUID) -> str:
        if self._output_archive is None:
            raise ReportParsingError("report_unavailable", "Archive de rapports indisponible.")
        batches = await self.list_batches(edition_id, include_replaced=True)
        if not any(batch.discovery_model_run_id == research_run_id for batch in batches):
            raise ReportParsingError("report_unavailable", "Rapport archivé introuvable.")
        run = await self._output_archive.get_run(research_run_id)
        if run is None or not run.output_references:
            raise ReportParsingError("report_unavailable", "Rapport archivé introuvable.")
        content = await self._output_archive.read_output(
            run.raw_output_reference or run.output_references[-1], max_bytes=10_000_000
        )
        if not content:
            raise ReportParsingError("report_empty", "Le rapport archivé est vide.")
        return content.decode(errors="replace")

    async def _record_parser_diagnostics(self, run_id: UUID, parsed: ParsedDiscoveryReport) -> None:
        if self._output_archive is None:
            return
        validation_errors = tuple(
            {
                "path": ["report"],
                "code": warning.split(":", 1)[0][:128],
                "value_sha256": hashlib.sha256(warning.encode()).hexdigest(),
            }
            for warning in parsed.warnings
        )
        await self._output_archive.record_output_diagnostics(
            run_id,
            normalized_reference=None,
            normalized_sha256=parsed.report_sha256,
            parser_stage=("report_parsing_partial" if parsed.status == "partial" else "completed"),
            normalization_version=PARSER_VERSION,
            transformations=("deterministic_markdown_parsing",),
            validation_errors=validation_errors,
        )

    async def _capabilities_snapshot(self) -> dict[str, object]:
        if self._bridge_capabilities_provider is None:
            return dict(self._bridge_capabilities)
        try:
            capabilities = await self._bridge_capabilities_provider.capabilities()
        except Exception as exc:
            return {
                **self._bridge_capabilities,
                "snapshot_available": False,
                "snapshot_error_type": type(exc).__name__,
            }
        return {**capabilities, "snapshot_available": True}

    async def _archive_ephemeral_conversation(self, conversation_id: UUID | None) -> None:
        if conversation_id is None or self._bridge_capabilities_provider is None:
            return
        try:
            await self._bridge_capabilities_provider.archive_conversation(conversation_id)
        except Exception as exc:
            logger.warning(
                "discovery_ephemeral_conversation_archive_failed conversation_id=%s "
                "correlation_id=%s error_type=%s",
                conversation_id,
                get_correlation_id(),
                type(exc).__name__,
            )

    async def list_batches(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> list[DiscoveryBatch]:
        async with self._uow_factory() as uow:
            batches = list(await uow.discovery_batches.list_for_edition(edition_id))
            return (
                batches
                if include_replaced
                else [item for item in batches if item.is_active_revision]
            )

    async def mark_source(
        self,
        edition_id: UUID,
        source_id: UUID,
        status: SourceVerificationStatus,
        *,
        actor_id: str,
    ) -> SourceCandidate:
        async with self._uow_factory() as uow:
            batches = await uow.discovery_batches.list_for_edition(edition_id)
            for batch in batches:
                source = batch.source(source_id)
                if source is not None:
                    source.mark(status, actor_id=actor_id)
                    await uow.discovery_batches.save(batch)
                    await uow.commit()
                    return source
        raise SourceCandidateNotFoundError(str(source_id))


def _parsed_to_domain_batch(
    parameters: DiscoverEditionParameters,
    request_hash: str,
    result: ParsedDiscoveryReport,
    research_run_id: UUID,
    bridge_capabilities: Mapping[str, object],
    *,
    source_mode: DiscoverySourceMode = DiscoverySourceMode.MODEL_DECLARED_URLS,
) -> DiscoveryBatch:
    return DiscoveryBatch(
        edition_id=parameters.edition_id,
        request_hash=request_hash,
        complementary_axis=parameters.complementary_axis,
        queries=(),
        citations=result.citations,
        contributions=_wrap_candidates_as_contributions(
            result.candidates, ContributionStatus.ACCEPTED
        ),
        discovery_model_run_id=research_run_id,
        tlp=parameters.tlp,
        sensitivity=parameters.sensitivity,
        external_llm_allowed=parameters.external_llm_allowed,
        report_sha256=result.report_sha256,
        parser_version=PARSER_VERSION,
        parsing_status=("report_parsing_partial" if result.status == "partial" else "completed"),
        parsing_warnings=result.warnings,
        unattached_visible_citations=result.unattached_visible_citations,
        source_mode=source_mode,
        bridge_capabilities=dict(bridge_capabilities),
        citation_count=len(result.citations),
        source_coverage_complete=False,
        source_coverage_incomplete_reason=(
            "Le rapport Markdown et les citations visibles ne constituent pas une liste "
            "exhaustive des sources consultées."
        ),
    )
