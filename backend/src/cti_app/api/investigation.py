"""HTTP API for launching and reading exact-synthesis investigations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID, uuid5

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.analyst_handoff import (
    AnalystHandoffPolicy,
    AnalystPostSynthesisService,
    analyst_handoff_policy_from_sources,
    loop_budget_from_settings,
)
from cti_app.application.identity import IdentityProvider
from cti_app.application.investigation_composition import (
    InvestigationComposition,
    InvestigationCompositionFactory,
)
from cti_app.application.investigation_governance import (
    EnvelopeDecisionConflictError,
    EnvelopeNotFoundError,
    EnvelopeVersionConflictError,
    InvestigationActor,
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
    SampleAssociationNotFoundError,
    SampleQualificationEvidenceError,
    SampleQualificationPermissionError,
    SampleQualificationService,
)
from cti_app.application.investigation_jobs import (
    INVESTIGATION_RUN_AGGREGATE_TYPE,
    InvestigationRunParameters,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.application.jobs import DuplicateJobError
from cti_app.application.research_context_builder import USER_REQUEST_ACTION_PREFIX
from cti_app.domain.analyst_opinion import AnalystOpinionV1
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    FindingAssessment,
    InvestigationBudget,
    QualificationActorKind,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import InvestigationWorkingState, StopReason
from cti_app.domain.jobs import INVESTIGATION_RUN_JOB_KIND, Job, JobStatus
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.logging import get_correlation_id

router = APIRouter(prefix="/api", tags=["investigations"])
_SHA256 = r"^[0-9a-f]{64}$"
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_USER_REQUEST_NAMESPACE = UUID("92e23e22-b681-49c9-87dd-a7fd13398c05")


class InvestigationStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str = Field(pattern=_SHA256)


class InvestigationActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class EnvelopeDisclosureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tlp: str | None = Field(default=None, max_length=32)
    do_not_submit: bool | None = None
    external_llm_allowed: bool | None = None


class EnvelopeDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = Field(default=None, ge=1)
    allowed_providers: tuple[str, ...] = Field(default=(), max_length=8)
    budget_limits: dict[str, int] = Field(default_factory=dict, max_length=16)
    acquire_hits_allowed: bool | None = None
    max_acquire_volume: int | None = Field(default=None, ge=0)
    disclosure: EnvelopeDisclosureRequest = Field(default_factory=EnvelopeDisclosureRequest)
    reason: str = Field(min_length=1, max_length=4_000)
    correlation_id: str = Field(min_length=1, max_length=80)


class EnvelopeView(BaseModel):
    investigation_id: UUID
    approved: bool
    version: int
    allowed_providers: list[str]
    available_providers: list[str]
    acquire_hits_allowed: bool
    max_acquire_volume: int
    budget: dict[str, BudgetDimensionView]
    disclosure: dict[str, Any]
    approval_decision_id: UUID | None = None
    approval_actor_id: str | None = None
    approved_at: datetime | None = None


class InvestigationRequestTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(hash|ioc|claim_id)$")
    value: str = Field(min_length=1, max_length=256)


class InvestigationUserRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2_000)
    target: InvestigationRequestTarget | None = None
    expected_version: int | None = Field(default=None, ge=1)


class SampleQualificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    qualification: SampleQualification
    reason: str = Field(min_length=1, max_length=4_000)
    evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    correlation_id: str = Field(min_length=1, max_length=80)


class EnvelopeRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=4_000)
    correlation_id: str = Field(min_length=1, max_length=80)


class QualificationChangeView(BaseModel):
    from_qualification: SampleQualification
    to_qualification: SampleQualification
    actor_kind: QualificationActorKind
    actor_id: str
    reason: str
    changed_at: datetime


class AgendaItemView(BaseModel):
    question: str
    potential: float
    rank: int


class HypothesisView(BaseModel):
    id: str
    statement: str
    status: str
    evidence_refs: list[UUID]
    finding_refs: list[UUID]


class FindingView(BaseModel):
    id: UUID
    version: int
    kind: str
    assessment: FindingAssessment
    novelty: str | None
    statement: str
    scope: str
    limitations: list[str]
    observation_refs: list[UUID]
    claim_refs: list[str]
    created_at: datetime


class ObservationView(BaseModel):
    id: UUID
    kind: str
    sha256: str
    source: str
    source_version: str
    observed_at: datetime
    object_type: str
    object_id: str
    limitations: list[str]
    invocation_ref: UUID | None


class SampleAssociationView(BaseModel):
    id: UUID
    sample_sha256: str
    origin: str
    qualification: SampleQualification
    related_sample_sha256: list[str]
    version: int


class SampleDetailView(SampleAssociationView):
    changes: list[QualificationChangeView]


class OpinionBlockView(BaseModel):
    type: str
    text: str
    finding_refs: list[UUID]
    evidence_refs: list[UUID]
    qualification: str
    certainty: float


class OpinionDraftView(BaseModel):
    id: UUID
    version: int
    status: str
    content_sha256: str
    blocks: list[OpinionBlockView]
    unresolved_points: list[str]
    created_at: datetime


class BudgetDimensionView(BaseModel):
    limit: int | None
    consumed: int
    remaining: int | None


class BlockedDecisionView(BaseModel):
    code: str
    tool_name: str | None


class InvestigationView(BaseModel):
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    status: AnalystInvestigationStatus
    version: int
    stop_reason: StopReason | None
    agenda: list[AgendaItemView]
    hypotheses: list[HypothesisView]
    findings: list[FindingView]
    observations: list[ObservationView]
    sample_associations: list[SampleAssociationView]
    opinion_draft: OpinionDraftView | None
    budget: dict[str, BudgetDimensionView]
    blocked_decisions: list[BlockedDecisionView]
    available_tools: list[str]
    unavailable_tools: list[str]
    envelope_approved: bool
    allowed_providers: list[str]
    acquire_hits_allowed: bool


class InvestigationActionResponse(BaseModel):
    investigation: InvestigationView
    job_id: UUID | None
    idempotent_replay: bool


class UserRequestResponse(BaseModel):
    investigation: InvestigationView
    request_id: UUID
    job_id: UUID | None
    idempotent_replay: bool


class SampleQualificationResponse(BaseModel):
    association: SampleDetailView
    idempotent_replay: bool


class InvestigationSummaryView(BaseModel):
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    status: AnalystInvestigationStatus
    version: int
    stop_reason: StopReason | None
    updated_at: datetime


@router.post(
    "/subjects/{subject_id}/investigations",
    response_model=InvestigationActionResponse,
)
async def start_investigation(
    subject_id: UUID,
    payload: InvestigationStartRequest,
    request: Request,
) -> InvestigationActionResponse:
    uow_factory = _uow_factory(request)
    composition_factory = _composition_factory(request)
    identity = await _identity(request)
    await _ensure_handoff_if_missing(request, subject_id, payload)
    async with uow_factory() as uow:
        run = await uow.production_runs.get(payload.production_run_id)
        artifact = await uow.production_artifacts.get(payload.synthesis_artifact_id)
        if run is None or run.subject_id != subject_id:
            raise _http_error(404, "production_run_not_found")
        if artifact is None:
            raise _http_error(404, "synthesis_artifact_not_found")
        _validate_synthesis_link(subject_id, payload, run, artifact)
        investigation = await uow.analyst_investigations.get_for_run(run.id)
        if investigation is None or investigation.synthesis_artifact_id != artifact.id:
            raise _http_error(409, "investigation_handoff_missing")
        try:
            snapshot = await composition_factory.article_snapshots.resolve(artifact.id)
        except (ValueError, KeyError) as exc:
            raise _http_error(409, "synthesis_not_verified") from exc
        if (
            snapshot.subject_id != subject_id
            or snapshot.production_run_id != run.id
            or snapshot.synthesis_sha256 != payload.synthesis_sha256
        ):
            raise _http_error(409, "synthesis_identity_mismatch")

        composition = composition_factory.build(uow, investigation.id)
        envelope = await uow.investigation_envelopes.get(investigation.id)
        try:
            state = await composition.service.start(
                run=run,
                synthesis=artifact,
                extraction_artifacts=(),
                extraction_items=(),
                policy=AnalystHandoffPolicy(
                    tlp=snapshot.tlp,
                    do_not_submit=snapshot.do_not_submit,
                    external_llm_allowed=snapshot.external_llm_allowed,
                ),
                envelope=envelope,
                uow=uow,
            )
            job, replay = await _submit_run_job(
                request,
                uow,
                investigation.id,
                generation=1,
                request_key_sha256=_hash_key(
                    f"start:{run.id}:{artifact.id}:{payload.synthesis_sha256}"
                ),
                actor_id=identity.actor_id,
            )
            view = await _investigation_view(
                uow,
                investigation.id,
                state=state,
                composition=composition,
            )
            await uow.commit()
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "investigation_version_conflict") from exc
        except ValueError as exc:
            raise _http_error(409, "investigation_start_conflict", str(exc)) from exc
    await _dispatch(request, job)
    return InvestigationActionResponse(
        investigation=view,
        job_id=job.id,
        idempotent_replay=replay,
    )


async def _ensure_handoff_if_missing(
    request: Request,
    subject_id: UUID,
    payload: InvestigationStartRequest,
) -> None:
    """Create the canonical post-synthesis handoff before resolving article context.

    The article snapshot adapter intentionally requires an existing input pack. The
    HTTP start route is also the entry point that may create that pack, so this
    model-free bootstrap runs in its own committed transaction first.
    """
    uow_factory = _uow_factory(request)
    factory = _composition_factory(request)
    async with uow_factory() as uow:
        run = await uow.production_runs.get(payload.production_run_id)
        artifact = await uow.production_artifacts.get(payload.synthesis_artifact_id)
        if run is None or run.subject_id != subject_id:
            raise _http_error(404, "production_run_not_found")
        if artifact is None:
            raise _http_error(404, "synthesis_artifact_not_found")
        _validate_synthesis_link(subject_id, payload, run, artifact)
        existing = await uow.analyst_investigations.get_for_run(run.id)
        if existing is not None:
            if existing.synthesis_artifact_id != artifact.id:
                raise _http_error(409, "investigation_handoff_mismatch")
            return
        if artifact.canonical_blob_id is None:
            raise _http_error(409, "synthesis_not_verified")
        try:
            synthesis_payload = await factory.artifact_store.read_json(artifact.canonical_blob_id)
        except (KeyError, ValueError) as exc:
            raise _http_error(409, "synthesis_not_verified") from exc
        synthesis_sha256 = hashlib.sha256(
            factory.artifact_store.canonical_json_bytes(synthesis_payload)
        ).hexdigest()
        if (
            synthesis_payload.get("subject_id") != str(subject_id)
            or synthesis_sha256 != payload.synthesis_sha256
        ):
            raise _http_error(409, "synthesis_identity_mismatch")
        input_snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        if input_snapshot is None:
            raise _http_error(409, "production_input_snapshot_missing")
        policy = analyst_handoff_policy_from_sources(input_snapshot.core_sources)
        if input_snapshot.subject_tlp.value.upper() == "RED":
            policy = AnalystHandoffPolicy(
                tlp=input_snapshot.subject_tlp.value,
                do_not_submit=True,
                external_llm_allowed=False,
            )
        else:
            policy = AnalystHandoffPolicy(
                tlp=input_snapshot.subject_tlp.value,
                do_not_submit=policy.do_not_submit,
                external_llm_allowed=policy.external_llm_allowed
                and bool(input_snapshot.core_sources),
            )
        handoff = AnalystPostSynthesisService(
            uow_factory,
            factory.artifact_store,
            loop_budget_factory=lambda: loop_budget_from_settings(factory.settings),
        )
        try:
            await handoff.ensure_for_verified_synthesis(
                run=run,
                synthesis=artifact,
                extraction_artifacts=(),
                extraction_items=(),
                policy=policy,
                uow=uow,
            )
            await uow.commit()
        except ValueError as exc:
            raise _http_error(409, "investigation_handoff_conflict", str(exc)) from exc


@router.get("/investigations/{investigation_id}", response_model=InvestigationView)
async def get_investigation(investigation_id: UUID, request: Request) -> InvestigationView:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        return await _investigation_view(
            uow,
            investigation_id,
            composition=composition,
        )


@router.get("/investigations/{investigation_id}/envelope", response_model=EnvelopeView)
async def get_investigation_envelope(
    investigation_id: UUID,
    request: Request,
) -> EnvelopeView:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        current = await uow.investigation_envelopes.get(investigation_id)
        settings = _composition_factory(request).settings
        if current is None:
            budget = _configured_budget(settings)
            allowed_providers: tuple[str, ...] = ()
            acquire_hits_allowed = False
            disclosure = _source_disclosure(snapshot)
            version = 0
            decision_id = None
            actor_id = None
            approved_at = None
        else:
            budget = current.budget
            allowed_providers = current.allowed_providers
            acquire_hits_allowed = current.acquire_hits_allowed
            disclosure = current.disclosure
            version = current.version
            decision_id = current.approval_decision_id
            actor_id = current.approval_actor_id
            approved_at = current.approved_at
        return _envelope_view(
            investigation_id=investigation_id,
            budget=budget,
            approved=current is not None,
            version=version,
            allowed_providers=allowed_providers,
            available_providers=_available_providers(composition.available_tools),
            acquire_hits_allowed=acquire_hits_allowed,
            disclosure=disclosure,
            approval_decision_id=decision_id,
            approval_actor_id=actor_id,
            approved_at=approved_at,
        )


@router.post("/investigations/{investigation_id}/envelope/approve", response_model=EnvelopeView)
async def approve_investigation_envelope(
    investigation_id: UUID,
    payload: EnvelopeDecisionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> EnvelopeView:
    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        available = _available_providers(composition.available_tools)
        budget = _budget_from_request(
            payload,
            defaults=_configured_budget(_composition_factory(request).settings),
        )
        providers = _requested_providers(payload.allowed_providers, available)
        disclosure = _disclosure_from_request(payload.disclosure, snapshot)
    proposal = InvestigationEnvelopeProposal(
        investigation_id=investigation_id,
        allowed_providers=providers,
        budget=budget,
        acquire_hits_allowed=bool(payload.acquire_hits_allowed),
        disclosure=disclosure,
    )
    try:
        await InvestigationEnvelopeService(_uow_factory(request)).approve(
            proposal,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
            expected_version=payload.expected_version,
        )
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "investigation_not_found") from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc
    return await _current_envelope_view(request, investigation_id)


@router.post("/investigations/{investigation_id}/envelope/extend", response_model=EnvelopeView)
async def extend_investigation_envelope(
    investigation_id: UUID,
    payload: EnvelopeDecisionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> EnvelopeView:
    identity = await _identity(request)
    service = InvestigationEnvelopeService(_uow_factory(request))
    current = await service.current(investigation_id)
    if current is None:
        raise _http_error(404, "envelope_not_found")
    if payload.expected_version is None:
        raise _http_error(422, "expected_version_required")
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        providers = _requested_providers(
            payload.allowed_providers or current.allowed_providers,
            _available_providers(composition.available_tools),
        )
        budget = _budget_from_request(
            payload,
            defaults=current.budget,
            acquire_default=current.acquire_hits_allowed,
        )
        acquire = (
            current.acquire_hits_allowed
            if payload.acquire_hits_allowed is None
            else payload.acquire_hits_allowed
        )
        disclosure = _disclosure_from_request(
            payload.disclosure, snapshot, current=current.disclosure
        )
    try:
        await service.extend(
            investigation_id,
            expected_version=payload.expected_version,
            allowed_providers=providers,
            budget=budget,
            acquire_hits_allowed=acquire,
            disclosure=disclosure,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
        )
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "envelope_not_found") from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc
    return await _current_envelope_view(request, investigation_id)


@router.post("/investigations/{investigation_id}/envelope/revoke", response_model=EnvelopeView)
async def revoke_investigation_envelope(
    investigation_id: UUID,
    payload: EnvelopeRevokeRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> EnvelopeView:
    identity = await _identity(request)
    try:
        await InvestigationEnvelopeService(_uow_factory(request)).revoke(
            investigation_id,
            expected_version=payload.expected_version,
            actor_id=identity.actor_id,
            reason=payload.reason,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
        )
    except EnvelopeNotFoundError as exc:
        raise _http_error(404, "envelope_not_found") from exc
    except (EnvelopeVersionConflictError, EnvelopeDecisionConflictError) as exc:
        raise _http_error(409, "envelope_conflict", str(exc)) from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_envelope", str(exc)) from exc
    return await _current_envelope_view(request, investigation_id)


@router.get(
    "/investigations/{investigation_id}/samples",
    response_model=list[SampleDetailView],
)
async def list_investigation_samples(
    investigation_id: UUID,
    request: Request,
) -> list[SampleDetailView]:
    async with _uow_factory(request)() as uow:
        if await uow.analyst_investigations.get(investigation_id) is None:
            raise _http_error(404, "investigation_not_found")
        associations = await uow.sample_associations.list_for_investigation(investigation_id)
        return [_sample_detail(item) for item in associations]


@router.post(
    "/investigations/{investigation_id}/samples/{association_id}/qualify",
    response_model=SampleQualificationResponse,
)
async def qualify_investigation_sample(
    investigation_id: UUID,
    association_id: UUID,
    payload: SampleQualificationRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> SampleQualificationResponse:
    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        if await uow.analyst_investigations.get(investigation_id) is None:
            raise _http_error(404, "investigation_not_found")
        association = await uow.sample_associations.get(association_id)
        if association is None or association.investigation_id != investigation_id:
            raise _http_error(404, "sample_association_not_found")
        if association.version != payload.expected_version:
            if association.qualification is not payload.qualification:
                raise _http_error(409, "stale_sample_association_version")
            return SampleQualificationResponse(
                association=_sample_detail(association), idempotent_replay=True
            )
    try:
        qualified = await SampleQualificationService(_uow_factory(request)).qualify(
            association_id,
            payload.qualification,
            InvestigationActor(
                actor_id=identity.actor_id,
                kind=QualificationActorKind.HUMAN,
            ),
            payload.reason,
            payload.evidence_refs,
            correlation_id=_decision_correlation(payload.correlation_id, idempotency_key),
            expected_version=payload.expected_version,
        )
    except SampleAssociationNotFoundError as exc:
        raise _http_error(404, "sample_association_not_found") from exc
    except SampleQualificationPermissionError as exc:
        raise _http_error(403, "human_qualification_required", str(exc)) from exc
    except SampleQualificationEvidenceError as exc:
        raise _http_error(422, "invalid_qualification_evidence", str(exc)) from exc
    except VersionConflictError as exc:
        raise _http_error(409, "stale_sample_association_version") from exc
    except ValueError as exc:
        raise _http_error(422, "invalid_qualification", str(exc)) from exc
    return SampleQualificationResponse(
        association=_sample_detail(qualified), idempotent_replay=False
    )


@router.post(
    "/investigations/{investigation_id}/requests",
    response_model=UserRequestResponse,
)
async def request_targeted_investigation(
    investigation_id: UUID,
    payload: InvestigationUserRequestBody,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> UserRequestResponse:
    key_hash = _hash_key(idempotency_key)
    target = payload.target.model_dump(mode="json") if payload.target else None
    if target and target["kind"] == "hash" and not re.fullmatch(_SHA256, target["value"]):
        raise _http_error(422, "invalid_request_target")
    request_fingerprint = hashlib.sha256(
        json.dumps(
            {"text": payload.text, "target": target},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    marker_prefix = f"api:user-request:{key_hash}:"
    request_id = uuid5(_USER_REQUEST_NAMESPACE, f"{investigation_id}:{key_hash}")
    marker = f"{marker_prefix}{request_fingerprint}:{request_id}"
    job: Job | None = None
    replay = False
    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        investigation, state = await _require_working_state(uow, investigation_id)
        previous = next(
            (item for item in state.recent_actions if item.startswith(marker_prefix)), None
        )
        if previous is not None:
            if previous != marker:
                raise _http_error(409, "idempotency_key_reused")
            replay = True
        else:
            if payload.expected_version is not None and state.version != payload.expected_version:
                raise _http_error(409, "stale_investigation_version")
            user_request = {
                "request_id": str(request_id),
                "text": payload.text,
                "target": target,
            }
            action = USER_REQUEST_ACTION_PREFIX + json.dumps(
                user_request,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            updated = replace(
                state,
                version=state.version + 1,
                recent_actions=(*state.recent_actions, action, marker),
            )
            await uow.investigation_states.save_if_version(updated, expected_version=state.version)
            state = updated
        jobs = await uow.jobs.list_for_aggregate(
            INVESTIGATION_RUN_AGGREGATE_TYPE,
            investigation_id,
            kind=INVESTIGATION_RUN_JOB_KIND,
        )
        active = _active_job(jobs)
        if active is not None:
            job = active
        elif replay:
            job = next(
                (
                    item
                    for item in jobs
                    if item.input_parameters.get("request_key_sha256") == key_hash
                ),
                None,
            )
        elif investigation.status in {
            AnalystInvestigationStatus.CANCELLED,
            AnalystInvestigationStatus.FAILED,
        }:
            raise _http_error(409, "investigation_cannot_resume")
        else:
            generation = (
                max(
                    (int(item.input_parameters.get("run_generation", 0)) for item in jobs),
                    default=0,
                )
                + 1
            )
            composition = _composition_factory(request).build(uow, investigation_id)
            if state.stop_reason is not None or investigation.status in {
                AnalystInvestigationStatus.AWAITING_REVIEW,
                AnalystInvestigationStatus.COMPLETED,
                AnalystInvestigationStatus.EXHAUSTED,
            }:
                try:
                    state = await composition.service.resume(investigation_id)
                except (VersionConflictError, InvestigationStateVersionConflict) as exc:
                    raise _http_error(409, "stale_investigation_version") from exc
            job, _ = await _submit_run_job(
                request,
                uow,
                investigation_id,
                generation=generation,
                request_key_sha256=key_hash,
                actor_id=identity.actor_id,
            )
        composition = _composition_factory(request).build(uow, investigation_id)
        view = await _investigation_view(
            uow, investigation_id, state=state, composition=composition
        )
        await uow.commit()
    if job is not None and job.status is JobStatus.QUEUED and not replay:
        await _dispatch(request, job)
    return UserRequestResponse(
        investigation=view,
        request_id=request_id,
        job_id=job.id if job else None,
        idempotent_replay=replay,
    )


@router.post(
    "/investigations/{investigation_id}/resume",
    response_model=InvestigationActionResponse,
)
async def resume_investigation(
    investigation_id: UUID,
    payload: InvestigationActionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> InvestigationActionResponse:
    identity = await _identity(request)
    key_hash = _hash_key(idempotency_key)
    async with _uow_factory(request)() as uow:
        _investigation, state = await _require_working_state(uow, investigation_id)
        replay_generation = _find_resume_replay(state, key_hash)
        if replay_generation is not None:
            job = await uow.jobs.get_by_idempotency_key(
                _run_idempotency_key(investigation_id, replay_generation)
            )
            if job is None:
                raise _http_error(409, "idempotency_record_incomplete")
            composition = _composition_factory(request).build(uow, investigation_id)
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            return InvestigationActionResponse(
                investigation=view,
                job_id=job.id,
                idempotent_replay=True,
            )
        if state.version != payload.expected_version:
            raise _http_error(409, "stale_investigation_version")
        jobs = await uow.jobs.list_for_aggregate(
            INVESTIGATION_RUN_AGGREGATE_TYPE,
            investigation_id,
            kind=INVESTIGATION_RUN_JOB_KIND,
        )
        if _active_job(jobs) is not None:
            raise _http_error(409, "investigation_job_already_running")
        generation = (
            max(
                (int(job.input_parameters.get("run_generation", 0)) for job in jobs),
                default=0,
            )
            + 1
        )
        composition = _composition_factory(request).build(uow, investigation_id)
        try:
            state = await composition.service.resume(investigation_id)
            marker = f"api:resume:{key_hash}:{generation}"
            state = await _save_idempotency_marker(uow, state, marker)
            job, replay = await _submit_run_job(
                request,
                uow,
                investigation_id,
                generation=generation,
                request_key_sha256=key_hash,
                actor_id=identity.actor_id,
            )
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            await uow.commit()
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_investigation_version") from exc
        except ValueError as exc:
            raise _http_error(409, "investigation_resume_conflict", str(exc)) from exc
    await _dispatch(request, job)
    return InvestigationActionResponse(
        investigation=view,
        job_id=job.id,
        idempotent_replay=replay,
    )


@router.post(
    "/investigations/{investigation_id}/interrupt",
    response_model=InvestigationActionResponse,
)
async def interrupt_investigation(
    investigation_id: UUID,
    payload: InvestigationActionRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> InvestigationActionResponse:
    identity = await _identity(request)
    key_hash = _hash_key(idempotency_key)
    active_job: Job | None = None
    async with _uow_factory(request)() as uow:
        _investigation, state = await _require_working_state(uow, investigation_id)
        replay_marker = f"api:interrupt:{key_hash}:"
        if any(action.startswith(replay_marker) for action in state.recent_actions):
            jobs = await uow.jobs.list_for_aggregate(
                INVESTIGATION_RUN_AGGREGATE_TYPE,
                investigation_id,
                kind=INVESTIGATION_RUN_JOB_KIND,
            )
            active_job = _active_job(jobs)
            composition = _composition_factory(request).build(uow, investigation_id)
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            return InvestigationActionResponse(
                investigation=view,
                job_id=active_job.id if active_job else None,
                idempotent_replay=True,
            )
        if state.version != payload.expected_version:
            raise _http_error(409, "stale_investigation_version")
        jobs = await uow.jobs.list_for_aggregate(
            INVESTIGATION_RUN_AGGREGATE_TYPE,
            investigation_id,
            kind=INVESTIGATION_RUN_JOB_KIND,
        )
        active_job = _active_job(jobs)
        composition = _composition_factory(request).build(uow, investigation_id)
        try:
            state = await composition.service.interrupt(investigation_id)
            state = await _save_idempotency_marker(
                uow,
                state,
                f"api:interrupt:{key_hash}:{identity.actor_id[:128]}",
            )
            view = await _investigation_view(
                uow,
                investigation_id,
                state=state,
                composition=composition,
            )
            await uow.commit()
        except (VersionConflictError, InvestigationStateVersionConflict) as exc:
            raise _http_error(409, "stale_investigation_version") from exc
        except ValueError as exc:
            raise _http_error(409, "investigation_interrupt_conflict", str(exc)) from exc
    if active_job is not None:
        await request.app.state.job_service.cancel(active_job.id, actor_id=identity.actor_id)
    return InvestigationActionResponse(
        investigation=view,
        job_id=active_job.id if active_job else None,
        idempotent_replay=False,
    )


@router.get(
    "/subjects/{subject_id}/investigations",
    response_model=list[InvestigationSummaryView],
)
async def list_subject_investigations(
    subject_id: UUID,
    request: Request,
) -> list[InvestigationSummaryView]:
    async with _uow_factory(request)() as uow:
        runs = await uow.production_runs.list_for_subject(subject_id)
        summaries: list[InvestigationSummaryView] = []
        for run in runs:
            investigation = await uow.analyst_investigations.get_for_run(run.id)
            if investigation is None:
                continue
            state = await uow.investigation_states.get(investigation.id)
            if state is None:
                continue
            try:
                snapshot = await _composition_factory(request).article_snapshots.resolve(
                    investigation.synthesis_artifact_id
                )
            except (KeyError, ValueError):
                continue
            if snapshot.subject_id != subject_id or snapshot.production_run_id != run.id:
                continue
            summaries.append(
                InvestigationSummaryView(
                    id=investigation.id,
                    subject_id=investigation.subject_id,
                    production_run_id=investigation.production_run_id,
                    synthesis_artifact_id=investigation.synthesis_artifact_id,
                    synthesis_sha256=snapshot.synthesis_sha256,
                    status=investigation.status,
                    version=state.version,
                    stop_reason=state.stop_reason,
                    updated_at=investigation.updated_at,
                )
            )
        return summaries


def _validate_synthesis_link(
    subject_id: UUID,
    payload: InvestigationStartRequest,
    run: ProductionRun,
    artifact: ProductionArtifact,
) -> None:
    if (
        run.id != payload.production_run_id
        or artifact.production_run_id != run.id
        or artifact.subject_id != subject_id
        or artifact.stage is not ProductionArtifactStage.SYNTHESIS
        or artifact.status is not ProductionArtifactStatus.VERIFIED
    ):
        raise _http_error(409, "synthesis_not_verified")


def _tlp_level(value: str | None) -> TLPLevel | None:
    if value is None:
        return None
    normalized = value.strip().upper().removeprefix("TLP:")
    if normalized == "AMBER+STRICT":
        normalized = "AMBER"
    try:
        return TLPLevel(normalized.lower())
    except ValueError:
        return None


async def _submit_run_job(
    request: Request,
    uow: Any,
    investigation_id: UUID,
    *,
    generation: int,
    request_key_sha256: str,
    actor_id: str,
) -> tuple[Job, bool]:
    parameters = InvestigationRunParameters(
        investigation_id=investigation_id,
        run_generation=generation,
        request_key_sha256=request_key_sha256,
    )
    idempotency_key = _run_idempotency_key(investigation_id, generation)
    job_service = request.app.state.job_service
    try:
        job = await job_service.submit_in_uow(
            uow,
            kind=INVESTIGATION_RUN_JOB_KIND,
            aggregate_type=INVESTIGATION_RUN_AGGREGATE_TYPE,
            aggregate_id=investigation_id,
            idempotency_key=idempotency_key,
            correlation_id=get_correlation_id(),
            input_parameters=parameters.model_dump(mode="json"),
            max_attempts=3,
            actor_id=actor_id,
        )
        return job, False
    except DuplicateJobError as exc:
        existing = await uow.jobs.get(exc.existing_job_id)
        if existing is None:
            raise _http_error(409, "investigation_job_idempotency_conflict") from exc
        if (
            existing.aggregate_id != investigation_id
            or existing.kind != INVESTIGATION_RUN_JOB_KIND
            or existing.input_parameters.get("run_generation") != generation
            or existing.input_parameters.get("request_key_sha256") != request_key_sha256
        ):
            raise _http_error(409, "investigation_job_idempotency_conflict") from exc
        return existing, True


def _run_idempotency_key(investigation_id: UUID, generation: int) -> str:
    return f"investigation:{investigation_id}:run:{generation}"


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _find_resume_replay(state: InvestigationWorkingState, key_hash: str) -> int | None:
    prefix = f"api:resume:{key_hash}:"
    for action in reversed(state.recent_actions):
        if action.startswith(prefix):
            suffix = action[len(prefix) :]
            return int(suffix) if suffix.isdigit() else None
    return None


async def _save_idempotency_marker(
    uow: Any,
    state: InvestigationWorkingState,
    marker: str,
) -> InvestigationWorkingState:
    if marker in state.recent_actions:
        return state
    updated = replace(
        state,
        version=state.version + 1,
        recent_actions=(*state.recent_actions, marker),
    )
    await uow.investigation_states.save_if_version(updated, expected_version=state.version)
    return updated


def _active_job(jobs: list[Job] | tuple[Job, ...]) -> Job | None:
    return next(
        (job for job in jobs if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}),
        None,
    )


async def _require_working_state(
    uow: Any,
    investigation_id: UUID,
) -> tuple[AnalystInvestigation, InvestigationWorkingState]:
    investigation = await uow.analyst_investigations.get(investigation_id)
    if investigation is None:
        raise _http_error(404, "investigation_not_found")
    state = await uow.investigation_states.get(investigation_id)
    if state is None:
        raise _http_error(409, "investigation_not_started")
    return investigation, state


async def _investigation_view(
    uow: Any,
    investigation_id: UUID,
    *,
    composition: InvestigationComposition,
    state: InvestigationWorkingState | None = None,
) -> InvestigationView:
    investigation, current_state = await _require_working_state(uow, investigation_id)
    state = state or current_state
    envelope = await uow.investigation_envelopes.get(investigation_id)
    snapshot = await composition.tool_dependencies.article_snapshots.resolve(
        investigation.synthesis_artifact_id
    )
    all_findings = await uow.investigation_findings.list_for_investigation(investigation_id)
    superseded_ids = {item.supersedes for item in all_findings if item.supersedes is not None}
    findings = tuple(item for item in all_findings if item.id not in superseded_ids)
    observations = await uow.evidence_observations.list_for_investigation(investigation_id)
    associations = await uow.sample_associations.list_for_investigation(investigation_id)
    opinion = await uow.analyst_opinions.get(uuid5(_OPINION_NAMESPACE, str(investigation_id)))
    effective_budget = (
        envelope.budget
        if envelope is not None
        else InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: investigation.budget.max_cycles,
                BudgetDimension.VT_UNITS: 0,
                BudgetDimension.SHODAN_CREDITS: 0,
                BudgetDimension.DOWNLOADS: 0,
            }
        )
    )
    budget = {
        dimension.value: BudgetDimensionView(
            limit=effective_budget.limits.get(dimension),
            consumed=effective_budget.consumed[dimension],
            remaining=effective_budget.remaining[dimension],
        )
        for dimension in BudgetDimension
    }
    blocked = _blocked_decisions(state)
    return InvestigationView(
        id=investigation.id,
        subject_id=investigation.subject_id,
        production_run_id=investigation.production_run_id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256=snapshot.synthesis_sha256,
        status=investigation.status,
        version=state.version,
        stop_reason=state.stop_reason,
        agenda=[
            AgendaItemView(question=item.question, potential=item.potential, rank=item.rank)
            for item in state.agenda.items
        ],
        hypotheses=[
            HypothesisView(
                id=item.id,
                statement=item.statement,
                status=item.status.value,
                evidence_refs=list(item.evidence_refs),
                finding_refs=list(item.finding_refs),
            )
            for item in state.hypotheses
        ],
        findings=[
            FindingView(
                id=item.id,
                version=item.version,
                kind=item.kind.value,
                assessment=item.assessment,
                novelty=item.novelty.value if item.novelty else None,
                statement=item.statement,
                scope=item.scope,
                limitations=list(item.limitations),
                observation_refs=list(item.observation_refs),
                claim_refs=list(item.claim_refs),
                created_at=item.created_at,
            )
            for item in findings
        ],
        observations=[
            ObservationView(
                id=item.id,
                kind=item.kind.value,
                sha256=item.sha256,
                source=item.source,
                source_version=item.source_version,
                observed_at=item.observed_at,
                object_type=item.object_type,
                object_id=item.object_id,
                limitations=list(item.limitations),
                invocation_ref=item.invocation_ref,
            )
            for item in observations
        ],
        sample_associations=[
            SampleAssociationView(
                id=item.id,
                sample_sha256=item.sample_sha256,
                origin=item.origin.value,
                qualification=item.qualification,
                related_sample_sha256=list(item.related_sample_sha256),
                version=item.version,
            )
            for item in associations
        ],
        opinion_draft=_opinion_view(opinion),
        budget=budget,
        blocked_decisions=blocked,
        available_tools=list(composition.available_tools),
        unavailable_tools=list(composition.unavailable_tools),
        envelope_approved=envelope is not None,
        allowed_providers=list(envelope.allowed_providers) if envelope is not None else [],
        acquire_hits_allowed=envelope.acquire_hits_allowed if envelope is not None else False,
    )


def _opinion_view(opinion: AnalystOpinionV1 | None) -> OpinionDraftView | None:
    if opinion is None:
        return None
    return OpinionDraftView(
        id=opinion.id,
        version=opinion.version,
        status=opinion.status.value,
        content_sha256=opinion.content_sha256,
        blocks=[
            OpinionBlockView(
                type=block.block_type.value,
                text=block.text,
                finding_refs=list(block.finding_refs),
                evidence_refs=list(block.evidence_refs),
                qualification=block.qualification.value,
                certainty=block.certainty,
            )
            for block in opinion.blocks
        ],
        unresolved_points=list(opinion.unresolved_points),
        created_at=opinion.created_at,
    )


def _blocked_decisions(state: InvestigationWorkingState) -> list[BlockedDecisionView]:
    blocked_statuses = {
        ToolInvocationStatus.POLICY_DENIED.value,
        ToolInvocationStatus.NEEDS_REVIEW.value,
    }
    result: list[BlockedDecisionView] = []
    for action in state.recent_actions:
        tool_name, separator, rest = action.partition(": ")
        code = rest.partition(":")[0] if separator else ""
        if code in blocked_statuses:
            result.append(BlockedDecisionView(code=code, tool_name=tool_name[:128]))
    if state.stop_reason is StopReason.PERMISSION_REQUIRED:
        result.append(BlockedDecisionView(code="permission_required", tool_name=None))
    return result


async def _current_envelope_view(request: Request, investigation_id: UUID) -> EnvelopeView:
    async with _uow_factory(request)() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise _http_error(404, "investigation_not_found")
        composition = _composition_factory(request).build(uow, investigation_id)
        snapshot = await composition.tool_dependencies.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        current = await uow.investigation_envelopes.get(investigation_id)
        settings = _composition_factory(request).settings
        if current is None:
            return _envelope_view(
                investigation_id=investigation_id,
                budget=_configured_budget(settings),
                approved=False,
                version=0,
                allowed_providers=(),
                available_providers=_available_providers(composition.available_tools),
                acquire_hits_allowed=False,
                disclosure=_source_disclosure(snapshot),
                approval_decision_id=None,
                approval_actor_id=None,
                approved_at=None,
            )
        return _envelope_view(
            investigation_id=investigation_id,
            budget=current.budget,
            approved=True,
            version=current.version,
            allowed_providers=current.allowed_providers,
            available_providers=_available_providers(composition.available_tools),
            acquire_hits_allowed=current.acquire_hits_allowed,
            disclosure=current.disclosure,
            approval_decision_id=current.approval_decision_id,
            approval_actor_id=current.approval_actor_id,
            approved_at=current.approved_at,
        )


def _configured_budget(settings: Any) -> InvestigationBudget:
    return InvestigationBudget(
        limits={
            BudgetDimension.BRIDGE_TURNS: int(getattr(settings, "investigation_max_cycles", 12)),
            BudgetDimension.VT_UNITS: int(getattr(settings, "investigation_max_vt_read_units", 0)),
            BudgetDimension.SHODAN_CREDITS: int(
                getattr(settings, "investigation_max_shodan_credits", 0)
            ),
            BudgetDimension.DOWNLOADS: int(getattr(settings, "investigation_max_hits_acquired", 0)),
        }
    )


def _budget_from_request(
    payload: EnvelopeDecisionRequest,
    *,
    defaults: InvestigationBudget,
    acquire_default: bool = False,
) -> InvestigationBudget:
    limits = dict(defaults.limits)
    for raw_dimension, amount in payload.budget_limits.items():
        try:
            dimension = BudgetDimension(raw_dimension)
        except ValueError as exc:
            raise _http_error(422, "invalid_budget_dimension", raw_dimension) from exc
        if isinstance(amount, bool) or amount < 0:
            raise _http_error(422, "invalid_budget_limit", raw_dimension)
        limits[dimension] = amount
    if payload.max_acquire_volume is not None:
        limits[BudgetDimension.DOWNLOADS] = payload.max_acquire_volume
    elif (
        payload.acquire_hits_allowed is True
        and BudgetDimension.DOWNLOADS not in payload.budget_limits
    ):
        limits[BudgetDimension.DOWNLOADS] = defaults.limits.get(BudgetDimension.DOWNLOADS, 0)
    if payload.acquire_hits_allowed is False and acquire_default:
        # An explicit false revokes acquisition only through `/revoke` so that the
        # operator records a distinct, auditable G2 revocation action.
        limits[BudgetDimension.DOWNLOADS] = max(
            limits.get(BudgetDimension.DOWNLOADS, 0),
            defaults.limits.get(BudgetDimension.DOWNLOADS, 0),
        )
    return InvestigationBudget(limits=limits)


def _available_providers(available_tools: tuple[str, ...]) -> tuple[str, ...]:
    providers: list[str] = []
    if any(name.startswith("vt.") for name in available_tools):
        providers.append("vt")
    if any(name.startswith("shodan.") for name in available_tools):
        providers.append("shodan")
    return tuple(providers)


def _requested_providers(
    requested: tuple[str, ...] | list[str], available: tuple[str, ...]
) -> tuple[str, ...]:
    providers = tuple(dict.fromkeys(item.strip().lower() for item in requested))
    if any(item not in {"vt", "shodan"} for item in providers):
        raise _http_error(422, "unsupported_provider")
    unavailable = sorted(set(providers).difference(available))
    if unavailable:
        raise _http_error(422, "provider_unavailable", ", ".join(unavailable))
    return providers


def _source_disclosure(snapshot: Any) -> DisclosureBounds:
    return DisclosureBounds(
        tlp=_tlp_level(getattr(snapshot, "tlp", None)),
        do_not_submit=bool(getattr(snapshot, "do_not_submit", True)),
        external_llm_allowed=bool(getattr(snapshot, "external_llm_allowed", False)),
    )


def _disclosure_from_request(
    payload: EnvelopeDisclosureRequest,
    snapshot: Any,
    *,
    current: DisclosureBounds | None = None,
) -> DisclosureBounds:
    source = _source_disclosure(snapshot)
    baseline = current or source
    tlp = _tlp_level(payload.tlp) if payload.tlp is not None else baseline.tlp
    if payload.tlp is not None and tlp is None:
        raise _http_error(422, "invalid_tlp")
    do_not_submit = (
        baseline.do_not_submit if payload.do_not_submit is None else payload.do_not_submit
    )
    external_allowed = (
        baseline.external_llm_allowed
        if payload.external_llm_allowed is None
        else payload.external_llm_allowed
    )
    if (source.do_not_submit or baseline.do_not_submit) and not do_not_submit:
        raise _http_error(422, "disclosure_bounds_cannot_be_relaxed")
    if (not source.external_llm_allowed or not baseline.external_llm_allowed) and external_allowed:
        raise _http_error(422, "disclosure_bounds_cannot_be_relaxed")
    if _tlp_rank(tlp) < max(_tlp_rank(source.tlp), _tlp_rank(baseline.tlp)):
        raise _http_error(422, "disclosure_bounds_cannot_be_relaxed")
    if do_not_submit or tlp is TLPLevel.RED:
        external_allowed = False
    return DisclosureBounds(
        tlp=tlp,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_allowed,
    )


def _tlp_rank(level: TLPLevel | None) -> int:
    if level is None:
        return 0
    return {
        TLPLevel.WHITE: 1,
        TLPLevel.CLEAR: 1,
        TLPLevel.GREEN: 2,
        TLPLevel.AMBER: 3,
        TLPLevel.RED: 4,
    }[level]


def _decision_correlation(correlation_id: str, idempotency_key: str) -> str:
    key_hash = _hash_key(idempotency_key)[:32]
    return f"{correlation_id.strip()[:80]}:idem:{key_hash}"


def _envelope_view(
    *,
    investigation_id: UUID,
    budget: InvestigationBudget,
    approved: bool,
    version: int,
    allowed_providers: tuple[str, ...],
    available_providers: tuple[str, ...],
    acquire_hits_allowed: bool,
    disclosure: DisclosureBounds,
    approval_decision_id: UUID | None,
    approval_actor_id: str | None,
    approved_at: datetime | None,
) -> EnvelopeView:
    return EnvelopeView(
        investigation_id=investigation_id,
        approved=approved,
        version=version,
        allowed_providers=list(allowed_providers),
        available_providers=list(available_providers),
        acquire_hits_allowed=acquire_hits_allowed,
        max_acquire_volume=budget.limits.get(BudgetDimension.DOWNLOADS, 0),
        budget={
            dimension.value: BudgetDimensionView(
                limit=budget.limits.get(dimension),
                consumed=budget.consumed[dimension],
                remaining=budget.remaining[dimension],
            )
            for dimension in BudgetDimension
        },
        disclosure={
            "tlp": disclosure.tlp.value if disclosure.tlp else None,
            "do_not_submit": disclosure.do_not_submit,
            "external_llm_allowed": disclosure.external_llm_allowed,
        },
        approval_decision_id=approval_decision_id,
        approval_actor_id=approval_actor_id,
        approved_at=approved_at,
    )


def _sample_detail(association: Any) -> SampleDetailView:
    return SampleDetailView(
        id=association.id,
        sample_sha256=association.sample_sha256,
        origin=association.origin,
        qualification=association.qualification,
        related_sample_sha256=list(association.related_sample_sha256),
        version=association.version,
        changes=[
            QualificationChangeView(
                from_qualification=item.from_qualification,
                to_qualification=item.to_qualification,
                actor_kind=item.actor_kind,
                actor_id=item.actor_id or "",
                reason=item.reason,
                changed_at=item.changed_at,
            )
            for item in association.changes
        ],
    )


def _composition_factory(request: Request) -> InvestigationCompositionFactory:
    factory = getattr(request.app.state, "investigation_composition_factory", None)
    if factory is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "investigation_unavailable")
    return cast(InvestigationCompositionFactory, factory)


def _uow_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "uow_factory", None)
    if factory is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "investigation_unavailable")
    return factory


async def _identity(request: Request) -> Any:
    provider: IdentityProvider = request.app.state.identity_provider
    return await provider.current()


async def _dispatch(request: Request, job: Job) -> None:
    dispatcher = getattr(request.app.state, "job_dispatcher", None)
    if dispatcher is None:
        raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "job_dispatch_unavailable")
    await dispatcher.dispatch(job.id)


def _http_error(code_status: int, code: str, message: str | None = None) -> HTTPException:
    detail: dict[str, str] = {"code": code}
    if message:
        detail["message"] = message
    return HTTPException(status_code=code_status, detail=detail)


class InvocationReconciliationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    outcome: str = Field(
        pattern=r"^(executed_with_result|not_executed|abandon)$",
    )
    reason: str = Field(min_length=1, max_length=4_000)
    response_refs: tuple[UUID, ...] = Field(default=(), max_length=128)


class InvocationReconciliationResponse(BaseModel):
    invocation_id: UUID
    status: ToolInvocationStatus
    version: int
    response_refs: tuple[UUID, ...]


@router.post(
    "/investigations/{investigation_id}/invocations/{invocation_id}/reconcile",
    response_model=InvocationReconciliationResponse,
)
async def reconcile_investigation_invocation(
    investigation_id: UUID,
    invocation_id: UUID,
    payload: InvocationReconciliationRequest,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> InvocationReconciliationResponse:
    from cti_app.application.investigation_service import InvocationReconciliationOutcome

    identity = await _identity(request)
    async with _uow_factory(request)() as uow:
        try:
            composition = _composition_factory(request).build(uow, investigation_id)
            invocation = await composition.service.reconcile_invocation(
                investigation_id,
                invocation_id,
                outcome=InvocationReconciliationOutcome(payload.outcome),
                actor=identity.actor_id,
                reason=payload.reason,
                response_refs=payload.response_refs,
                expected_version=payload.expected_version,
                idempotency_key=_decision_correlation(
                    f"invocation:{invocation_id}", idempotency_key
                ),
            )
            await uow.commit()
        except LookupError as exc:
            raise _http_error(status.HTTP_404_NOT_FOUND, "invocation_not_found") from exc
        except VersionConflictError as exc:
            raise _http_error(status.HTTP_409_CONFLICT, "stale_invocation_version") from exc
        except ValueError as exc:
            raise _http_error(
                status.HTTP_409_CONFLICT, "invocation_reconciliation_conflict"
            ) from exc
        except RuntimeError as exc:
            raise _http_error(
                status.HTTP_503_SERVICE_UNAVAILABLE, "invocation_audit_unavailable"
            ) from exc
    return InvocationReconciliationResponse(
        invocation_id=invocation.id,
        status=invocation.status,
        version=invocation.version,
        response_refs=invocation.response_refs,
    )
