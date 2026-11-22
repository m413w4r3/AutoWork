"""Canonical external-disclosure gate for opening an operator session (M24 FIX-S).

R03 B2: opening a Codex/Claude session hands a shell, a provider credential and
the workspace bytes (samples, PCAPs, notes) to an external provider.  That is
exactly the disclosure the canonical rule already governs for Bridge and
operator tools, so this module *composes* the existing decisions instead of
inventing a second policy:

* a materialized G2 envelope that no ``PIVOT_REJECT`` decision revoked
  (:func:`g2_envelope_authorization_state`);
* for an OSINT-policy envelope, a pinned policy that is still active and bound
  to this investigation (:func:`osint_effective_policy_authorizes_binding`);
* ``snapshot.external_llm_allowed`` **and** ``envelope.disclosure.
  external_llm_allowed`` with a source/envelope TLP at or below the approved
  bound and neither RED (:meth:`InvestigationService.external_llm_allowed`);
* the configured advisor disclosure profile allowing the family
  (:meth:`AdvisorDisclosurePolicy.allows_disclosure`).

The gate runs before every host call and before any session row is written.
Its refusal is typed (``agent_session_external_llm_not_allowed``) and carries
only a stable reason code, never evidence.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from cti_app.application.agentic_sessions import SessionExternalDisclosureDeniedError
from cti_app.application.external_effects import advisor_disclosure_policy
from cti_app.application.investigation_governance import g2_envelope_authorization_state
from cti_app.application.investigation_persistence import InvestigationEnvelopeRepository
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.config import Settings
from cti_app.domain.investigation import EnvelopeAuthorityKind
from cti_app.domain.osint_open import (
    OsintEffectivePolicy,
    osint_effective_policy_authorizes_binding,
)

SESSION_DISCLOSURE_FAMILY = "agent.session"

SessionDisclosureGate = Callable[[UUID], Awaitable[None]]
OsintPolicyResolver = Callable[[UUID], Awaitable[OsintEffectivePolicy | None]]


class SessionDisclosureUnitOfWork(Protocol):
    """The canonical stores the gate reads; no host and no session row."""

    analyst_investigations: AnalystInvestigationRepository
    investigation_envelopes: InvestigationEnvelopeRepository
    analyst_decisions: AnalystDecisionRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


def _deny(reason: str) -> SessionExternalDisclosureDeniedError:
    return SessionExternalDisclosureDeniedError(
        "the investigation's G2 disclosure envelope does not allow an external "
        f"operator session ({reason})",
        details={"reason": reason},
    )


def build_session_disclosure_gate(
    *,
    uow_factory: Callable[[], SessionDisclosureUnitOfWork],
    article_snapshots: ArticleSnapshotPort,
    settings: Settings,
    effective_policy_for: OsintPolicyResolver | None = None,
) -> SessionDisclosureGate:
    """Return the canonical gate an operator session must pass before any host call."""
    disclosure_policy = advisor_disclosure_policy(settings.advisor_disclosure_profile)

    async def require_external_disclosure(investigation_id: UUID) -> None:
        async with uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise SessionExternalDisclosureDeniedError(
                    f"investigation {investigation_id} does not exist",
                    details={"reason": "investigation_missing"},
                )
            envelope = await uow.investigation_envelopes.get(investigation_id)
            if envelope is None:
                raise _deny("envelope_absent")
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            if g2_envelope_authorization_state(envelope, decisions).revoked:
                raise _deny("envelope_revoked")
            subject_id = getattr(investigation, "subject_id", None)
            synthesis_artifact_id = getattr(investigation, "synthesis_artifact_id", None)

        if envelope.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY:
            effective_policy = (
                None
                if effective_policy_for is None
                else await effective_policy_for(investigation_id)
            )
            if not isinstance(subject_id, UUID) or not osint_effective_policy_authorizes_binding(
                effective_policy,
                investigation_id=investigation_id,
                subject_id=subject_id,
                policy_id=envelope.inherited_policy_id,
                policy_version=envelope.inherited_policy_version,
                policy_event_id=envelope.inherited_policy_event_id,
            ):
                raise _deny("osint_policy_revoked")
        if synthesis_artifact_id is None:
            raise _deny("snapshot_unavailable")
        try:
            snapshot = await article_snapshots.resolve(synthesis_artifact_id)
        except (KeyError, OSError, ValueError) as exc:
            raise _deny("snapshot_unavailable") from exc
        allowed = InvestigationService.external_llm_allowed(snapshot, envelope)
        if not disclosure_policy.allows_disclosure(
            allowed, envelope_exists=True, family=SESSION_DISCLOSURE_FAMILY
        ):
            raise _deny("external_llm_not_allowed")

    return require_external_disclosure


__all__ = [
    "SESSION_DISCLOSURE_FAMILY",
    "SessionDisclosureGate",
    "SessionDisclosureUnitOfWork",
    "build_session_disclosure_gate",
]
