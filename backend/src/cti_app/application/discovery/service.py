from __future__ import annotations

# ruff: noqa: RUF001 - The exact French business prompt intentionally uses typographic apostrophes.
import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator

from cti_app.application.discovery.ports import (
    BridgeCapabilitiesProvider,
    ModelOutputArchive,
)
from cti_app.application.discovery_report_parser import (
    PARSER_VERSION,
    ParsedDiscoveryReport,
    ReportParsingError,
    parse_discovery_report,
)
from cti_app.application.jobs import (
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.model_gateway import (
    BackgroundResponsePendingError,
    ConversationContext,
    ConversationLifecycleSpec,
    ModelExecution,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ResearchModel,
)
from cti_app.application.persistence import DiscoveryUnitOfWorkFactory
from cti_app.domain.classification import TLP
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
from cti_app.domain.model_runs import ModelProvider, ModelRun, ModelRunStatus
from cti_app.logging import get_correlation_id

DISCOVERY_JOB_KIND = "discover_edition"
# Compatibility import for callers compiled against the previous name.
PROMPT_TEMPLATE_ID = "monthly-cti-discovery"
PROMPT_TEMPLATE_VERSION = "4.1"
logger = logging.getLogger(__name__)


def _wrap_candidates_as_contributions(
    candidates: list[CandidateTopic],
    status: ContributionStatus = ContributionStatus.PENDING,
) -> list[DiscoveryContribution]:
    """Wrap candidate topics into contributions with temporal tracking."""
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


class DiscoverEditionParameters(JobParameters):
    edition_id: UUID
    country: str = Field(min_length=2, max_length=100)
    country_aliases: list[str] = Field(min_length=1, max_length=30)
    period_start: date
    period_end: date
    as_of_date: date = Field(default_factory=date.today)
    languages: list[str] = Field(min_length=1, max_length=10)
    source_profile: str = Field(min_length=1, max_length=128)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    tlp: TLP
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True
    research_nonce: UUID | None = None

    @field_validator("edition_id", "research_nonce", mode="before")
    @classmethod
    def parse_edition_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) and value else value

    @field_validator("period_start", "period_end", "as_of_date", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("tlp", mode="before")
    @classmethod
    def parse_tlp(cls, value: object) -> object:
        return TLP(value) if isinstance(value, str) else value


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
        self._background_poll_interval_seconds = background_poll_interval_seconds
        self._background_waiter = background_waiter
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

        # Create and persist the conversation lifecycle (DELETE_ON_SUCCESS policy)
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

        # Release the conversation lifecycle with SUCCESS after batch commit
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
                return await self._completed_execution_from_archive(existing)
            if existing.status is ModelRunStatus.WAITING_BACKGROUND:
                return await self._poll_background_research(existing.id, context)
            if existing.status is ModelRunStatus.NEEDS_REVIEW:
                recovered = await self._resume_recovery_child(existing, context)
                if recovered is not None:
                    return recovered
                await self._wait_for_incomplete_review(existing, context)
            if existing.status in {ModelRunStatus.FAILED, ModelRunStatus.BLOCKED}:
                error = ModelGatewayError(existing.error_message or "Research ModelRun failed")
                error.code = existing.error_code or "research_failed"
                raise error

        # RUNNING signifie que l'identité a pu être persistée avant une réponse
        # HTTP incertaine. Le POST idempotent avec le même run id peut alors
        # uniquement rejoindre le run bridge ; il ne produit jamais un second clic.
        execution = await self._research_model.research(request)
        if execution.run.status is ModelRunStatus.WAITING_BACKGROUND:
            return await self._poll_background_research(execution.run.id, context)
        if execution.run.status is ModelRunStatus.SUCCEEDED and not execution.output_text:
            return await self._completed_execution_from_archive(execution.run)
        return execution

    async def _resume_recovery_child(
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
            execution = await self._poll_background_research(child.id, context)
        elif child.status is ModelRunStatus.SUCCEEDED:
            execution = await self._completed_execution_from_archive(child)
        elif child.status is ModelRunStatus.NEEDS_REVIEW:
            await self._wait_for_incomplete_review(child, context)
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
            **self._preview_report(parameters, parent_run_id, text),
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
        return self._preview_report(parameters, parent_run_id, text)

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

    def _preview_report(
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
        preview = self._preview_report(parameters, parent_run_id, text)
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

    async def preview_standalone_import(
        self,
        parameters: DiscoverEditionParameters,
        markdown: str,
    ) -> dict[str, Any]:
        """Prévisualiser l'import d'une réponse ChatGPT Markdown existante.

        Aucune persistance au preview : permet à l'utilisateur de vérifier
        le contenu avant de confirmer.
        """
        digest = hashlib.sha256(markdown.encode()).hexdigest()
        manual_run_id = uuid5(
            NAMESPACE_URL,
            f"cti-discovery-manual-import:{parameters.edition_id}:{digest}",
        )
        return self._preview_report(parameters, manual_run_id, markdown)

    async def import_standalone_report(
        self,
        parameters: DiscoverEditionParameters,
        markdown: str,
        *,
        expected_sha256: str,
        actor_id: str,
    ) -> tuple[DiscoveryBatch, bool, UUID | None]:
        """Importer une réponse ChatGPT Markdown en tant que contribution autonome.

        Étapes:
        1. Refaire preview et vérifier expected_sha256
        2. Calculer manual_run_id et manual_request_hash
        3. Vérifier si un batch avec ce request_hash existe → retourner (batch, reused=True)
        4. Créer/obtenir le ModelRun synthétique manual-import
        5. Parser le rapport
        6. Enregistrer les diagnostics parser
        7. Transformer en DiscoveryBatch
        8. Déclencher la réconciliation cumulative (job asynchrone).
        9. Retourner (batch, reused=False, reconciliation_job_id)
        """
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

        # 1. Refaire preview et vérifier expected_sha256
        preview = self._preview_report(parameters, manual_run_id, markdown)
        if preview["sha256"] != expected_sha256:
            raise ValueError("Import preview no longer matches the confirmed report")

        # 2. Vérifier si un batch avec ce request_hash existe (idempotence)
        async with self._uow_factory() as uow:
            existing_batch = await uow.discovery_batches.get_by_request_hash(
                parameters.edition_id, manual_request_hash
            )
            if existing_batch is not None:
                return existing_batch, True, None

        # 3. Créer le ModelRun synthétique manual-import
        await self._output_archive.create_manual_research_output(
            manual_run_id,
            markdown.encode(),
            # Le hash de requête manuelle tient lieu d'empreinte d'entrée : il est
            # déterministe pour (édition, contenu) et satisfait l'invariant SHA-256.
            evidence_pack_hash=manual_request_hash,
            actor_id=actor_id,
        )

        # 4. Parser le rapport
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

        # 5. Enregistrer les diagnostics parser, comme pour une recherche ChatGPT.
        await self._record_parser_diagnostics(manual_run_id, parsed)

        # 6. Transformer en DiscoveryBatch
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

        # 7. Ajouter et committer. Une insertion concurrente du même Markdown
        # partage le même request_hash : on adopte le batch canonique.
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

        # 8. Regrouper éditorialement la nouvelle contribution. Ceci ne fait que
        # soumettre et dispatcher un job de réconciliation ASYNCHRONE : la
        # consolidation (fusion en sujets) n'est pas terminée quand cet appel
        # revient. Le job id est renvoyé pour que l'appelant puisse suivre son
        # achèvement au lieu de rafraîchir l'état trop tôt.
        reconciliation_job_id: UUID | None = None
        if self._after_persisted_batch is not None:
            job = await self._after_persisted_batch(
                batch, DiscoveryInputMode.MANUAL_IMPORT, actor_id
            )
            job_id = getattr(job, "id", None)
            if isinstance(job_id, UUID):
                reconciliation_job_id = job_id

        return batch, False, reconciliation_job_id

    async def _poll_background_research(
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
                return await self._completed_execution_from_archive(current)
            if current.status is ModelRunStatus.NEEDS_REVIEW:
                await self._wait_for_incomplete_review(current, context)
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
                    await self._wait_for_incomplete_review(execution.run, context)
                raise ModelGatewayError("Background research returned a non-terminal result")
            if execution.output_text:
                return execution
            return await self._completed_execution_from_archive(execution.run)

    @staticmethod
    async def _wait_for_incomplete_review(
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

    async def _completed_execution_from_archive(self, run: ModelRun) -> ModelExecution:
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


def register_discovery_jobs(registry: JobRegistry, service: DiscoveryService) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, DiscoverEditionParameters):
            raise TypeError("Invalid discovery parameters")
        try:
            batch = await service.discover_edition(parameters, context)
        except (ModelGatewayError, ReportParsingError) as exc:
            details = None
            if isinstance(exc, ReportParsingError):
                details = {
                    "phase": "local_parsing",
                    "research_model_run_id": (
                        str(exc.research_model_run_id)
                        if exc.research_model_run_id is not None
                        else None
                    ),
                    "correlation_id": get_correlation_id(),
                    "diagnostic_available": exc.research_model_run_id is not None,
                }
            else:
                details = {
                    "correlation_id": get_correlation_id(),
                }
            error_code = str(getattr(exc, "code", "research_failed"))
            if error_code == "bridge_unreachable":
                error_code = "bridge_unavailable"
            raise JobHandlerError(
                error_code,
                str(exc),
                transient=bool(getattr(exc, "retryable", False)),
                details=details,
            ) from exc
        return f"discovery-batch://{batch.id}"

    registry.register(
        DISCOVERY_JOB_KIND,
        DiscoverEditionParameters,
        handler,
        resume_after_worker_loss=True,
    )


def discovery_request_hash(parameters: DiscoverEditionParameters) -> str:
    value = parameters.model_dump(mode="json")
    for key in ("country_aliases", "languages", "keywords", "exclusions"):
        cleaned = [item.strip() for item in value[key] if item.strip()]
        value[key] = (
            sorted({item.casefold() for item in cleaned})
            if key in {"country_aliases", "languages"}
            else sorted(dict.fromkeys(cleaned))
        )
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def discovery_idempotency_key(parameters: DiscoverEditionParameters) -> str:
    return f"discover-edition:{parameters.edition_id}:{discovery_request_hash(parameters)}"


def _research_prompt(parameters: DiscoverEditionParameters) -> str:
    aliases: list[str] = []
    seen_aliases = {parameters.country.strip().casefold()}
    for value in parameters.country_aliases:
        alias = value.strip()
        fingerprint = alias.casefold()
        if alias and fingerprint not in seen_aliases:
            aliases.append(alias)
            seen_aliases.add(fingerprint)
    formatted_aliases = f" (alias : {', '.join(aliases)})" if aliases else ""
    languages: list[str] = []
    seen_languages: set[str] = set()
    for value in parameters.languages:
        language = value.strip()
        fingerprint = language.casefold()
        if language and fingerprint not in seen_languages:
            languages.append(language)
            seen_languages.add(fingerprint)
    observable_end = min(parameters.period_end, parameters.as_of_date)
    return f"""Mission : rechercher les publications CTI significatives concernant
{parameters.country}{formatted_aliases}.

Date de recherche : {parameters.as_of_date.isoformat()}
Période demandée : {parameters.period_start.isoformat()} au {parameters.period_end.isoformat()}
Période observable : {parameters.period_start.isoformat()} au {observable_end.isoformat()}
Langues de travail de l'édition : {", ".join(languages)}
Axe complémentaire : {parameters.complementary_axis}

La langue n'est jamais un critère de sélection. Recherche dans toutes les
langues et n'écarte aucune publication au motif qu'elle n'est pas rédigée dans
une langue de travail de l'édition. Couvre notamment l'anglais, le français,
l'espagnol, le portugais, l'allemand, l'italien, le néerlandais, le polonais,
l'ukrainien, le russe, le turc, l'arabe, le persan, l'hébreu, le chinois,
le japonais, le coréen, le vietnamien, l'indonésien, le thaï et l'hindi,
qui concentrent une part importante de la production CTI publique.

Conserve le titre exact de chaque publication dans sa langue d'origine, sans le
traduire ni le translittérer. Les champs de description que tu rédiges restent
en français.

Ne recherche pas de publication postérieure à la date de recherche.

Priorise les activités APT étatiques ou supposées étatiques et les publications
techniques comportant des IOC, des échantillons, des configurations, une chaîne
d’infection, des outils, des TTP ou des règles de détection.

Propose tous les sujets significatifs retrouvés. Il n’existe aucune limite ni
quota de sujets, de brèves ou d’articles approfondis. La sélection finale sera
effectuée par un analyste humain.

Regroupe dans un même SUBJECT les publications décrivant manifestement la même
campagne, le même incident ou la même recherche.

Une synthèse mensuelle ou trimestrielle peut être liée à plusieurs SUBJECT.
Ne fusionne pas des campagnes différentes uniquement parce qu’elles sont
mentionnées dans la même synthèse.

Chaque SUBJECT doit normalement comporter au moins une publication dans la
période observable. Les publications antérieures peuvent être ajoutées comme
rapport original, analyse indépendante ou contexte technique.

Limite cette phase à la sélection éditoriale. N’effectue pas encore l’analyse
exhaustive de la chaîne d’infection, des TTP, des outils ou de la victimologie.

Pour les IOC :

- signale uniquement les IOC explicitement visibles dans les pages consultées ;
- reproduis leurs valeurs exactes sans les corriger ni les compléter ;
- indique leur type lorsqu’il est identifiable ;
- distingue un total annoncé par l’éditeur des valeurs effectivement visibles ;
- n’estime jamais un nombre d’IOC ;
- utilise `unknown` si tu ne peux pas déterminer l’information ;
- utilise `none` seulement si la publication indique clairement qu’aucun IOC
  n’est fourni ou si son contenu visible permet de l’établir ;
- une URL normale de publication ou de navigation n’est pas un IOC ;
- un domaine d’éditeur ou de CDN n’est pas un IOC sauf s’il est explicitement
  présenté comme tel dans la source.

N’invente aucune URL, date, attribution, disponibilité d’artefact ou valeur
d’IOC.

Retourne uniquement du Markdown, sans bloc de code et sans texte avant le titre.
N’échappe pas les tirets des noms de champs.
N’insère pas de citation Markdown dans les champs de description.
Toutes les URL de référence doivent apparaître dans un bloc PUBLICATION.

# SUJETS CANDIDATS

## SUBJECT S1

title: <intitulé proposé>
presentation: <deux phrases neutres maximum>
actor-campaign: <acteur ou campagne explicitement rapporté, sinon unknown>
technical-potential: <entier de 0 à 4>
technical-reason: <raison en une phrase>
artifacts: <liste parmi ioc, samples, configurations, pcap, yara, suricata, none, unknown>
uncertainty: <une ou deux incertitudes courtes>

### PUBLICATION P1

title: <titre exact>
url: <URL HTTP(S) exacte>
publisher: <éditeur ou unknown>
published-at: <YYYY-MM-DD ou unknown>
role: <primary, independent, relay, aggregator ou unknown>
ioc-visibility: <none, declared, visible ou unknown>
visible-ioc-types: <liste des types visibles ou none/unknown>
visible-iocs: <jusqu’à 10 valeurs exactes explicitement visibles ou none/unknown>
publisher-ioc-count: <entier explicitement annoncé ou unknown>
ioc-note: <une phrase courte ou none>

### PUBLICATION P2

...

## SUBJECT S2

...

# LIMITES

<limites principales de la recherche et de l’accès aux sources>"""


def _source_profile_description(profile_id: str) -> str:
    profiles = {
        "iran-default": (
            "sources CTI primaires, CERT nationaux, chercheurs techniques indépendants, "
            "puis relais et agrégateurs explicitement étiquetés"
        )
    }
    return profiles.get(
        profile_id,
        "sources primaires et institutionnelles, corroborations techniques indépendantes, "
        "puis relais explicitement étiquetés",
    )


def _has_recovery_provenance(run: ModelRun, provenance: str) -> bool:
    recovery = (run.error_details or {}).get("recovery")
    return (
        run.status is ModelRunStatus.SUCCEEDED
        and isinstance(recovery, dict)
        and recovery.get("provenance") == provenance
    )

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
