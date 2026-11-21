"""Read-only, subject-scoped history for persisted editorial proposals."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.editorial_proposal import EditorialProposal
from cti_app.domain.production import ProductionArtifactStage

MAX_EDITORIAL_HISTORY_PAGE_SIZE = 100

_OPERATION_LABELS = {
    "rewrite_title": "Réécriture du titre",
    "rewrite_lead": "Réécriture du chapeau",
    "rewrite_paragraph": "Réécriture du paragraphe",
    "insert_paragraph": "Insertion d'un paragraphe",
    "delete_paragraph": "Suppression d'un paragraphe",
    "rewrite_section": "Réécriture d'une section",
    "insert_section": "Insertion d'une section",
    "delete_section": "Suppression d'une section",
    "merge_sections": "Fusion de sections",
    "split_section": "Division d'une section",
    "rename_section": "Renommage d'une section",
    "create_table": "Insertion d'un tableau",
    "rewrite_table": "Modification d'un tableau",
    "delete_table": "Suppression d'un tableau",
    "create_bar_chart": "Insertion d'un graphique en barres",
    "create_chart": "Insertion d'un graphique",
    "rewrite_chart": "Modification d'un graphique",
    "delete_chart": "Suppression d'un graphique",
    "create_diagram": "Insertion d'un diagramme",
    "rewrite_diagram": "Modification d'un diagramme",
    "delete_diagram": "Suppression d'un diagramme",
    "create_source_figure": "Insertion de figures",
    "rewrite_source_figure": "Modification d'une figure",
    "replace_source_figure": "Remplacement d'une figure",
    "delete_source_figure": "Retrait d'une figure",
    "update_visual_caption": "Modification d'une légende",
    "move_enrichment_element": "Déplacement d'un élément",
    "reorder_enrichment_elements": "Réordonnancement des éléments",
}


class EditorialHistoryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class EditorialHistoryItem:
    proposal_id: UUID
    subject_id: UUID
    occurrence_id: UUID
    state: str
    origin: str
    operation: str
    operation_label: str
    actor_id: UUID
    actor_display: str
    created_at: datetime
    updated_at: datetime
    canonical_result_artifact_id: UUID | None
    canonical_result_version: int | None
    canonical_result_stage: str | None
    thread_id: UUID | None
    thread_url: str | None


@dataclass(frozen=True, slots=True)
class EditorialHistoryPage:
    items: tuple[EditorialHistoryItem, ...]
    next_cursor: str | None
    limit: int


def _encode_cursor(proposal: EditorialProposal) -> str:
    value = f"{proposal.created_at.isoformat()}|{proposal.id}"
    return base64.urlsafe_b64encode(value.encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    if not cursor or len(cursor) > 256:
        raise EditorialHistoryError("editorial_history_cursor_invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = base64.b64decode(padded, altchars=b"-_", validate=True).decode("ascii")
        timestamp, proposal_id = value.split("|", maxsplit=1)
        created_at = datetime.fromisoformat(timestamp)
        parsed_id = UUID(proposal_id)
    except (ValueError, UnicodeDecodeError) as error:
        raise EditorialHistoryError("editorial_history_cursor_invalid") from error
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise EditorialHistoryError("editorial_history_cursor_invalid")
    return created_at, parsed_id


class EditorialHistoryService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def read(
        self,
        subject_id: UUID,
        *,
        occurrence_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> EditorialHistoryPage:
        if type(limit) is not int or limit < 1 or limit > MAX_EDITORIAL_HISTORY_PAGE_SIZE:
            raise EditorialHistoryError("editorial_history_limit_invalid")
        before = _decode_cursor(cursor)
        async with self._uow_factory() as uow:
            if await uow.subjects.get(subject_id) is None:
                raise EditorialHistoryError("subject_not_found")
            proposals = await uow.editorial_proposals.list_for_subject(
                subject_id,
                occurrence_id=occurrence_id,
                before=before,
                limit=limit + 1,
            )
            has_more = len(proposals) > limit
            page_proposals = tuple(proposals[:limit])
            projected_items: list[EditorialHistoryItem] = []
            for proposal in page_proposals:
                projected_items.append(await self._project_item(uow, subject_id, proposal))
            items = tuple(projected_items)
        return EditorialHistoryPage(
            items=items,
            next_cursor=_encode_cursor(page_proposals[-1]) if has_more and page_proposals else None,
            limit=limit,
        )

    @staticmethod
    async def _project_item(
        uow: UnitOfWork,
        subject_id: UUID,
        proposal: EditorialProposal,
    ) -> EditorialHistoryItem:
        result_artifact_id = proposal.accepted_artifact_id
        result_version: int | None = None
        result_stage: str | None = None
        if result_artifact_id is not None:
            artifact = await uow.production_artifacts.get(result_artifact_id)
            if (
                artifact is not None
                and artifact.subject_id == subject_id
                and artifact.production_run_id == proposal.production_run_id
            ):
                result_version = artifact.version
                result_stage = (
                    artifact.stage.value
                    if isinstance(artifact.stage, ProductionArtifactStage)
                    else str(artifact.stage)
                )
            else:
                result_artifact_id = None
        thread_url = (
            f"/api/subjects/{subject_id}/editor/threads/{proposal.occurrence_id}"
            if proposal.origin.value == "bridge_draft" and proposal.thread_id is not None
            else None
        )
        actor_id = proposal.actor_id
        return EditorialHistoryItem(
            proposal_id=proposal.id,
            subject_id=proposal.subject_id,
            occurrence_id=proposal.occurrence_id,
            state=proposal.state.value,
            origin=proposal.origin.value,
            operation=proposal.operation,
            operation_label=_OPERATION_LABELS.get(proposal.operation, "Modification éditoriale"),
            actor_id=actor_id,
            actor_display=f"Opérateur · {actor_id.hex[:8]}",
            created_at=proposal.created_at,
            updated_at=proposal.updated_at,
            canonical_result_artifact_id=result_artifact_id,
            canonical_result_version=result_version,
            canonical_result_stage=result_stage,
            thread_id=proposal.thread_id if thread_url is not None else None,
            thread_url=thread_url,
        )
