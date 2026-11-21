"""Rebuild the exact, policy-checked context for an editorial discussion turn."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict, cast
from uuid import UUID

from cti_app.application.editorial_conversation_turns import (
    EditorialDiscussionContext,
    EditorialDiscussionObjective,
    EditorialParagraphEvidenceContext,
)
from cti_app.application.editorial_thread_common import decode_thread_request
from cti_app.application.editorial_threads import EditorialThreadApplicationError
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.editorial_workspace.registry import (
    bar_chart_insertion_preparation_ready,
    revision_target_path,
    table_insertion_preparation_ready,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
)
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    build_synthesis_access_policy,
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editorial_thread import (
    EditorialBindingState,
    EditorialThread,
    EditorialThreadBinding,
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadRecord,
)
from cti_app.domain.model_runs import EDITORIAL_DISCUSSION_PROMPT_ID
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import production_extraction_from_json
from cti_app.domain.production_relevance import (
    relevance_projection_from_json,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    extraction_evidence_refs_v1,
    production_synthesis_from_json,
)


class EditorialWorkspaceBlock(TypedDict, total=False):
    block_id: str
    kind: str
    parent_block_id: str | None
    position: int
    text: str
    revision_status: str
    evidence_refs: list[object]


class EditorialEvidenceReference(TypedDict):
    source_document_id: str
    kind: str
    evidence_key: str


class EditorialEvidenceEntry(TypedDict):
    handle: str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class EditorialDiscussionHistoryEntry:
    speaker: str
    text: str


@dataclass(frozen=True, slots=True)
class PreparedEditorialDiscussion:
    record: EditorialThreadRecord
    send_event: EditorialThreadEvent
    instruction: str
    context: EditorialDiscussionContext
    external_llm_allowed: bool
    objective: EditorialDiscussionObjective


@dataclass(frozen=True, slots=True)
class _EditorialDiscussionContextBase:
    section_title: str
    block_text: str
    adjacent_paragraphs: tuple[str, ...]
    admitted_evidence: tuple[dict[str, object], ...]
    access_policy: SynthesisAccessPolicyV1
    current_subject_tlp: TLP
    proposal_snapshot_id: UUID | None
    proposal_target_path: str | None
    proposal_operations: tuple[str, ...]
    proposal_before_hash: str | None
    proposal_base_sha256: str | None
    paragraph_evidence_context: EditorialParagraphEvidenceContext | None
    proposal_enrichment_context: Mapping[str, object] | None


class EditorialDiscussionContextBuilder:
    """Resolve, rebuild, and policy-check discussion context from canonical state."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialWorkspaceService,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace = workspace_service
        self._artifact_store = artifact_store

    async def prepare_job(self, job_id: UUID) -> PreparedEditorialDiscussion:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job(job_id)
            if job is None:
                raise EditorialThreadApplicationError("editorial_thread_job_not_found")
            record = await uow.editorial_threads.get(job.thread_id)
        if record is None or record.thread.model_conversation_id is None:
            raise EditorialThreadApplicationError("editorial_thread_not_found")
        send_event = next(
            (
                event
                for event in record.events
                if event.id == job.send_event_id
                and event.kind is EditorialThreadEventKind.USER_MESSAGE
            ),
            None,
        )
        if send_event is None or send_event.payload_blob_ref is None:
            raise EditorialThreadApplicationError("editorial_thread_message_missing")
        objective_value, instruction = await self._read_event_request(
            send_event, "editorial_thread_message_corrupt"
        )
        objective = EditorialDiscussionObjective(objective_value)
        context_base = await self._load_base(record.thread)
        history: list[EditorialDiscussionHistoryEntry] = []
        for event in record.events:
            if event.sequence >= send_event.sequence or event.payload_blob_ref is None:
                continue
            if event.kind not in {
                EditorialThreadEventKind.USER_MESSAGE,
                EditorialThreadEventKind.BRIDGE_REPLY,
            }:
                continue
            if event.kind is EditorialThreadEventKind.USER_MESSAGE:
                _prior_objective, prior_text = await self._read_event_request(
                    event, "editorial_context_unavailable"
                )
            else:
                prior_text = await self._read_event_text(event, "editorial_context_unavailable")
            history.append(
                EditorialDiscussionHistoryEntry(
                    "Utilisateur"
                    if event.kind is EditorialThreadEventKind.USER_MESSAGE
                    else "Bridge",
                    prior_text,
                )
            )
        context = self._compose_context(context_base, instruction, tuple(history))
        return PreparedEditorialDiscussion(
            record=record,
            send_event=send_event,
            instruction=instruction,
            context=context,
            external_llm_allowed=context.effective_tlp is not TLP.RED,
            objective=objective,
        )

    async def build(
        self,
        thread: EditorialThread,
        instruction: str,
        history: tuple[EditorialDiscussionHistoryEntry, ...],
    ) -> EditorialDiscussionContext:
        """Build context from the thread's current pinned occurrence and history."""
        base = await self._load_base(thread)
        return self._compose_context(base, instruction, history)

    async def build_manual(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> EditorialDiscussionContext:
        """Build local evidence validation context without creating a thread or model turn."""
        try:
            resolution = await self._workspace.resolve_occurrence(subject_id, occurrence_id)
        except Exception:
            raise EditorialThreadApplicationError("editorial_context_unavailable") from None
        if (
            resolution.entry is None
            or resolution.binding_state != EditorialBindingState.VALID.value
            or resolution.artifact_id is None
            or resolution.artifact_version is None
            or resolution.canonical_sha256 is None
        ):
            raise EditorialThreadApplicationError("editorial_context_unavailable")
        origin = EditorialThreadBinding(
            subject_id=subject_id,
            production_run_id=resolution.record.production_run_id,
            occurrence_id=occurrence_id,
            artifact_id=resolution.artifact_id,
            artifact_version=resolution.artifact_version,
            canonical_sha256=resolution.canonical_sha256,
        )
        base = await self._load_base(origin, require_external_llm=False)
        return self._compose_context(base, "", ())

    async def _load_base(
        self,
        thread: EditorialThread | EditorialThreadBinding,
        *,
        require_external_llm: bool = True,
    ) -> _EditorialDiscussionContextBase:
        origin = thread.origin if isinstance(thread, EditorialThread) else thread
        try:
            resolution = await self._workspace.resolve_occurrence(
                origin.subject_id, origin.occurrence_id
            )
            snapshot_view = await self._workspace.snapshot(origin.subject_id)
        except Exception:
            raise EditorialThreadApplicationError("editorial_context_unavailable") from None
        stage = getattr(resolution.entry, "source_stage", None)
        stage_value = getattr(stage, "value", stage)
        if stage_value == "editorial_enrichment":
            pinned_artifact_id = snapshot_view.get("enrichment_artifact_id")
            pinned_version = snapshot_view.get("enrichment_version")
            pinned_sha256 = snapshot_view.get("enrichment_canonical_sha256")
        else:
            pinned_artifact_id = snapshot_view.get("synthesis_artifact_id")
            pinned_version = snapshot_view.get("synthesis_version")
            pinned_sha256 = snapshot_view.get("synthesis_canonical_sha256")
        if (
            resolution.entry is None
            or resolution.binding_state != EditorialBindingState.VALID.value
            or resolution.record.production_run_id != origin.production_run_id
            or resolution.entry.block_id != origin.occurrence_id
            or resolution.artifact_id is None
            or resolution.artifact_version is None
            or resolution.canonical_sha256 is None
            or snapshot_view.get("production_run_id") != str(origin.production_run_id)
            or pinned_artifact_id != str(resolution.artifact_id)
            or pinned_version != resolution.artifact_version
            or pinned_sha256 != resolution.canonical_sha256
        ):
            raise EditorialThreadApplicationError("editorial_context_unavailable")

        blocks = _workspace_blocks(snapshot_view.get("blocks"))
        raw_target = next(
            (
                block
                for block in blocks
                if isinstance(block, dict)
                and str(block.get("block_id")) == str(origin.occurrence_id)
            ),
            None,
        )
        if not isinstance(raw_target, dict):
            raise EditorialThreadApplicationError("editorial_context_unavailable")
        target = cast(EditorialWorkspaceBlock, raw_target)
        if target.get("revision_status") != "ready":
            raise EditorialThreadApplicationError("editorial_context_unavailable")
        target_text = target.get("text")
        if not isinstance(target_text, str):
            raise EditorialThreadApplicationError("editorial_context_unavailable")
        section_title = _section_title(target, blocks)
        adjacent = _adjacent_paragraphs(target, blocks)
        raw_snapshot_id = snapshot_view.get("snapshot_id")
        proposal_snapshot_id = None
        if isinstance(raw_snapshot_id, str):
            try:
                proposal_snapshot_id = UUID(raw_snapshot_id)
            except ValueError:
                pass
        raw_operations = target.get("allowed_operations", [])
        offered_operations = (
            {value for value in raw_operations if isinstance(value, str)}
            if isinstance(raw_operations, list)
            else set()
        )
        proposal_target_path = (
            revision_target_path(resolution.entry) if resolution.entry is not None else None
        )
        raw_before_hash = target.get("before_hash")
        proposal_before_hash = raw_before_hash if isinstance(raw_before_hash, str) else None

        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(origin.subject_id)
            production_snapshot = (
                await uow.production_input_snapshots.get_by_run(run.id) if run else None
            )
            synthesis_artifact = (
                await uow.production_artifacts.get(resolution.artifact_id)
                if (
                    run is not None
                    and stage_value == "synthesis"
                    and (
                        (target.get("kind") == "title" and "rewrite_title" in offered_operations)
                        or (
                            target.get("kind") in {"paragraph", "lead_paragraph"}
                            and (
                                "rewrite_paragraph" in offered_operations
                                or "create_table" in offered_operations
                            )
                        )
                    )
                )
                else None
            )
            extraction_artifact = (
                await uow.production_artifacts.get_current(
                    run.id, ProductionArtifactStage.EXTRACTION.value
                )
                if run
                else None
            )
            projection_artifact = (
                await uow.production_artifacts.get_current(
                    run.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
                )
                if run
                else None
            )
            enrichment_artifact = (
                await uow.production_artifacts.get_current(
                    run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
                )
                if run is not None and "create_table" in offered_operations
                else None
            )
            current_subject = await uow.subjects.get(origin.subject_id)
            if (
                run is None
                or run.id != origin.production_run_id
                or production_snapshot is None
                or extraction_artifact is None
                or current_subject is None
                or extraction_artifact.status is not ProductionArtifactStatus.VERIFIED
                or extraction_artifact.canonical_blob_id is None
            ):
                raise EditorialThreadApplicationError("editorial_context_unavailable")
            try:
                extraction = production_extraction_from_json(
                    await self._artifact_store.read_json(extraction_artifact.canonical_blob_id)
                )
                if (
                    extraction.subject_id != origin.subject_id
                    or extraction.production_input_hash != production_snapshot.input_hash
                    or extraction_artifact.production_run_id != run.id
                ):
                    raise ValueError("extraction_lineage_invalid")
                projection = None
                if projection_artifact is not None:
                    if (
                        projection_artifact.status is not ProductionArtifactStatus.VERIFIED
                        or projection_artifact.canonical_blob_id is None
                    ):
                        raise ValueError("projection_unavailable")
                    projection = relevance_projection_from_json(
                        await self._artifact_store.read_json(projection_artifact.canonical_blob_id)
                    )
                    validate_relevance_projection_lineage(
                        projection,
                        extraction,
                        extraction_hash=canonical_extraction_hash(extraction),
                    )
                evidence_pack = build_synthesis_evidence_pack(
                    production_snapshot, extraction, projection
                )
                access_policy = await build_synthesis_access_policy(
                    production_snapshot,
                    extraction,
                    uow.source_documents,
                    uow.source_collections,
                )
                subject_tlp_tightened = tuple(TLP).index(current_subject.tlp) > tuple(TLP).index(
                    production_snapshot.subject_tlp
                )
            except Exception:
                raise EditorialThreadApplicationError("editorial_context_unavailable") from None

        if require_external_llm and (
            not access_policy.external_llm_allowed
            or access_policy.effective_tlp is TLP.RED
            or subject_tlp_tightened
        ):
            raise EditorialThreadApplicationError("external_llm_blocked")

        evidence_by_handle: dict[str, EditorialEvidenceEntry] = {}
        for raw_entry in (
            *evidence_pack.narrative_evidence,
            *evidence_pack.technical_evidence,
            *evidence_pack.reserve_evidence,
        ):
            handle = raw_entry.get("handle")
            if isinstance(handle, str):
                evidence_by_handle[handle] = {
                    "handle": handle,
                    "payload": _object_mapping(raw_entry),
                }

        admitted: list[EditorialEvidenceEntry] = []
        admitted_refs: list[ExtractionEvidenceRefV1] = []
        references: Sequence[ExtractionEvidenceRefV1]
        if target.get("kind") == "title":
            references = extraction_evidence_refs_v1(extraction)
        else:
            raw_refs = target.get("evidence_refs", [])
            if not isinstance(raw_refs, list):
                raise EditorialThreadApplicationError("editorial_context_unavailable")
            parsed_references: list[ExtractionEvidenceRefV1] = []
            for raw_ref in raw_refs:
                reference_data = _evidence_reference(raw_ref)
                if reference_data is None:
                    raise EditorialThreadApplicationError("editorial_context_unavailable")
                try:
                    parsed_references.append(
                        ExtractionEvidenceRefV1(
                            source_document_id=UUID(reference_data["source_document_id"]),
                            kind=EvidenceKind(reference_data["kind"]),
                            evidence_key=reference_data["evidence_key"],
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    raise EditorialThreadApplicationError("editorial_context_unavailable") from None
            references = tuple(parsed_references)
        for reference in references:
            handle = evidence_pack.handle_for(reference)
            if handle is not None and handle in evidence_by_handle:
                admitted_refs.append(reference)
                admitted.append(evidence_by_handle[handle])

        paragraph_evidence_context: EditorialParagraphEvidenceContext | None = None
        kind = target.get("kind")
        can_validate_title = kind == "title" and "rewrite_title" in offered_operations
        can_validate_paragraph = (
            kind in {"paragraph", "lead_paragraph"} and "rewrite_paragraph" in offered_operations
        )
        if stage_value == "synthesis" and (can_validate_title or can_validate_paragraph):
            if (
                synthesis_artifact is not None
                and synthesis_artifact.status is ProductionArtifactStatus.VERIFIED
                and synthesis_artifact.canonical_blob_id is not None
                and synthesis_artifact.id == resolution.artifact_id
                and synthesis_artifact.version == resolution.artifact_version
                and synthesis_artifact.production_run_id == origin.production_run_id
            ):
                try:
                    base_artifact_content = await self._artifact_store.read_json(
                        synthesis_artifact.canonical_blob_id
                    )
                    paragraph_evidence_context = EditorialParagraphEvidenceContext(
                        evidence_pack=evidence_pack,
                        extraction=extraction,
                        snapshot=production_snapshot,
                        base_artifact=synthesis_artifact,
                        evidence_refs=tuple(admitted_refs),
                        evidence_handles=tuple(
                            dict.fromkeys(
                                handle
                                for reference in admitted_refs
                                if (handle := evidence_pack.handle_for(reference)) is not None
                            )
                        ),
                        publication_kind=resolution.record.publication_kind,
                    )
                    # Check canonical artifact bytes against the resolution used for the fence.
                    canonical_digest = hashlib.sha256(
                        self._artifact_store.canonical_json_bytes(base_artifact_content)
                    ).hexdigest()
                    if (
                        canonical_digest != resolution.canonical_sha256
                        or paragraph_evidence_context.base_artifact.stage
                        is not ProductionArtifactStage.SYNTHESIS
                    ):
                        paragraph_evidence_context = None
                except Exception:
                    paragraph_evidence_context = None

        enrichment_proposal_context: Mapping[str, object] | None = None
        table_admitted: list[EditorialEvidenceEntry] | None = None
        can_check_table = (
            stage_value == "synthesis"
            and kind in {"paragraph", "lead_paragraph"}
            and bool({"create_table", "create_bar_chart"} & offered_operations)
            and synthesis_artifact is not None
            and synthesis_artifact.canonical_blob_id is not None
            and enrichment_artifact is not None
            and enrichment_artifact.canonical_blob_id is not None
        )
        if can_check_table:
            assert synthesis_artifact is not None
            assert synthesis_artifact.canonical_blob_id is not None
            assert enrichment_artifact is not None
            assert enrichment_artifact.canonical_blob_id is not None
            try:
                synthesis_bytes = await self._artifact_store.read_bytes(
                    synthesis_artifact.canonical_blob_id
                )
                synthesis_payload = json.loads(synthesis_bytes)
                if not isinstance(synthesis_payload, dict):
                    raise ValueError("synthesis_payload_invalid")
                synthesis = production_synthesis_from_json(synthesis_payload)
                synthesis_read_sha256 = hashlib.sha256(synthesis_bytes).hexdigest()
                enrichment_bytes = await self._artifact_store.read_bytes(
                    enrichment_artifact.canonical_blob_id
                )
                enrichment_payload = json.loads(enrichment_bytes)
                if not isinstance(enrichment_payload, dict):
                    raise ValueError("enrichment_payload_invalid")
                enrichment_read_sha256 = hashlib.sha256(enrichment_bytes).hexdigest()
                enrichment_pack = build_editorial_enrichment_evidence_pack(
                    production_snapshot,
                    extraction,
                    synthesis,
                    projection,
                    workspace_record=resolution.record,
                )
                anchor_registered = (
                    enrichment_pack.registered_anchor_ids is not None
                    and origin.occurrence_id in enrichment_pack.registered_anchor_ids
                )
                if table_insertion_preparation_ready(
                    record=resolution.record,
                    entry=resolution.entry,
                    synthesis_artifact=synthesis_artifact,
                    synthesis_read_sha256=synthesis_read_sha256,
                    enrichment_artifact=enrichment_artifact,
                    enrichment_read_sha256=enrichment_read_sha256,
                    anchor_registered=anchor_registered,
                ):
                    table_records = (
                        *enrichment_pack.narrative_evidence,
                        *enrichment_pack.technical_evidence,
                    )
                    table_admitted = [
                        {"handle": str(item["handle"]), "payload": _object_mapping(item)}
                        for item in table_records
                        if isinstance(item.get("handle"), str)
                    ]
                    enrichment_proposal_context = {
                        "anchor_block_id": str(origin.occurrence_id),
                        "target_path": proposal_target_path,
                        "base_artifact_id": str(enrichment_artifact.id),
                        "base_artifact_version": enrichment_artifact.version,
                        "base_input_hash": enrichment_artifact.input_hash,
                        "base_canonical_sha256": resolution.record.enrichment_canonical_sha256,
                        "base_content": _object_mapping(enrichment_payload),
                        "evidence_pack": enrichment_pack,
                        "extraction": extraction,
                        "synthesis": synthesis,
                        "figure_catalog": (),
                    }
            except Exception:
                enrichment_proposal_context = None
                table_admitted = None

        if enrichment_proposal_context is not None and table_admitted is not None:
            admitted = table_admitted

        proposal_operations = _supported_proposal_operations(
            stage=stage_value,
            block_kind=str(target.get("kind", "")),
            offered_operations=offered_operations,
            paragraph_validation_ready=paragraph_evidence_context is not None,
            table_validation_ready=enrichment_proposal_context is not None,
            bar_chart_validation_ready=(
                enrichment_proposal_context is not None
                and "create_bar_chart" in offered_operations
                and bar_chart_insertion_preparation_ready(
                    record=resolution.record,
                    entry=resolution.entry,
                    synthesis_artifact=synthesis_artifact,
                    synthesis_read_sha256=synthesis_read_sha256,
                    enrichment_artifact=enrichment_artifact,
                    enrichment_read_sha256=enrichment_read_sha256,
                    anchor_registered=anchor_registered,
                )
            ),
        )

        return _EditorialDiscussionContextBase(
            section_title=section_title,
            block_text=target_text,
            adjacent_paragraphs=adjacent,
            admitted_evidence=tuple(entry["payload"] for entry in admitted),
            access_policy=access_policy,
            current_subject_tlp=current_subject.tlp,
            proposal_snapshot_id=proposal_snapshot_id,
            proposal_target_path=proposal_target_path,
            proposal_operations=proposal_operations,
            proposal_before_hash=proposal_before_hash,
            proposal_base_sha256=(
                resolution.record.enrichment_canonical_sha256
                if enrichment_proposal_context is not None
                else pinned_sha256
            ),
            paragraph_evidence_context=paragraph_evidence_context,
            proposal_enrichment_context=enrichment_proposal_context,
        )

    @staticmethod
    def _compose_context(
        base: _EditorialDiscussionContextBase,
        instruction: str,
        history: tuple[EditorialDiscussionHistoryEntry, ...],
    ) -> EditorialDiscussionContext:
        section_title = base.section_title
        target_text = base.block_text
        adjacent = base.adjacent_paragraphs
        access_policy = base.access_policy
        current_subject_tlp = base.current_subject_tlp
        admitted_payloads = list(base.admitted_evidence)
        evidence_hash = hashlib.sha256(
            json.dumps(
                admitted_payloads,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        effective_tlp = max(
            (current_subject_tlp, access_policy.effective_tlp),
            key=lambda item: tuple(TLP).index(item),
        )
        context_identity = {
            "prompt_id": EDITORIAL_DISCUSSION_PROMPT_ID,
            "section_title": section_title,
            "block_text": target_text,
            "adjacent_paragraphs": adjacent,
            "admitted_evidence": admitted_payloads,
            "history": [(item.speaker, item.text) for item in history],
            "evidence_pack_hash": evidence_hash,
            "access_policy_hash": synthesis_access_policy_hash(access_policy),
            "current_subject_tlp": current_subject_tlp.value,
            "effective_tlp": effective_tlp.value,
            "proposal_snapshot_id": (
                str(base.proposal_snapshot_id) if base.proposal_snapshot_id is not None else None
            ),
            "proposal_target_path": base.proposal_target_path,
            "proposal_operations": base.proposal_operations,
            "proposal_before_hash": base.proposal_before_hash,
            "proposal_base_sha256": base.proposal_base_sha256,
            "paragraph_evidence_handles": (
                base.paragraph_evidence_context.evidence_handles
                if base.paragraph_evidence_context is not None
                else ()
            ),
            "proposal_enrichment_anchor": (
                base.proposal_enrichment_context.get("anchor_block_id")
                if base.proposal_enrichment_context is not None
                else None
            ),
            "proposal_enrichment_base_sha256": (
                base.proposal_enrichment_context.get("base_canonical_sha256")
                if base.proposal_enrichment_context is not None
                else None
            ),
        }
        context_sha256 = hashlib.sha256(
            json.dumps(
                context_identity,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return EditorialDiscussionContext(
            section_title=section_title,
            block_text=target_text,
            adjacent_paragraphs=adjacent,
            admitted_evidence=tuple(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                for item in admitted_payloads
            ),
            history=tuple((item.speaker, item.text) for item in history),
            evidence_pack_hash=evidence_hash,
            context_sha256=context_sha256,
            effective_tlp=effective_tlp,
            instruction=instruction,
            proposal_snapshot_id=base.proposal_snapshot_id,
            proposal_target_path=base.proposal_target_path,
            proposal_operations=base.proposal_operations,
            proposal_before_hash=base.proposal_before_hash,
            proposal_base_sha256=base.proposal_base_sha256,
            paragraph_evidence_context=base.paragraph_evidence_context,
            proposal_enrichment_context=base.proposal_enrichment_context,
        )

    async def _read_event_request(
        self, event: EditorialThreadEvent, error_code: str
    ) -> tuple[str, str]:
        body = await self._read_event_body(event, error_code)
        try:
            return decode_thread_request(body)
        except (UnicodeDecodeError, ValueError):
            raise EditorialThreadApplicationError(error_code) from None

    async def _read_event_text(self, event: EditorialThreadEvent, error_code: str) -> str:
        body = await self._read_event_body(event, error_code)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise EditorialThreadApplicationError(error_code) from None

    async def _read_event_body(self, event: EditorialThreadEvent, error_code: str) -> bytes:
        if event.payload_blob_ref is None or event.payload_sha256 is None:
            raise EditorialThreadApplicationError(error_code)
        body = await self._artifact_store.read_bytes(event.payload_blob_ref)
        if hashlib.sha256(body).hexdigest() != event.payload_sha256:
            raise EditorialThreadApplicationError(error_code)
        return body


def _workspace_blocks(value: object) -> list[EditorialWorkspaceBlock | object]:
    if not isinstance(value, list):
        raise EditorialThreadApplicationError("editorial_context_unavailable")
    return [
        cast(EditorialWorkspaceBlock, _object_mapping(item)) if isinstance(item, Mapping) else item
        for item in value
    ]


def _supported_proposal_operations(
    *,
    stage: str | None,
    block_kind: str,
    offered_operations: set[str],
    paragraph_validation_ready: bool,
    table_validation_ready: bool = False,
    bar_chart_validation_ready: bool = False,
) -> tuple[str, ...]:
    if stage != "synthesis":
        return ()
    if block_kind == "title" and "rewrite_title" in offered_operations:
        return ("rewrite_title",)
    if block_kind in {"paragraph", "lead_paragraph"}:
        operations: list[str] = []
        if "rewrite_paragraph" in offered_operations and paragraph_validation_ready:
            operations.append("rewrite_paragraph")
        if "create_table" in offered_operations and table_validation_ready:
            operations.append("create_table")
        if "create_bar_chart" in offered_operations and bar_chart_validation_ready:
            operations.append("create_bar_chart")
        return tuple(operations)
    return ()


def _object_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _evidence_reference(value: object) -> EditorialEvidenceReference | None:
    if not isinstance(value, Mapping):
        return None
    raw = _object_mapping(value)
    source_document_id = raw.get("source_document_id")
    kind = raw.get("kind")
    evidence_key = raw.get("evidence_key")
    if source_document_id is None or kind is None or evidence_key is None:
        return None
    return {
        "source_document_id": str(source_document_id),
        "kind": str(kind),
        "evidence_key": str(evidence_key),
    }


def _section_title(
    target: EditorialWorkspaceBlock, blocks: Sequence[EditorialWorkspaceBlock | object]
) -> str:
    parent_value = target.get("parent_block_id")
    parent = str(parent_value) if parent_value else None
    if parent is not None:
        for block in blocks:
            if (
                isinstance(block, dict)
                and str(block.get("block_id")) == parent
                and block.get("kind") == "section_heading"
                and isinstance(block.get("text"), str)
            ):
                return str(block["text"])
    target_position = target.get("position")
    candidates = [
        block
        for block in blocks
        if isinstance(block, dict)
        and block.get("kind") == "section_heading"
        and isinstance(block.get("text"), str)
        and isinstance(block.get("position"), int)
        and isinstance(target_position, int)
        and block["position"] <= target_position
    ]
    if candidates:
        latest = max(candidates, key=lambda item: int(item["position"]))
        return str(latest["text"])
    for block in blocks:
        if isinstance(block, dict) and block.get("kind") == "title":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return "Document"


def _adjacent_paragraphs(
    target: EditorialWorkspaceBlock, blocks: Sequence[EditorialWorkspaceBlock | object]
) -> tuple[str, ...]:
    parent = target.get("parent_block_id")
    position = target.get("position")
    if not isinstance(position, int):
        return ()
    siblings = sorted(
        (
            block
            for block in blocks
            if isinstance(block, dict)
            and block.get("kind") == "paragraph"
            and block.get("parent_block_id") == parent
            and isinstance(block.get("position"), int)
            and isinstance(block.get("text"), str)
            and block.get("block_id") != target.get("block_id")
        ),
        key=lambda item: int(item["position"]),
    )
    before = [item for item in siblings if int(item["position"]) < position]
    after = [item for item in siblings if int(item["position"]) > position]
    selected = ([before[-1]] if before else []) + ([after[0]] if after else [])
    return tuple(str(item["text"]) for item in selected)
