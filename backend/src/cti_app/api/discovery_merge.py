from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.domain.discovery_cumulative import DiscoveryMergeRun

merge_runs_router = APIRouter(
    prefix="/api/editions/{edition_id}/merge-runs", tags=["discovery-merge-review"]
)


class MergeHandleLabelView(BaseModel):
    handle: str
    title: str
    summary: str
    source_urls: list[str]


class MergeRunView(BaseModel):
    id: UUID
    edition_id: UUID
    parent_snapshot_id: UUID | None
    intake_id: UUID | None
    planner_kind: str
    validation_status: str
    review_reasons: list[str]
    warnings: list[str]
    plan: dict[str, object] | None
    projected_diff: list[dict[str, object]]
    # Populated only on single-run read (costs a snapshot + batch load); list view omits it.
    handle_labels: dict[str, MergeHandleLabelView] = Field(default_factory=dict)
    supersedes_merge_run_id: UUID | None
    created_at: datetime


@merge_runs_router.get("", response_model=list[MergeRunView])
async def list_merge_runs(edition_id: UUID, request: Request) -> list[MergeRunView]:
    service: CumulativeDiscoveryService = request.app.state.cumulative_discovery_service
    return [_merge_run_view(run) for run in await service.list_merge_runs(edition_id)]


@merge_runs_router.get("/{run_id}", response_model=MergeRunView)
async def read_merge_run(edition_id: UUID, run_id: UUID, request: Request) -> MergeRunView:
    service: CumulativeDiscoveryService = request.app.state.cumulative_discovery_service
    try:
        view = _merge_run_view(await service.get_merge_run(edition_id, run_id))
        labels = await service.describe_merge_handles(edition_id, run_id)
        view.handle_labels = {
            handle: MergeHandleLabelView(
                handle=label.handle,
                title=label.title,
                summary=label.summary,
                source_urls=list(label.source_urls),
            )
            for handle, label in labels.items()
        }
        return view
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "merge_run_not_found", "message": str(exc)}
        ) from exc


def _merge_run_view(run: DiscoveryMergeRun) -> MergeRunView:
    groups = []
    if isinstance(run.plan_payload, dict):
        raw_groups = run.plan_payload.get("groups")
        if isinstance(raw_groups, list):
            for index, group in enumerate(raw_groups):
                if not isinstance(group, dict):
                    continue
                groups.append(
                    {
                        "group_index": index,
                        "existing_subject_handles": group.get("existing_subject_handles", []),
                        "incoming_candidate_handles": group.get("incoming_candidate_handles", []),
                        "disposition": group.get("disposition"),
                        "flags": group.get("flags", []),
                        # Needed so a reviewer can judge why the planner proposed this grouping.
                        "confidence": group.get("confidence"),
                        "rationale": group.get("rationale", ""),
                        "evidence": group.get("evidence", {}),
                    }
                )
    return MergeRunView(
        id=run.id,
        edition_id=run.edition_id,
        parent_snapshot_id=run.parent_snapshot_id,
        intake_id=run.intake_id,
        planner_kind=run.planner_kind.value,
        validation_status=run.validation_status.value,
        review_reasons=list(run.review_reasons),
        warnings=list(run.warnings),
        plan=run.plan_payload,
        projected_diff=groups,
        supersedes_merge_run_id=run.supersedes_merge_run_id,
        created_at=run.created_at,
    )
