from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    ReprocessDiscoveryReportParameters,
    discover_parameters_from_run,
    discovery_conversation_id,
    discovery_initial_batch_id,
    discovery_request_hash,
    discovery_request_snapshot,
    discovery_research_model_run_id,
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
    DiscoveryRun,
    DiscoveryRunInputMode,
    DiscoverySourceMode,
    SourceCandidate,
    SourceVerificationStatus,
)
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.model_runs import ModelRunStatus
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
            existing = await uow.discovery_batches.get(
                discovery_initial_batch_id(parameters.discovery_run_id)
            )
            if existing is not None:
                return existing

        await context.report_progress(1, 4, "Préparation de la recherche sourcée")
        bridge_capabilities = await self._capabilities_snapshot()
        research_run_id = discovery_research_model_run_id(parameters.discovery_run_id)
        fresh_conversation_id = discovery_conversation_id(parameters.discovery_run_id)

        research_request = ModelRequest(
            text=_research_prompt(parameters),
            prompt_template_id=PROMPT_TEMPLATE_ID,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            evidence_pack_hash=request_hash,
            external_llm_allowed=parameters.external_llm_allowed,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            sensitivity=parameters.sensitivity,
            metadata={
                "edition_id": str(parameters.edition_id),
                "discovery_run_id": str(parameters.discovery_run_id),
                "discovery_request_hash": request_hash,
                "tlp": parameters.tlp.value,
                "source_profile_id": parameters.source_profile,
                "collected_at": datetime.now(UTC).isoformat(),
            },
            parameters={"reasoning": {"effort": "high"}},
            background=True,
            conversation=ConversationContext(mode="fresh", id=fresh_conversation_id),
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
                existing = await uow.discovery_batches.get(batch.id)
                if existing is None:
                    raise RuntimeError("Discovery conflict without canonical batch")
                batch = existing
            await uow.commit()

        # This discovery conversation is bounded (DELETE_ON_SUCCESS semantics):
        # only close its live Temporary Chat browser session once the batch it
        # produced is durably persisted as the canonical successful result.
        # Generation error, needs_review, parser failure, or uncertain recovery
        # never reach this line — the session stays alive for inspection/recovery.
        await self._archive_ephemeral_conversation(fresh_conversation_id)

        if self._after_persisted_batch is not None:
            await self._after_persisted_batch(
                batch, DiscoveryInputMode.BRIDGE_RESEARCH, "system:discovery"
            )
        await context.report_progress(4, 4, "Candidats proposés — vérification humaine requise")
        return batch

    async def reprocess_archived_report(
        self,
        parameters: ReprocessDiscoveryReportParameters,
        context: JobExecutionContext,
    ) -> DiscoveryBatch:
        async with self._uow_factory() as uow:
            run = await uow.discovery_runs.get(parameters.discovery_run_id)
            if run is None or run.edition_id != parameters.edition_id:
                raise LookupError(str(parameters.discovery_run_id))
            discover_parameters = discover_parameters_from_run(run)
            current = await uow.discovery_batches.get(
                discovery_initial_batch_id(run.id)
            )
            if current is None:
                raise LookupError(str(run.id))
            visited: set[UUID] = set()
            chain: list[DiscoveryBatch] = []
            while current is not None:
                if current.id in visited:
                    raise RuntimeError("Discovery batch replacement cycle")
                if current.discovery_run_id != run.id:
                    raise ValueError("Discovery batch does not belong to its run")
                visited.add(current.id)
                chain.append(current)
                if current.replaced_by_batch_id is None:
                    break
                current = await uow.discovery_batches.get(current.replaced_by_batch_id)
                if current is None:
                    raise RuntimeError("Discovery batch replacement chain is incomplete")

        if not any(
            batch.discovery_model_run_id == parameters.research_model_run_id
            for batch in chain
        ):
            raise ValueError("Archived report is not part of the discovery run revision chain")

        report = await self.read_archived_report(
            parameters.edition_id, parameters.research_model_run_id
        )
        await context.report_progress(1, 2, "Analyse locale du rapport archivé")
        try:
            parsed = parse_discovery_report(
                report,
                visible_citations=[],
                period_start=discover_parameters.period_start,
                period_end=discover_parameters.period_end,
                tlp=discover_parameters.tlp,
                sensitivity=discover_parameters.sensitivity,
                external_llm_allowed=discover_parameters.external_llm_allowed,
                research_model_run_id=parameters.research_model_run_id,
            )
        except ReportParsingError as exc:
            exc.research_model_run_id = parameters.research_model_run_id
            raise
        await self._record_parser_diagnostics(parameters.research_model_run_id, parsed)

        batch = _parsed_to_domain_batch(
            discover_parameters,
            discovery_request_hash(discover_parameters),
            parsed,
            parameters.research_model_run_id,
            chain[-1].bridge_capabilities,
            source_mode=chain[-1].source_mode,
            batch_id=uuid5(NAMESPACE_URL, f"cti-discovery-batch-reprocess:{context.job_id}"),
            parsing_revision=chain[-1].parsing_revision + 1,
            supersedes_batch_id=chain[-1].id,
        )
        async with self._uow_factory() as uow:
            existing = await uow.discovery_batches.get(batch.id)
            if existing is not None:
                return existing
            if batch.supersedes_batch_id is None:
                raise RuntimeError("Reprocessed batch has no superseded batch")
            previous = await uow.discovery_batches.get(batch.supersedes_batch_id)
            if previous is None or previous.discovery_run_id != run.id:
                raise RuntimeError("Current discovery batch revision is unavailable")
            inserted = await uow.discovery_batches.add_if_absent(batch)
            if not inserted:
                existing = await uow.discovery_batches.get(batch.id)
                if existing is None:
                    raise RuntimeError("Discovery conflict without canonical batch")
                return existing
            previous.replaced_by_batch_id = batch.id
            await uow.discovery_batches.save(previous)
            await uow.commit()

        if self._after_persisted_batch is not None:
            await self._after_persisted_batch(
                batch, DiscoveryInputMode.RECOVERY, parameters.actor_id
            )
        await context.report_progress(2, 2, "Révision du rapport archivé terminée")
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
        return self._recovery.preview_report(parameters, parameters.discovery_run_id, markdown)

    async def import_standalone_report(
        self,
        parameters: DiscoverEditionParameters,
        markdown: str,
        *,
        expected_sha256: str,
        actor_id: str,
        idempotency_key: str,
    ) -> tuple[DiscoveryBatch, bool, UUID | None]:
        """Import a standalone ChatGPT Markdown report as a self-contained contribution."""
        if self._output_archive is None:
            raise ModelGatewayError("Model output archive is unavailable")
        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key is required")

        async with self._uow_factory() as uow:
            existing_run = await uow.discovery_runs.get_by_idempotency_key(
                parameters.edition_id,
                DiscoveryRunInputMode.MANUAL_IMPORT,
                idempotency_key,
            )
            if existing_run is not None:
                existing_batch = await uow.discovery_batches.get(
                    discovery_initial_batch_id(existing_run.id)
                )
                if existing_batch is None:
                    raise RuntimeError("Manual import run has no canonical batch")
                return existing_batch, True, None

        digest = hashlib.sha256(markdown.encode()).hexdigest()
        manual_request_hash = hashlib.sha256(
            f"manual-import:v1:{parameters.edition_id}:{digest}".encode()
        ).hexdigest()

        preview = self._recovery.preview_report(parameters, parameters.discovery_run_id, markdown)
        if preview["sha256"] != expected_sha256:
            raise ValueError("Import preview no longer matches the confirmed report")

        discovery_run_id = uuid4()
        effective_parameters = parameters.model_copy(update={"discovery_run_id": discovery_run_id})
        manual_run_id = discovery_research_model_run_id(discovery_run_id)

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
            period_start=effective_parameters.period_start,
            period_end=effective_parameters.period_end,
            tlp=effective_parameters.tlp,
            sensitivity=effective_parameters.sensitivity,
            external_llm_allowed=effective_parameters.external_llm_allowed,
            research_model_run_id=manual_run_id,
        )

        await self._record_parser_diagnostics(manual_run_id, parsed)

        batch = _parsed_to_domain_batch(
            effective_parameters,
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

        run = DiscoveryRun(
            id=discovery_run_id,
            edition_id=effective_parameters.edition_id,
            input_mode=DiscoveryRunInputMode.MANUAL_IMPORT,
            source_profile=effective_parameters.source_profile,
            complementary_axis=effective_parameters.complementary_axis,
            request_snapshot=discovery_request_snapshot(effective_parameters),
            idempotency_key=idempotency_key,
            created_by=actor_id,
        )

        # Parse and archive are deliberately complete before this transaction. The run
        # and its canonical initial batch become visible together.
        async with self._uow_factory() as uow:
            inserted = await uow.discovery_runs.add_if_absent(run)
            if not inserted:
                existing_run = await uow.discovery_runs.get_by_idempotency_key(
                    effective_parameters.edition_id,
                    DiscoveryRunInputMode.MANUAL_IMPORT,
                    idempotency_key,
                )
                if existing_run is None:
                    raise RuntimeError("Discovery conflict without canonical run")
                existing_batch = await uow.discovery_batches.get(
                    discovery_initial_batch_id(existing_run.id)
                )
                if existing_batch is None:
                    raise RuntimeError("Discovery conflict without canonical batch")
                await uow.commit()
                return existing_batch, True, None
            if not await uow.discovery_batches.add_if_absent(batch):
                raise RuntimeError("Discovery conflict without canonical batch")
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
    batch_id: UUID | None = None,
    parsing_revision: int = 1,
    supersedes_batch_id: UUID | None = None,
) -> DiscoveryBatch:
    return DiscoveryBatch(
        edition_id=parameters.edition_id,
        discovery_run_id=parameters.discovery_run_id,
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
        id=batch_id or discovery_initial_batch_id(parameters.discovery_run_id),
        parsing_revision=parsing_revision,
        supersedes_batch_id=supersedes_batch_id,
    )
