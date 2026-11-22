"""One-time session secret hand-off to the host supervisor (ADR-0011 D7).

ADR D7 derives the per-session HMAC secret in the backend and hands it to the
host **once**; SESSION-RUNNER §3/§5/§6 make that hand-off the ``auth_secret`` of
the single ``POST /v1/sessions`` prepare call.  This implementation:

* performs the host prepare exactly once per session, with the derived secret;
* keeps no copy of the secret (only its SHA-256, for an idempotent replay);
* treats the recorded host probe hash as a fact to re-verify, never to trust;
* refuses a second, different delivery for the same session, cleanly.

The :class:`SessionSecret` value is redacted by construction; this module never
logs it, returns it, or puts it in an error message.
"""

from __future__ import annotations

import base64
import hashlib
from uuid import UUID

from cti_app.application.agentic_host_sessions import (
    HOST_SECRET_BYTES,
    HostCapabilityUnavailableError,
    HostPrepareSessionRequestV1,
    HostSecretHandoffConsumedError,
    HostSessionContractError,
    HostSessionsPort,
)
from cti_app.application.agentic_sessions import (
    AgentSessionSecretDeliveryPort,
    SessionSecret,
)
from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.infrastructure.agentic.host_operator import (
    HostSessionPreparationRegistry,
    host_idempotency_key,
)


class HostSessionSecretDelivery(AgentSessionSecretDeliveryPort):
    """The real D7 delivery; the supervisor may receive the secret once."""

    def __init__(
        self, *, host: HostSessionsPort, preparations: HostSessionPreparationRegistry
    ) -> None:
        self._host = host
        self._preparations = preparations

    async def deliver_session_secret(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        host_session_ref: str,
        secret: SessionSecret,
        idempotency_key: str,
    ) -> None:
        if host_session_ref != str(session_id):
            raise HostSessionContractError("the host session reference names another session")
        registered = self._preparations.get(session_id)
        if registered is None:
            raise HostSessionContractError("no host preparation was recorded for the session")
        preparation = registered.preparation
        if (
            preparation.investigation_id != investigation_id
            or preparation.task_id != task_id
            or preparation.adapter_kind != adapter_kind.value
        ):
            raise HostSessionContractError("the session secret names another session identity")

        secret_hex = secret.reveal()
        secret_sha256 = hashlib.sha256(secret_hex.encode("ascii")).hexdigest()
        delivered = self._preparations.delivery(session_id)
        if delivered is not None:
            # An idempotent 05B1 replay re-derives the same secret and key: the
            # hand-off stays one-shot.  Anything else is refused before a second
            # secret can reach the supervisor.
            if (
                delivered.secret_sha256 == secret_sha256
                and delivered.idempotency_key == idempotency_key
            ):
                return
            raise HostSecretHandoffConsumedError(
                "the one-time session secret was already delivered"
            )

        secret_bytes = bytes.fromhex(secret_hex)
        if len(secret_bytes) != HOST_SECRET_BYTES:  # pragma: no cover - SessionSecret guarantees
            raise HostSessionContractError("the session secret is not 32 bytes")
        request: HostPrepareSessionRequestV1 = preparation.host_request(
            base64.b64encode(secret_bytes).decode("ascii")
        )
        prepared = await self._host.prepare_session(
            request,
            idempotency_key=host_idempotency_key("prepare", session_id, idempotency_key),
        )
        if prepared.session_id != session_id:
            raise HostSessionContractError("the supervisor prepared another session")
        if prepared.capability_sha256 != preparation.expected_probe_sha256:
            raise HostCapabilityUnavailableError(
                "the host capability probe changed between probe and prepare"
            )
        self._preparations.record_delivery(
            session_id,
            secret_sha256=secret_sha256,
            idempotency_key=idempotency_key,
        )


__all__ = ["HostSessionSecretDelivery"]
