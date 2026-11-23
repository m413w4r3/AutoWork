"""Canonical external-disclosure gate for opening or feeding an operator session.

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

The gate runs before every host call that can *start or feed* the external
operator — ``prepare``/``open``/``start``/``resume``, a transfer that opens a
destination session, and an instruction delivery (including a note
continuation) — and before any session row is written.  Transport-only host
calls (interrupt, reconcile, collect, release) do not carry new evidence and
are not gated.  Its refusal is typed
(``agent_session_external_llm_not_allowed``) and carries only a stable reason
code, never evidence.

F3-D §3.3 splits the gate in two so a *deferred* instruction delivery can never
rely on a decision taken before it: :meth:`SessionDisclosureChecker.pre_read`
records only the immutable inputs (the investigation binding and its synthesis
snapshot) ahead of the delivery transaction, while
:meth:`SessionDisclosureChecker.require` re-reads the mutable inputs (envelope,
revocation decisions, OSINT policy binding) inside the transaction that
reserves or confirms the attempt.  The composed :func:`build_session_disclosure_gate`
keeps the single-call contract every existing host call site already uses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, Self
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
from cti_app.application.research_context_builder import ArticleSnapshot, ArticleSnapshotPort
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


@dataclass(frozen=True, slots=True)
class SessionDisclosurePreRead:
    """The immutable half of the disclosure decision (F3-D §3.3).

    The investigation binding and its synthesis snapshot are content-addressed
    and do not change under a delivery attempt, so a deferred delivery may read
    them once.  Everything mutable — the G2 envelope, its revocation decisions
    and the OSINT policy binding — is deliberately absent: it is re-read by
    :meth:`SessionDisclosureChecker.require` in the transaction that acts.
    """

    investigation_id: UUID
    subject_id: UUID | None
    snapshot: ArticleSnapshot


class SessionDisclosureCheckerPort(Protocol):
    """The split gate a delivery attempt re-runs in each transaction."""

    async def pre_read(self, investigation_id: UUID) -> SessionDisclosurePreRead: ...

    async def require(
        self, pre_read: SessionDisclosurePreRead, uow: SessionDisclosureUnitOfWork
    ) -> None: ...


def _deny(reason: str) -> SessionExternalDisclosureDeniedError:
    return SessionExternalDisclosureDeniedError(
        "the investigation's G2 disclosure envelope does not allow an external "
        f"operator session ({reason})",
        details={"reason": reason},
    )


class SessionDisclosureChecker:
    """The split gate: immutable pre-read plus a mutable in-transaction check."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], SessionDisclosureUnitOfWork],
        article_snapshots: ArticleSnapshotPort,
        disclosure_policy: Any,
        effective_policy_for: OsintPolicyResolver | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._article_snapshots = article_snapshots
        self._disclosure_policy = disclosure_policy
        self._effective_policy_for = effective_policy_for

    async def pre_read(self, investigation_id: UUID) -> SessionDisclosurePreRead:
        """Read the immutable inputs in a short, read-only unit of work.

        The transaction closes before the content-addressed snapshot is
        resolved, so no lock is held across the artifact read.  The snapshot
        resolution is delegated to the canonical artifact port, so this method
        never trusts a caller-supplied snapshot or TLP value.
        """
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise SessionExternalDisclosureDeniedError(
                    f"investigation {investigation_id} does not exist",
                    details={"reason": "investigation_missing"},
                )
            subject_id = getattr(investigation, "subject_id", None)
            synthesis_artifact_id = getattr(investigation, "synthesis_artifact_id", None)
        if synthesis_artifact_id is None:
            raise _deny("snapshot_unavailable")
        try:
            snapshot = await self._article_snapshots.resolve(synthesis_artifact_id)
        except (KeyError, OSError, ValueError) as exc:
            raise _deny("snapshot_unavailable") from exc
        return SessionDisclosurePreRead(
            investigation_id=investigation_id,
            subject_id=subject_id if isinstance(subject_id, UUID) else None,
            snapshot=snapshot,
        )

    async def require(
        self, pre_read: SessionDisclosurePreRead, uow: SessionDisclosureUnitOfWork
    ) -> None:
        """Re-read every mutable input and refuse or accept the disclosure."""
        investigation_id = pre_read.investigation_id
        envelope = await uow.investigation_envelopes.get(investigation_id)
        if envelope is None:
            raise _deny("envelope_absent")
        decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
        if g2_envelope_authorization_state(envelope, decisions).revoked:
            raise _deny("envelope_revoked")
        if envelope.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY:
            effective_policy = (
                None
                if self._effective_policy_for is None
                else await self._effective_policy_for(investigation_id)
            )
            if not isinstance(
                pre_read.subject_id, UUID
            ) or not osint_effective_policy_authorizes_binding(
                effective_policy,
                investigation_id=investigation_id,
                subject_id=pre_read.subject_id,
                policy_id=envelope.inherited_policy_id,
                policy_version=envelope.inherited_policy_version,
                policy_event_id=envelope.inherited_policy_event_id,
            ):
                raise _deny("osint_policy_revoked")
        allowed = InvestigationService.external_llm_allowed(pre_read.snapshot, envelope)
        if not self._disclosure_policy.allows_disclosure(
            allowed, envelope_exists=True, family=SESSION_DISCLOSURE_FAMILY
        ):
            raise _deny("external_llm_not_allowed")


def build_session_disclosure_checker(
    *,
    uow_factory: Callable[[], SessionDisclosureUnitOfWork],
    article_snapshots: ArticleSnapshotPort,
    settings: Settings,
    effective_policy_for: OsintPolicyResolver | None = None,
) -> SessionDisclosureChecker:
    """Return the split gate a delivery attempt re-runs in each transaction."""
    return SessionDisclosureChecker(
        uow_factory=uow_factory,
        article_snapshots=article_snapshots,
        disclosure_policy=advisor_disclosure_policy(settings.advisor_disclosure_profile),
        effective_policy_for=effective_policy_for,
    )


def build_session_disclosure_gate(
    *,
    uow_factory: Callable[[], SessionDisclosureUnitOfWork],
    article_snapshots: ArticleSnapshotPort,
    settings: Settings,
    effective_policy_for: OsintPolicyResolver | None = None,
) -> SessionDisclosureGate:
    """Return the canonical gate an operator session must pass before any host call."""
    checker = build_session_disclosure_checker(
        uow_factory=uow_factory,
        article_snapshots=article_snapshots,
        settings=settings,
        effective_policy_for=effective_policy_for,
    )

    async def require_external_disclosure(investigation_id: UUID) -> None:
        pre_read = await checker.pre_read(investigation_id)
        async with uow_factory() as uow:
            await checker.require(pre_read, uow)

    return require_external_disclosure


__all__ = [
    "SESSION_DISCLOSURE_FAMILY",
    "SessionDisclosureChecker",
    "SessionDisclosureCheckerPort",
    "SessionDisclosureGate",
    "SessionDisclosurePreRead",
    "SessionDisclosureUnitOfWork",
    "build_session_disclosure_checker",
    "build_session_disclosure_gate",
]
