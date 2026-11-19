"""Rebuild the exact, policy-checked context for an editorial discussion turn."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict, cast
from uuid import UUID

from cti_app.application.editorial_conversation_turns import EditorialDiscussionContext
from cti_app.application.editorial_threads import EditorialThreadApplicationError
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
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
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadRecord,
)
from cti_app.domain.model_runs import EDITORIAL_DISCUSSION_PROMPT_ID
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.production_extraction import production_extraction_from_json
from cti_app.domain.production_relevance import (
    relevance_projection_from_json,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1


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


@dataclass(frozen=True, slots=True)
class _EditorialDiscussionContextBase:
    section_title: str
    block_text: str
    adjacent_paragraphs: tuple[str, ...]
    admitted_evidence: tuple[dict[str, object], ...]
    access_policy: SynthesisAccessPolicyV1
    current_subject_tlp: TLP


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
        instruction = await self._read_event_text(send_event, "editorial_thread_message_corrupt")
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

    async def _load_base(self, thread: EditorialThread) -> _EditorialDiscussionContextBase:
        origin = thread.origin
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

        async with self._uow_factory() as uow:
            run = await uow.production_runs.get_current_for_subject(origin.subject_id)
            production_snapshot = (
                await uow.production_input_snapshots.get_by_run(run.id) if run else None
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

        if (
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
        raw_refs = target.get("evidence_refs", [])
        if not isinstance(raw_refs, list):
            raise EditorialThreadApplicationError("editorial_context_unavailable")
        for raw_ref in raw_refs:
            reference_data = _evidence_reference(raw_ref)
            if reference_data is None:
                raise EditorialThreadApplicationError("editorial_context_unavailable")
            try:
                reference = ExtractionEvidenceRefV1(
                    source_document_id=UUID(reference_data["source_document_id"]),
                    kind=EvidenceKind(reference_data["kind"]),
                    evidence_key=reference_data["evidence_key"],
                )
            except (KeyError, TypeError, ValueError):
                raise EditorialThreadApplicationError("editorial_context_unavailable") from None
            handle = evidence_pack.handle_for(reference)
            if handle is not None and handle in evidence_by_handle:
                admitted.append(evidence_by_handle[handle])

        return _EditorialDiscussionContextBase(
            section_title=section_title,
            block_text=target_text,
            adjacent_paragraphs=adjacent,
            admitted_evidence=tuple(entry["payload"] for entry in admitted),
            access_policy=access_policy,
            current_subject_tlp=current_subject.tlp,
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
        )

    async def _read_event_text(self, event: EditorialThreadEvent, error_code: str) -> str:
        if event.payload_blob_ref is None or event.payload_sha256 is None:
            raise EditorialThreadApplicationError(error_code)
        body = await self._artifact_store.read_bytes(event.payload_blob_ref)
        if hashlib.sha256(body).hexdigest() != event.payload_sha256:
            raise EditorialThreadApplicationError(error_code)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise EditorialThreadApplicationError(error_code) from None


def _workspace_blocks(value: object) -> list[EditorialWorkspaceBlock | object]:
    if not isinstance(value, list):
        raise EditorialThreadApplicationError("editorial_context_unavailable")
    return [
        cast(EditorialWorkspaceBlock, _object_mapping(item)) if isinstance(item, Mapping) else item
        for item in value
    ]


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
