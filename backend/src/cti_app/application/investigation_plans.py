"""Create append-only investigation plan versions from verified Bridge advice.

ADR-0011 D1/D2: a plan version is born from one persisted ``advisor.consult``
invocation whose ``advisor_response_received`` observation belongs to the same
investigation, carries the exact ``advisor.consult.response`` source, and is
linked back to that invocation.  ``investigation_plans.current_version`` is the
only head and advances by compare-and-swap; ``investigation_plan_versions`` is
append-only.  Turning Bridge text into leads belongs to lot 04A2: this service
only accepts an already-structured :class:`InvestigationPlanV1`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    InvestigationPlanRepository,
    ToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.persistence import AnalystInvestigationRepository
from cti_app.domain.investigation import (
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import (
    InvestigationPlanV1,
    InvestigationPlanVersion,
    PlanParseStatus,
)

ADVISOR_TOOL_NAME = "advisor.consult"
ADVISOR_RESPONSE_SOURCE = "advisor.consult.response"
ADVISOR_RESPONSE_OBJECT_TYPE = "advisor_response_received"
PLAN_CONSULTATION_TYPE = "plan_investigation"
_ACCEPTED_INVOCATION_STATUSES = frozenset(
    {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
)


class InvestigationPlanError(ValueError):
    """Base refusal for a plan that cannot become a canonical version."""

    code = "investigation_plan_invalid"


class InvestigationPlanProvenanceError(InvestigationPlanError):
    """The claimed source is not a verified advisor response of this investigation."""

    code = "plan_provenance_invalid"


class InvestigationPlanIdempotencyError(InvestigationPlanError):
    """One consultation response cannot produce two different plan versions."""

    code = "plan_source_conflict"


class InvestigationPlanService:
    def __init__(
        self,
        *,
        plans: InvestigationPlanRepository,
        invocations: ToolInvocationRepository,
        observations: EvidenceObservationRepository,
        investigations: AnalystInvestigationRepository,
    ) -> None:
        self._plans = plans
        self._invocations = invocations
        self._observations = observations
        self._investigations = investigations

    async def create_version(
        self,
        plan: InvestigationPlanV1,
        *,
        parse_status: PlanParseStatus,
        source_invocation_id: UUID,
        source_observation_id: UUID,
        now: datetime | None = None,
    ) -> InvestigationPlanVersion:
        """Append one plan version and advance the CAS head in the caller's UoW.

        Replaying the same ``(source_invocation_id, source_observation_id)``
        pair with the same content returns the stored version unchanged.  A
        concurrent writer that loses the head CAS raises ``VersionConflictError``.
        """
        if not isinstance(plan, InvestigationPlanV1):
            raise TypeError("plan must use InvestigationPlanV1")
        if not isinstance(parse_status, PlanParseStatus):
            raise TypeError("parse_status must use PlanParseStatus")
        if not isinstance(source_invocation_id, UUID):
            raise TypeError("source_invocation_id must be a UUID")
        if not isinstance(source_observation_id, UUID):
            raise TypeError("source_observation_id must be a UUID")
        created_at = now or datetime.now(UTC)

        investigation = await self._investigations.get(plan.investigation_id)
        if investigation is None:
            raise InvestigationPlanError("plan_investigation_missing")
        invocation = await self._invocations.get(source_invocation_id)
        observation = await self._observations.get(source_observation_id)
        self._require_advisor_response(
            invocation,
            observation,
            investigation_id=plan.investigation_id,
            source_invocation_id=source_invocation_id,
            source_observation_id=source_observation_id,
        )

        existing = await self._plans.get_by_source(source_invocation_id, source_observation_id)
        if existing is not None:
            if existing.content != plan or existing.parse_status is not parse_status:
                raise InvestigationPlanIdempotencyError(
                    "The advisor response already produced a different plan version"
                )
            return existing

        head = await self._plans.get_head(plan.investigation_id)
        expected_current_version = head.current_version if head is not None else None
        expected_version = 1 if expected_current_version is None else expected_current_version + 1
        if plan.version != expected_version:
            raise VersionConflictError("Plan current version changed")

        supersedes_id: UUID | None = None
        if head is not None:
            previous = await self._plans.get_by_version(plan.investigation_id, head.current_version)
            if previous is None:
                raise RuntimeError("The plan head points to a missing version")
            supersedes_id = previous.id

        version = InvestigationPlanVersion(
            investigation_id=plan.investigation_id,
            version=plan.version,
            content=plan,
            parse_status=parse_status,
            source_invocation_id=source_invocation_id,
            source_observation_id=source_observation_id,
            supersedes_id=supersedes_id,
            created_at=created_at,
        )
        stored, _created = await self._plans.create_version(
            version, expected_current_version=expected_current_version
        )
        return stored

    @staticmethod
    def _require_advisor_response(
        invocation: ToolInvocation | None,
        observation: EvidenceObservation | None,
        *,
        investigation_id: UUID,
        source_invocation_id: UUID,
        source_observation_id: UUID,
    ) -> None:
        if invocation is None or invocation.id != source_invocation_id:
            raise InvestigationPlanProvenanceError("Plan source invocation is not persisted")
        if invocation.investigation_id != investigation_id:
            raise InvestigationPlanProvenanceError(
                "Plan source invocation belongs to another investigation"
            )
        if invocation.tool_name != ADVISOR_TOOL_NAME:
            raise InvestigationPlanProvenanceError(
                "Plan source invocation is not an advisor consultation"
            )
        if invocation.status not in _ACCEPTED_INVOCATION_STATUSES:
            raise InvestigationPlanProvenanceError(
                "Plan source invocation has no usable advisor output"
            )
        consultation_type = invocation.normalized_parameters.get("consultation_type")
        if consultation_type != PLAN_CONSULTATION_TYPE:
            raise InvestigationPlanProvenanceError(
                "Plan source invocation is not a plan_investigation consultation"
            )
        if observation is None or observation.id != source_observation_id:
            raise InvestigationPlanProvenanceError("Plan source observation is not persisted")
        if observation.investigation_id != investigation_id:
            raise InvestigationPlanProvenanceError(
                "Plan source observation belongs to another investigation"
            )
        if (
            observation.source != ADVISOR_RESPONSE_SOURCE
            or observation.object_type != ADVISOR_RESPONSE_OBJECT_TYPE
        ):
            raise InvestigationPlanProvenanceError(
                "Plan source is not an advisor_response_received observation"
            )
        if observation.invocation_ref is not None and observation.invocation_ref != invocation.id:
            raise InvestigationPlanProvenanceError(
                "Plan source observation points to another invocation"
            )
        linked_by_ref = observation.invocation_ref == invocation.id
        linked_by_response = observation.id in invocation.response_refs
        if not linked_by_ref and not linked_by_response:
            raise InvestigationPlanProvenanceError(
                "Plan source observation is not linked to its invocation"
            )
