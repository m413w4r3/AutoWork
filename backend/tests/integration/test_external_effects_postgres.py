from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    ConsumeCommitted,
    ConsumePolicyContext,
    DefinitelyNotSubmitted,
    ExternalEffectCoordinator,
    HandoffGateRefused,
    PhysicalSendCapability,
    Refused,
    Reused,
    ReuseDecision,
    TransportResponse,
)
from cti_app.application.investigation_advisor_tools import ADVISOR_SCHEMA_VERSION
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    BRIDGE_SOURCE_PROVENANCE_KEY,
    AdapterResult,
    AdapterResultStatus,
    BridgeExternalEffectBinding,
    ModelCapabilities,
    ModelGateway,
    ModelRouter,
    ModelUsage,
)
from cti_app.application.osint_open_policy import OsintOpenPolicyService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.external_effects import (
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    ExternalEffectValidationError,
    FrozenWireRequest,
    PreparedExternalRequest,
    _wire_request_sha256,
    application_request_sha256,
    canonical_json_bytes,
    effect_key,
    occurrence_sha256,
)
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    DisclosureBounds,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationExecutionMode,
    TLPLevel,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPurpose,
    ConversationTransport,
)
from cti_app.domain.model_runs import ModelBackend, ModelProvider, ModelRole, ModelTransport
from cti_app.domain.osint_open import OsintCapabilities, OsintScopeKind
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.core import BlobRow, SampleRow, SourceDocumentRow
from cti_app.infrastructure.database.models.external_effects import (
    ExternalEffectClaimRow,
    ExternalEffectUseRow,
)
from cti_app.infrastructure.database.models.investigation import (
    EvidenceObservationRow,
    InvestigationBudgetRecordRow,
    InvestigationBudgetReservationAmountRow,
    InvestigationBudgetReservationRow,
    InvestigationEnvelopeRow,
    SampleAssociationRow,
    ToolInvocationRow,
)
from cti_app.infrastructure.database.models.model_execution import (
    ModelConversationRow,
    ModelConversationTurnRow,
    ModelRunRow,
)
from cti_app.infrastructure.database.models.production import AnalystInvestigationRow
from cti_app.infrastructure.database.repositories.external_effects import (
    ExternalEffectFamilyBinding,
    SqlAlchemyExternalEffectClaimStore,
)
from cti_app.infrastructure.database.repositories.investigation import _budget_to_payload
from cti_app.infrastructure.database.repositories.virustotal_external_effects import (
    SqlAlchemyVirusTotalPolicyGuard,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.investigation_runtime_ports import create_external_effect_coordinator
from cti_app.integrations.models import (
    BlobModelOutputStore,
    BridgeTransportError,
    FakeModelAdapter,
    create_coordinated_http_responses_transport,
)
from tests.integration.conftest import _alembic_config
from tests.workflow_emulator.investigation_support import seed_verified_article

pytestmark = pytest.mark.integration

_FAMILY = "vt.file_report"
_NON_BRIDGE_MODEL_UPLOAD_FAMILY = "openai.external_upload.v1"
_PROVIDER = "virustotal"
_NORMALIZER = "vt.file-report.v1"
_ARGUMENTS = {"hash": "abc"}
_REQUEST = {"hash": "abc", "include": ["relations", "history"]}


class _CountingTransport:
    def __init__(
        self,
        *,
        response: TransportResponse | Exception | None = None,
    ) -> None:
        self.__raw_client = _CountingRawClient(response)
        self.submit_entries = 0

    @property
    def calls(self) -> int:
        return self.__raw_client.calls

    async def submit_once(
        self,
        request: FrozenWireRequest,
        capability: PhysicalSendCapability | None = None,
    ) -> TransportResponse:
        self.submit_entries += 1
        if (
            not isinstance(capability, PhysicalSendCapability)
            or type(capability) is not PhysicalSendCapability
        ):
            raise HandoffGateRefused("physical send capability is required")
        if self.__raw_client.retries_enabled:
            raise HandoffGateRefused("raw client retries must be disabled")
        capability(request)
        return await self.__raw_client.send(request, follow_redirects=False)


class _CountingRawClient:
    def __init__(self, response: TransportResponse | Exception | None = None) -> None:
        self.calls = 0
        self.retries_enabled = False
        self.response = response or TransportResponse(result_ref="artifact:pg-fake-result")

    async def send(
        self, _request: FrozenWireRequest, *, follow_redirects: bool
    ) -> TransportResponse:
        assert follow_redirects is False
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _SyntheticOpenAIBridgeAdapter:
    """A deterministic local adapter for persisted Bridge conversation fixtures."""

    provider = ModelProvider.OPENAI
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    requested_model = "test-model"
    is_external = True
    is_configured = True
    capabilities = ModelCapabilities()

    async def invoke(
        self,
        _request: Any,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del role, output_schema
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(),
            output_text="Synthetic local advisor response.",
        )

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("The deterministic synthetic adapter is never resumed.")


class _BarrierPolicyGuard:
    """Barrier guard with policy-event lock before envelope lock in both paths."""

    def __init__(self, policy_event_id: UUID, *, block_consume: bool = False) -> None:
        self.policy_event_id = policy_event_id
        self.lock_key = policy_event_id.int & ((1 << 63) - 1)
        self.policy_version = "osint-open.v1"
        self.revoked = False
        self.block_consume = block_consume
        self.consume_locked = asyncio.Event()
        self.release_consume = asyncio.Event()
        self.revoke_started = asyncio.Event()
        self.events: list[str] = []

    async def lock_for_consume(self, database_session: AsyncSession, authority: Any) -> bool:
        self.events.append("policy_lock")
        if authority.policy_event_id != self.policy_event_id:
            return False
        if authority.policy_version != self.policy_version:
            return False
        await database_session.execute(
            text("SELECT pg_advisory_xact_lock(:policy_lock_key)"),
            {"policy_lock_key": self.lock_key},
        )
        self.consume_locked.set()
        if self.block_consume:
            await self.release_consume.wait()
        return not self.revoked

    async def authorize(self, context: ConsumePolicyContext) -> bool:
        self.events.append("policy_authorize")
        return (
            bool(context.database_session.in_transaction())
            and not self.revoked
            and context.investigation_subject_id == context.authority.pinned_subject_id
        )

    async def revoke(self, session_factory, investigation_id: UUID) -> None:
        self.revoke_started.set()
        async with session_factory() as session:
            async with session.begin():
                # Policy-event then envelope is the shared consume/revoke order.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:policy_lock_key)"),
                    {"policy_lock_key": self.lock_key},
                )
                await session.scalar(
                    select(InvestigationEnvelopeRow)
                    .where(InvestigationEnvelopeRow.investigation_id == investigation_id)
                    .with_for_update()
                )
                self.revoked = True


class _ClaimBarrierStore:
    """Boundedly rendezvous workers immediately before the claim transaction."""

    def __init__(
        self,
        delegate: SqlAlchemyExternalEffectClaimStore,
        barrier: asyncio.Barrier,
        *,
        timeout_seconds: float = 5.0,
    ):
        self._delegate = delegate
        self._barrier = barrier
        self._timeout_seconds = timeout_seconds
        self._participants_remaining = barrier.parties
        self._barrier_aborted = False
        self._barrier_state_lock = asyncio.Lock()

    async def consume(self, *args: Any, **kwargs: Any):
        async with self._barrier_state_lock:
            should_wait = not self._barrier_aborted and self._participants_remaining > 0
            if should_wait:
                self._participants_remaining -= 1
        if not should_wait:
            return await self._delegate.consume(*args, **kwargs)
        try:
            await asyncio.wait_for(self._barrier.wait(), timeout=self._timeout_seconds)
        except TimeoutError as exc:
            # A caller may have refused during coordinator pre-validation and
            # never reached this seam. Break the one-shot barrier so no peer
            # waits on it; later replay calls bypass this already-used gate.
            async with self._barrier_state_lock:
                self._barrier_aborted = True
            await self._barrier.abort()
            raise RuntimeError("claim workers did not rendezvous before timeout") from exc
        except asyncio.BrokenBarrierError as exc:
            async with self._barrier_state_lock:
                self._barrier_aborted = True
            raise RuntimeError("claim worker barrier was aborted") from exc
        return await self._delegate.consume(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _PreConsumeFailureStore:
    """Inject a crash before the real store opens a transaction."""

    def __init__(self, delegate: SqlAlchemyExternalEffectClaimStore) -> None:
        self._delegate = delegate
        self.consume_calls = 0

    async def consume(self, *args: Any, **kwargs: Any):
        self.consume_calls += 1
        raise RuntimeError("injected crash before consume")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


async def _prepare_case(
    database_url: str,
    tmp_path: Path,
    *,
    suffix: str,
    invocations: int = 1,
) -> tuple[
    async_sessionmaker[AsyncSession],
    AsyncEngine,
    UUID,
    UUID,
    list[tuple[ExternalEffectAuthority, PreparedExternalRequest]],
]:
    engine = create_postgres_engine(database_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    artifact_store = ProductionArtifactStore(
        BlobCatalogService(
            FilesystemBlobStore(tmp_path / f"external-effects-{suffix}"), uow_factory
        )
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=suffix,
        include_seed=False,
    )
    subject_id = seeded.subject.id
    approval_id = uuid4()
    now = datetime.now(UTC)
    budget = InvestigationBudget(limits={BudgetDimension.VT_UNITS: max(1, invocations)})
    operation_keys = [f"external-effect-op-{uuid4()}" for _ in range(invocations)]
    for operation_key in operation_keys:
        budget.reserve(operation_key, {BudgetDimension.VT_UNITS: 1})
    budget_snapshot = _budget_to_payload(budget)
    authorities: list[tuple[ExternalEffectAuthority, PreparedExternalRequest]] = []
    async with session_factory() as session:
        async with session.begin():
            await session.execute(update_investigation_status(seeded.investigation.id))
            await session.execute(
                insert(InvestigationBudgetRecordRow)
                .values(investigation_id=seeded.investigation.id, version=1, updated_at=now)
                .on_conflict_do_nothing(
                    index_elements=[InvestigationBudgetRecordRow.investigation_id]
                )
            )
            await session.execute(
                insert(InvestigationEnvelopeRow)
                .values(
                    investigation_id=seeded.investigation.id,
                    allowed_providers=[_PROVIDER],
                    budget_snapshot=budget_snapshot,
                    acquire_hits_allowed=True,
                    disclosure_tlp="clear",
                    disclosure_do_not_submit=False,
                    disclosure_external_llm_allowed=True,
                    approval_decision_id=approval_id,
                    approval_actor_id="integration-test",
                    approved_at=now,
                    updated_at=now,
                    version=4,
                )
                .on_conflict_do_update(
                    index_elements=[InvestigationEnvelopeRow.investigation_id],
                    set_={
                        "allowed_providers": [_PROVIDER],
                        "budget_snapshot": budget_snapshot,
                        "acquire_hits_allowed": True,
                        "disclosure_tlp": "clear",
                        "disclosure_do_not_submit": False,
                        "disclosure_external_llm_allowed": True,
                        "approval_decision_id": approval_id,
                        "approval_actor_id": "integration-test",
                        "approved_at": now,
                        "updated_at": now,
                        "version": 4,
                    },
                )
            )
            for index in range(invocations):
                operation_key = operation_keys[index]
                invocation_id = uuid4()
                session.add(
                    ToolInvocationRow(
                        id=invocation_id,
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        tool_name=_FAMILY,
                        schema_version="v1",
                        normalized_parameters=_ARGUMENTS,
                        purpose="S3 external-effect integration fixture",
                        status="running",
                        reserved_budget={"vt_units": 1},
                        consumed_budget={},
                        response_refs=[],
                        created_at=now,
                        updated_at=now,
                        version=1,
                    )
                )
                session.add(
                    InvestigationBudgetReservationRow(
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        status="reserved",
                        sequence=1000 + index,
                    )
                )
                await session.flush()
                session.add(
                    InvestigationBudgetReservationAmountRow(
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        dimension="vt_units",
                        reserved_amount=1,
                        committed_amount=None,
                    )
                )
                authority = ExternalEffectAuthority(
                    investigation_id=seeded.investigation.id,
                    invocation_id=invocation_id,
                    operation_key=operation_key,
                    reservation_amounts={"vt_units": 1},
                    expected_envelope_version=4,
                    approval_decision_id=approval_id,
                    invocation_arguments_sha256=hashlib.sha256(
                        canonical_json_bytes(_ARGUMENTS)
                    ).hexdigest(),
                )
                prepared = PreparedExternalRequest.build(
                    family=_FAMILY,
                    provider=_PROVIDER,
                    provider_scope="tenant-test",
                    normalizer_version=_NORMALIZER,
                    canonical_request=_REQUEST,
                    application_request=FrozenWireRequest(
                        method=b"GET",
                        target=b"/v1/report?hash=abc",
                        headers=((b"accept", b"application/json"),),
                        body=b"",
                    ),
                )
                authorities.append((authority, prepared))
    async with uow_factory() as uow:
        await uow.investigation_states.save_if_version(
            InvestigationWorkingState(
                investigation_id=seeded.investigation.id,
                agenda=InvestigationAgenda(
                    items=(
                        AgendaItem(question="Verify the source claim.", potential=0.9, rank=1),
                        AgendaItem(question="Check for contradictions.", potential=0.7, rank=2),
                    )
                ),
                execution_mode=InvestigationExecutionMode.BRIDGE_LED,
            ),
            expected_version=None,
        )
        await uow.commit()
    return session_factory, engine, seeded.investigation.id, subject_id, authorities


async def _prepare_model_submission_case(
    database_url: str,
    tmp_path: Path,
    *,
    suffix: str,
    persisted_request_id: str = "bridge-request-1",
    claimed_request_id: str | None = None,
) -> tuple[
    async_sessionmaker[AsyncSession],
    AsyncEngine,
    UUID,
    ExternalEffectAuthority,
    PreparedExternalRequest,
    SqlAlchemyExternalEffectClaimStore,
    UUID,
]:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        database_url, tmp_path, suffix=suffix
    )
    authority, _base = authorities[0]
    conversation_id = uuid4()
    turn_id = uuid4()
    run_id = uuid4()
    input_sha256 = "a" * 64
    effective_claimed_request_id = claimed_request_id or persisted_request_id
    now = datetime.now(UTC)
    async with session_factory() as session:
        async with session.begin():
            envelope = await session.get(InvestigationEnvelopeRow, investigation_id)
            assert envelope is not None
            envelope.allowed_providers = ["bridge"]
            session.add(
                ModelConversationRow(
                    id=conversation_id,
                    provider="openai",
                    transport="chatgpt_bridge",
                    purpose="analyst_assistance",
                    edition_id=None,
                    subject_id=_subject_id,
                    title="S3 test conversation",
                    status="busy",
                    external_id=None,
                    external_locator=None,
                    expected_profile=None,
                    requested_model=None,
                    head_turn_id=None,
                    turn_count=1,
                    created_at=now,
                    updated_at=now,
                    last_used_at=None,
                    version=1,
                )
            )
            session.add(
                ModelRunRow(
                    id=run_id,
                    provider="openai",
                    backend="chatgpt_bridge",
                    transport="openai_responses",
                    model_role="research",
                    requested_model="test-model",
                    actual_model_version=None,
                    prompt_template_id="test.prompt",
                    prompt_template_version="1",
                    authorized_input_hash=input_sha256,
                    evidence_pack_hash="b" * 64,
                    parameters={},
                    duration_ms=None,
                    usage=None,
                    status="running",
                    submission_state="external_state_unknown",
                    submission_attempt=1,
                    bridge_request_id=persisted_request_id,
                    response_id=None,
                    output_references=[],
                    error_code=None,
                    error_message=None,
                    error_details=None,
                    raw_output_reference=None,
                    raw_output_sha256=None,
                    raw_output_chars=None,
                    normalized_output_reference=None,
                    normalized_output_sha256=None,
                    parser_stage=None,
                    serializer_version=None,
                    normalization_version=None,
                    json_error_line=None,
                    json_error_column=None,
                    validation_errors=[],
                    transformations=[],
                    citation_count=0,
                    extracted_url_count=0,
                    visible_citations=[],
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                ModelConversationTurnRow(
                    id=turn_id,
                    conversation_id=conversation_id,
                    sequence=1,
                    parent_turn_id=None,
                    model_run_id=run_id,
                    input_blob_reference="sha256:" + input_sha256,
                    input_sha256=input_sha256,
                    output_blob_reference=None,
                    output_sha256=None,
                    status="running",
                    external_turn_id=None,
                    idempotency_key=f"test:{run_id}",
                    correlation_id=f"test:{run_id}",
                    error_code=None,
                    error_message=None,
                    error_details=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                )
            )
    authority = replace(authority, bridge_turn_id=turn_id, model_run_id=run_id)
    prepared = PreparedExternalRequest.build(
        family="bridge.investigation_turn.v1",
        provider="bridge",
        provider_scope="bridge-test",
        normalizer_version="bridge.turn.v1",
        canonical_request={
            "model_input_sha256": input_sha256,
            "authorized_input_hash": input_sha256,
            "bridge_request_id": effective_claimed_request_id,
        },
        application_request=FrozenWireRequest(
            method=b"POST",
            target=b"/responses",
            headers=((b"content-type", b"application/json"),),
            body=b'{"input":"safe"}',
        ),
    )
    store = SqlAlchemyExternalEffectClaimStore(
        session_factory,
        family_bindings={
            prepared.family: ExternalEffectFamilyBinding(
                provider="bridge",
                provider_scopes=frozenset({"bridge-test"}),
                normalizer_versions=frozenset({"bridge.turn.v1"}),
                invocation_tool_binding=(_FAMILY, "v1"),
                model_submission=True,
                requires_model_lineage=True,
                model_run_provider="openai",
            )
        },
    )
    return session_factory, engine, investigation_id, authority, prepared, store, run_id


def _bridge_model_lineage_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyExternalEffectClaimStore:
    return SqlAlchemyExternalEffectClaimStore(
        session_factory,
        family_bindings={
            "bridge.investigation_turn.v1": ExternalEffectFamilyBinding(
                provider="bridge",
                provider_scopes=frozenset({"bridge-test"}),
                normalizer_versions=frozenset({"bridge.turn.v1"}),
                invocation_tool_binding=None,
                model_submission=True,
                requires_model_lineage=True,
                model_run_provider="openai",
            )
        },
    )


@pytest.mark.asyncio
async def test_prestarted_bridge_attempt_matches_request_and_does_not_increment_twice(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    (
        session_factory,
        engine,
        _investigation_id,
        authority,
        prepared,
        store,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    transport = _CountingTransport()
    try:
        async with session_factory() as session:
            before = await session.get(ModelRunRow, run_id)
        assert before is not None
        assert before.submission_state == "external_state_unknown"
        assert before.submission_attempt == 1
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(outcome, Completed)
        assert transport.calls == 1
        async with session_factory() as session:
            run = await session.get(ModelRunRow, run_id)
            claim = await session.get(ExternalEffectClaimRow, outcome.claim_id)
        assert run is not None
        assert run.submission_state == "result_obtained"
        assert run.submission_attempt == 1
        assert run.bridge_request_id == prepared.canonical_request["bridge_request_id"]
        assert claim is not None and claim.model_run_id == run_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_coordinated_bridge_http_post_completes_one_persisted_model_claim(
    migrated_postgres_url: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        session_factory,
        engine,
        investigation_id,
        authority,
        prepared,
        store,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    api_key = "pg-bridge-api-key-canary-do-not-persist"
    captured: list[httpx.Request] = []
    prompt = "Exact synthetic prompt bytes for the PostgreSQL transport test."
    payload = {"model": "chatgpt-web", "input": prompt}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"id":"resp-synthetic","status":"completed","output_text":"ok"}',
        )

    assert authority.bridge_turn_id is not None
    assert authority.model_run_id == run_id
    assert isinstance(prepared.canonical_request["model_input_sha256"], str)
    assert isinstance(prepared.canonical_request["authorized_input_hash"], str)
    binding = BridgeExternalEffectBinding(
        authority=authority,
        family=prepared.family,
        provider_scope=prepared.provider_scope,
        normalizer_version=prepared.normalizer_version,
        bridge_turn_id=authority.bridge_turn_id,
        model_run_id=run_id,
        model_input_sha256=prepared.canonical_request["model_input_sha256"],
        authorized_input_hash=prepared.canonical_request["authorized_input_hash"],
    )
    transport = create_coordinated_http_responses_transport(
        "https://bridge.synthetic/v1",
        api_key=api_key,
        coordinator=ExternalEffectCoordinator(store),
        http_transport=httpx.MockTransport(handler),
    )
    try:
        result = await transport.create(
            payload,
            idempotency_key="bridge-request-1",
            bridge_external_effect_binding=binding,
        )
        assert result["output_text"] == "ok"
        assert len(captured) == 1
        request = captured[0]
        assert request.method == "POST"
        assert request.url.path == "/v1/responses"
        assert json.loads(request.content)["input"] == prompt
        assert request.headers["Authorization"] == f"Bearer {api_key}"
        async with session_factory() as session:
            claims = list(
                await session.scalars(
                    select(ExternalEffectClaimRow).where(
                        ExternalEffectClaimRow.investigation_id == investigation_id,
                        ExternalEffectClaimRow.family == "bridge.investigation_turn.v1",
                    )
                )
            )
            run = await session.get(ModelRunRow, run_id)
        assert len(claims) == 1
        claim = claims[0]
        assert claim.state == "COMPLETED"
        assert claim.bridge_turn_id == authority.bridge_turn_id
        assert claim.model_run_id == run_id
        assert claim.application_request_sha256 == application_request_sha256(
            FrozenWireRequest(
                method=request.method.encode("ascii"),
                target=request.url.raw_path,
                headers=tuple(request.headers.raw),
                body=request.content,
            )
        )
        assert api_key not in claim.application_request_sha256
        persisted_columns = {
            column.name: getattr(claim, column.name)
            for column in ExternalEffectClaimRow.__table__.columns
        }
        assert api_key not in repr(persisted_columns)
        assert run is not None and run.submission_state == "result_obtained"
        assert api_key not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bridge_submission_cannot_omit_restricted_prompt_source_reference(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    (
        session_factory,
        engine,
        _investigation_id,
        initial_authority,
        _prepared,
        _store_for_other_family,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    assert initial_authority.bridge_turn_id is not None
    authority = replace(
        initial_authority,
        invocation_id=None,
        invocation_arguments_sha256=None,
    )
    passage = "Synthetic passage restricted from external submission."
    document_id = uuid4()
    prompt = f"Summarize source_document {document_id}: {passage}"
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    request_id = "bridge-request-1"
    sent: list[httpx.Request] = []
    now = datetime.now(UTC)

    async with session_factory() as session:
        async with session.begin():
            turn = await session.get(ModelConversationTurnRow, authority.bridge_turn_id)
            run = await session.get(ModelRunRow, run_id)
            blob = await session.scalar(select(BlobRow).order_by(BlobRow.id).limit(1))
            assert turn is not None and run is not None and blob is not None
            conversation = await session.get(ModelConversationRow, turn.conversation_id)
            assert conversation is not None
            run.authorized_input_hash = prompt_sha256
            turn.input_blob_reference = f"sha256:{prompt_sha256}"
            turn.input_sha256 = prompt_sha256
            session.add(
                SourceDocumentRow(
                    id=document_id,
                    subject_id=conversation.subject_id,
                    blob_id=blob.id,
                    original_name="restricted-synthetic-source.txt",
                    origin="test",
                    acquired_at=now,
                    license_restriction=None,
                    tlp="RED",
                    do_not_submit=False,
                    external_llm_allowed=True,
                    created_at=now,
                )
            )

    # This caller-supplied binding has valid authority and model lineage, but
    # omits the source_document ref for the restricted passage in the prompt.
    binding = BridgeExternalEffectBinding(
        authority=authority,
        family="bridge.investigation_turn.v1",
        provider_scope="bridge.responses",
        normalizer_version="bridge.turn.v1",
        evidence_refs=(),
        bridge_turn_id=authority.bridge_turn_id,
        model_run_id=run_id,
        model_input_sha256=prompt_sha256,
        authorized_input_hash=prompt_sha256,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["input"] == prompt
        sent.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"id":"resp-synthetic","status":"completed","output_text":"ok"}',
        )

    transport = create_coordinated_http_responses_transport(
        "https://bridge.synthetic/v1",
        api_key=None,
        coordinator=create_external_effect_coordinator(session_factory),
        http_transport=httpx.MockTransport(handler),
    )
    refusal: BridgeTransportError | None = None
    try:
        try:
            await transport.create(
                {"model": "chatgpt-web", "input": prompt},
                idempotency_key=request_id,
                bridge_external_effect_binding=binding,
            )
        except BridgeTransportError as exc:
            refusal = exc

        assert refusal is not None, "restricted passage without a source ref was dispatched"
        assert refusal.code == "external_effect_authority_required"
        assert sent == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference_kind", "observation_object_type", "restriction"),
    [
        ("sample", None, None),
        ("sample_association_sample", None, None),
        ("sample_association", None, None),
        ("sample_hash", None, None),
        ("source_document", None, None),
        ("evidence_observation", "tool_result", None),
        ("evidence_observation", "source_document", None),
        ("sample", None, "sample_red"),
        ("sample", None, "sample_license"),
        ("source_document", None, "document_red"),
        ("source_document", None, "document_license"),
        ("sample", None, "envelope_do_not_submit"),
        ("sample", None, "envelope_external_llm_denied"),
    ],
)
async def test_bridge_source_rules_ignore_submission_flags_but_keep_red_and_license_vetoes(
    migrated_postgres_url: str,
    tmp_path: Path,
    reference_kind: str,
    observation_object_type: str | None,
    restriction: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        session_factory,
        engine,
        investigation_id,
        initial_authority,
        initial_prepared,
        _unused_store,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority = replace(
        initial_authority,
        invocation_id=None,
        invocation_arguments_sha256=None,
        expected_envelope_version=5,
    )
    sample_id = uuid4()
    association_id = uuid4()
    document_id = uuid4()
    observation_id = uuid4()
    sample_sha256 = "9" * 64
    observation_version = "source-v1"
    sample_license_text = "sample license restriction canary"
    document_license_text = "document license restriction canary"
    now = datetime.now(UTC)
    try:
        async with session_factory() as session:
            async with session.begin():
                envelope = await session.get(InvestigationEnvelopeRow, investigation_id)
                assert envelope is not None
                envelope.disclosure_do_not_submit = restriction == "envelope_do_not_submit"
                envelope.disclosure_external_llm_allowed = (
                    restriction != "envelope_external_llm_denied"
                )
                envelope.version = 5
                turn = await session.get(ModelConversationTurnRow, authority.bridge_turn_id)
                run = await session.get(ModelRunRow, run_id)
                blob = await session.scalar(select(BlobRow).order_by(BlobRow.id).limit(1))
                assert turn is not None and run is not None and blob is not None
                sample_sha256 = blob.sha256
                conversation = await session.get(ModelConversationRow, turn.conversation_id)
                assert conversation is not None
                session.add(
                    SampleRow(
                        id=sample_id,
                        subject_id=conversation.subject_id,
                        blob_id=blob.id,
                        original_name="bridge-permission-flag-sample.bin",
                        origin="test",
                        acquired_at=now,
                        license_restriction=(
                            sample_license_text if restriction == "sample_license" else None
                        ),
                        tlp="RED" if restriction == "sample_red" else "CLEAR",
                        do_not_submit=True,
                        external_llm_allowed=False,
                        origin_kind="manual",
                        state="validated",
                        source_service=None,
                        source_object_id=sample_sha256,
                        expected_hash=sample_sha256,
                        validation_actor=None,
                        validation_date=None,
                        validation_reason=None,
                        imphash=None,
                        ssdeep=None,
                        tlsh=None,
                        rich_header_hash=None,
                        vhash=None,
                        main_icon_dhash=None,
                        imphash_source=None,
                        ssdeep_source=None,
                        tlsh_source=None,
                        rich_header_hash_source=None,
                        vhash_source=None,
                        main_icon_dhash_source=None,
                        created_at=now,
                    )
                )
                session.add(
                    SampleAssociationRow(
                        id=association_id,
                        investigation_id=investigation_id,
                        sample_id=sample_id,
                        sample_sha256=sample_sha256,
                        origin="manual",
                    )
                )
                session.add(
                    SourceDocumentRow(
                        id=document_id,
                        subject_id=conversation.subject_id,
                        blob_id=blob.id,
                        original_name="bridge-permission-flag-source.txt",
                        origin="test",
                        acquired_at=now,
                        license_restriction=(
                            document_license_text if restriction == "document_license" else None
                        ),
                        tlp="RED" if restriction == "document_red" else "CLEAR",
                        do_not_submit=True,
                        external_llm_allowed=False,
                        created_at=now,
                    )
                )
                if reference_kind == "evidence_observation":
                    session.add(
                        EvidenceObservationRow(
                            id=observation_id,
                            investigation_id=investigation_id,
                            kind="other",
                            blob_id=blob.id,
                            sha256=blob.sha256,
                            source="test.source",
                            source_version=observation_version,
                            observed_at=now,
                            object_type=observation_object_type or "tool_result",
                            object_id=(
                                str(document_id)
                                if observation_object_type == "source_document"
                                else "synthetic-tool-result"
                            ),
                            limitations=[],
                            invocation_ref=None,
                        )
                    )
                if reference_kind in {"sample", "sample_association_sample"}:
                    source_ref = EvidenceReference(
                        kind=reference_kind,
                        id=(
                            str(association_id)
                            if reference_kind == "sample_association_sample"
                            else str(sample_id)
                        ),
                        version=(
                            "1" if reference_kind == "sample_association_sample" else "sample.v1"
                        ),
                        sha256=sample_sha256,
                    )
                elif reference_kind == "sample_association":
                    source_ref = EvidenceReference(
                        kind=reference_kind,
                        id=str(association_id),
                        version="association.v1",
                        sha256=sample_sha256,
                    )
                elif reference_kind == "sample_hash":
                    source_ref = EvidenceReference(
                        kind=reference_kind,
                        id=sample_sha256,
                        version="sample-hash.v1",
                        sha256=sample_sha256,
                    )
                elif reference_kind == "source_document":
                    source_ref = EvidenceReference(
                        kind=reference_kind,
                        id=str(document_id),
                        version=f"blob:{blob.id}",
                        sha256=blob.sha256,
                    )
                else:
                    source_ref = EvidenceReference(
                        kind=reference_kind,
                        id=str(observation_id),
                        version=observation_version,
                        sha256=blob.sha256,
                    )
                run.parameters = {
                    BRIDGE_SOURCE_PROVENANCE_KEY: {
                        "family": initial_prepared.family,
                        "authorized_input_hash": run.authorized_input_hash,
                        "turn_input_sha256": turn.input_sha256,
                        "evidence_refs": [source_ref.to_json()],
                    }
                }

        prepared = PreparedExternalRequest.build(
            family=initial_prepared.family,
            provider=initial_prepared.provider,
            provider_scope="bridge.responses",
            normalizer_version=initial_prepared.normalizer_version,
            canonical_request=initial_prepared.canonical_request,
            application_request=initial_prepared.application_request,
            evidence_refs=(source_ref,),
        )
        transport = _CountingTransport()
        outcome = await create_external_effect_coordinator(session_factory).send_one(
            authority, prepared, transport
        )
        if restriction is not None:
            assert isinstance(outcome, Refused)
            expected_reason = {
                "envelope_do_not_submit": "do_not_submit",
                "envelope_external_llm_denied": "external_llm_not_allowed",
            }.get(
                restriction,
                "sample_source_restricted"
                if restriction.startswith("sample_")
                else "source_document_restricted",
            )
            assert outcome.reason == expected_reason
            assert transport.calls == 0
            assert sample_license_text not in repr(outcome)
            assert document_license_text not in repr(outcome)
            assert sample_license_text not in caplog.text
            assert document_license_text not in caplog.text
            restriction_text = (
                sample_license_text
                if restriction == "sample_license"
                else document_license_text
                if restriction == "document_license"
                else None
            )
            if restriction_text is not None:
                assert restriction_text not in repr(outcome)
                assert restriction_text not in caplog.text
            async with session_factory() as session:
                claim_count = await session.scalar(
                    select(func.count())
                    .select_from(ExternalEffectClaimRow)
                    .where(ExternalEffectClaimRow.investigation_id == investigation_id)
                )
            assert claim_count == 0
            return
        assert isinstance(outcome, Completed)
        assert transport.calls == 1
        async with session_factory() as session:
            claims = list(
                await session.scalars(
                    select(ExternalEffectClaimRow).where(
                        ExternalEffectClaimRow.investigation_id == investigation_id,
                        ExternalEffectClaimRow.family == prepared.family,
                    )
                )
            )
        assert len(claims) == 1
        assert claims[0].state == "COMPLETED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bridge_rate_limit_retry_and_correction_consume_one_claim_per_persisted_attempt(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    (
        session_factory,
        engine,
        investigation_id,
        initial_authority,
        initial_prepared,
        _unused_store,
        first_run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    first_turn_id = initial_authority.bridge_turn_id
    assert first_turn_id is not None
    authority = replace(
        initial_authority,
        invocation_id=None,
        invocation_arguments_sha256=None,
    )
    coordinator = ExternalEffectCoordinator(_bridge_model_lineage_store(session_factory))
    first_transport = _CountingTransport(
        response=TransportResponse(outcome_code="bridge_rate_limited")
    )
    second_transport = _CountingTransport()
    third_transport = _CountingTransport()
    second_turn_id = uuid4()
    second_run_id = uuid4()
    second_request_id = "bridge-request-retry-2"
    second_input_sha256 = "c" * 64
    third_turn_id = uuid4()
    third_run_id = uuid4()
    third_request_id = "bridge-request-correction-3"
    third_input_sha256 = "d" * 64
    now = datetime.now(UTC)

    async def persist_next_attempt(
        previous_turn_id: UUID,
        previous_run_id: UUID,
        *,
        previous_turn_status: str,
        previous_run_status: str,
        turn_id: UUID,
        run_id: UUID,
        request_id: str,
        input_sha256: str,
    ) -> None:
        async with session_factory() as session:
            async with session.begin():
                previous_turn = await session.get(ModelConversationTurnRow, previous_turn_id)
                previous_run = await session.get(ModelRunRow, previous_run_id)
                assert previous_turn is not None and previous_run is not None
                conversation = await session.get(
                    ModelConversationRow, previous_turn.conversation_id
                )
                assert conversation is not None
                previous_turn.status = previous_turn_status
                previous_turn.finished_at = now
                previous_run.status = previous_run_status
                previous_run.finished_at = now

                run_values = {
                    column.name: getattr(previous_run, column.name)
                    for column in ModelRunRow.__table__.columns
                }
                run_values.update(
                    {
                        "id": run_id,
                        "authorized_input_hash": input_sha256,
                        "status": "running",
                        "submission_state": "external_state_unknown",
                        "submission_attempt": 1,
                        "bridge_request_id": request_id,
                        "response_id": None,
                        "output_references": [],
                        "error_code": None,
                        "error_message": None,
                        "error_details": None,
                        "raw_output_reference": None,
                        "raw_output_sha256": None,
                        "raw_output_chars": None,
                        "normalized_output_reference": None,
                        "normalized_output_sha256": None,
                        "duration_ms": None,
                        "usage": None,
                        "finished_at": None,
                        "updated_at": now,
                        "started_at": now,
                    }
                )
                session.add(ModelRunRow(**run_values))
                await session.flush()

                turn_values = {
                    column.name: getattr(previous_turn, column.name)
                    for column in ModelConversationTurnRow.__table__.columns
                }
                turn_values.update(
                    {
                        "id": turn_id,
                        "sequence": previous_turn.sequence + 1,
                        "parent_turn_id": previous_turn.id,
                        "model_run_id": run_id,
                        "input_blob_reference": "sha256:" + input_sha256,
                        "input_sha256": input_sha256,
                        "output_blob_reference": None,
                        "output_sha256": None,
                        "status": "running",
                        "external_turn_id": None,
                        "idempotency_key": request_id,
                        "correlation_id": request_id,
                        "error_code": None,
                        "error_message": None,
                        "error_details": None,
                        "created_at": now,
                        "started_at": now,
                        "finished_at": None,
                    }
                )
                session.add(ModelConversationTurnRow(**turn_values))
                conversation.turn_count += 1
                conversation.version += 1
                conversation.updated_at = now

    try:
        first = await coordinator.send_one(authority, initial_prepared, first_transport)
        assert isinstance(first, Completed)
        assert first.outcome_code == "bridge_rate_limited"
        await persist_next_attempt(
            first_turn_id,
            first_run_id,
            previous_turn_status="failed",
            previous_run_status="failed",
            turn_id=second_turn_id,
            run_id=second_run_id,
            request_id=second_request_id,
            input_sha256=second_input_sha256,
        )
        second_authority = replace(
            authority,
            bridge_turn_id=second_turn_id,
            model_run_id=second_run_id,
        )
        second_prepared = PreparedExternalRequest.build(
            family="bridge.investigation_turn.v1",
            provider="bridge",
            provider_scope="bridge-test",
            normalizer_version="bridge.turn.v1",
            canonical_request={
                "model_input_sha256": second_input_sha256,
                "authorized_input_hash": second_input_sha256,
                "bridge_request_id": second_request_id,
            },
            application_request=FrozenWireRequest(
                method=b"POST",
                target=b"/responses",
                headers=((b"content-type", b"application/json"),),
                body=b'{"input":"corrected synthetic request"}',
            ),
        )
        retry = await coordinator.send_one(second_authority, second_prepared, second_transport)
        assert isinstance(retry, Completed)
        assert first.claim_id != retry.claim_id
        await persist_next_attempt(
            second_turn_id,
            second_run_id,
            previous_turn_status="succeeded",
            previous_run_status="succeeded",
            turn_id=third_turn_id,
            run_id=third_run_id,
            request_id=third_request_id,
            input_sha256=third_input_sha256,
        )
        correction_authority = replace(
            authority,
            bridge_turn_id=third_turn_id,
            model_run_id=third_run_id,
        )
        correction_prepared = PreparedExternalRequest.build(
            family="bridge.investigation_turn.v1",
            provider="bridge",
            provider_scope="bridge-test",
            normalizer_version="bridge.turn.v1",
            canonical_request={
                "model_input_sha256": third_input_sha256,
                "authorized_input_hash": third_input_sha256,
                "bridge_request_id": third_request_id,
            },
            application_request=FrozenWireRequest(
                method=b"POST",
                target=b"/responses",
                headers=((b"content-type", b"application/json"),),
                body=b'{"input":"corrective synthetic request"}',
            ),
        )
        correction = await coordinator.send_one(
            correction_authority, correction_prepared, third_transport
        )
        assert isinstance(correction, Completed)
        assert len({first.claim_id, retry.claim_id, correction.claim_id}) == 3
        assert first_transport.calls == second_transport.calls == third_transport.calls == 1
        async with session_factory() as session:
            async with session.begin():
                final_turn = await session.get(ModelConversationTurnRow, third_turn_id)
                final_run = await session.get(ModelRunRow, third_run_id)
                assert final_turn is not None and final_run is not None
                final_turn.status = "succeeded"
                final_turn.finished_at = now
                final_run.status = "succeeded"
                final_run.finished_at = now
            claims = list(
                await session.scalars(
                    select(ExternalEffectClaimRow)
                    .where(
                        ExternalEffectClaimRow.investigation_id == investigation_id,
                        ExternalEffectClaimRow.family == "bridge.investigation_turn.v1",
                    )
                    .order_by(ExternalEffectClaimRow.created_at, ExternalEffectClaimRow.id)
                )
            )
        assert len(claims) == 3
        assert {claim.model_run_id for claim in claims} == {
            first_run_id,
            second_run_id,
            third_run_id,
        }
        assert {claim.bridge_turn_id for claim in claims} == {
            first_turn_id,
            second_turn_id,
            third_turn_id,
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_osint_open_inherited_advisor_bridge_claim_and_revocation_gate(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = FilesystemBlobStore(tmp_path / f"osint-advisor-{uuid4().hex}")
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=f"osint-advisor-{uuid4().hex[:12]}",
        include_seed=True,
    )
    policy_service = OsintOpenPolicyService(uow_factory, Settings(_env_file=None))
    policy = await policy_service.activate(
        scope_kind=OsintScopeKind.SUBJECT_SET,
        scope_ref=str(seeded.subject.id),
        capabilities=OsintCapabilities(
            allowed_providers=("bridge",),
            budget_limits={
                BudgetDimension.VT_UNITS: 0,
                BudgetDimension.SHODAN_CREDITS: 0,
                BudgetDimension.DOWNLOADS: 0,
                BudgetDimension.BRIDGE_TURNS: 4,
                BudgetDimension.TOOL_SECONDS: 120,
                BudgetDimension.CONTEXT_BYTES: 1024,
                BudgetDimension.CONTEXT_TOKENS: 256,
            },
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.CLEAR,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            max_effect_claims_per_investigation=10,
        ),
        actor_id="human:s3-advisor-integration",
        reason="Authorize a synthetic inherited Bridge advisor consultation.",
        correlation_id=f"s3-advisor-policy:{seeded.subject.id}:{uuid4()}",
    )

    async with uow_factory() as uow:
        investigation = await uow.analyst_investigations.get(seeded.investigation.id)
        assert investigation is not None
        inherited_envelope = await policy_service.ensure_inherited_envelope(
            uow,
            investigation=investigation,
            snapshot=SimpleNamespace(
                subject_id=seeded.subject.id,
                tlp=TLPLevel.CLEAR,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            at_start=True,
        )
        assert inherited_envelope is not None
        await uow.commit()

    now = datetime.now(UTC)
    conversation_id: UUID | None = None
    first_invocation_id = uuid4()
    first_operation_key = f"advisor-osint-first:{uuid4()}"
    second_invocation_id = uuid4()
    second_operation_key = f"advisor-osint-second:{uuid4()}"
    assert seeded.seed_observation is not None
    source_document_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            session.add(
                SourceDocumentRow(
                    id=source_document_id,
                    subject_id=seeded.subject.id,
                    blob_id=seeded.seed_observation.blob_id,
                    original_name="synthetic-advisor-source.txt",
                    origin="test",
                    acquired_at=now,
                    license_restriction=None,
                    tlp="CLEAR",
                    do_not_submit=False,
                    external_llm_allowed=True,
                    created_at=now,
                )
            )
            observation = await session.get(EvidenceObservationRow, seeded.seed_observation.id)
            assert observation is not None
            observation.object_type = "source_document"
            observation.object_id = str(source_document_id)
    evidence_refs = (
        EvidenceReference(
            kind="evidence_observation",
            id=str(seeded.seed_observation.id),
            version=seeded.seed_observation.source_version,
            sha256=seeded.seed_observation.sha256,
        ),
    )
    arguments = {
        "consultation_type": "draft_text",
        "question": "Synthetic advisor request.",
        "evidence_refs": [str(seeded.seed_observation.id)],
    }
    reservation_dimensions = {
        BudgetDimension.BRIDGE_TURNS: 1,
        BudgetDimension.TOOL_SECONDS: 10,
        BudgetDimension.CONTEXT_BYTES: 128,
        BudgetDimension.CONTEXT_TOKENS: 32,
    }
    reservation_json = {key.value: value for key, value in reservation_dimensions.items()}
    fake_bridge_adapter = _SyntheticOpenAIBridgeAdapter()
    model_gateway = ModelGateway(
        ModelRouter(
            openai_research=fake_bridge_adapter,
            openai_structured=fake_bridge_adapter,
            qwen=FakeModelAdapter(),
            fake=FakeModelAdapter(),
        ),
        uow_factory,
        BlobModelOutputStore(BlobCatalogService(blob_store, uow_factory)),
    )
    model_conversations = ModelConversationService(uow_factory, model_gateway, blob_store)

    async def persist_attempt(
        *,
        index: int,
        operation_key: str,
        invocation_id: UUID,
    ) -> tuple[ExternalEffectAuthority, PreparedExternalRequest]:
        nonlocal conversation_id
        async with uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
            assert envelope is not None
            envelope.budget.reserve(operation_key, reservation_dimensions)
            reserved_envelope = replace(envelope, version=envelope.version + 1)
            await uow.investigation_envelopes.save_if_version(
                reserved_envelope, expected_version=envelope.version
            )
            await uow.commit()
        assert reserved_envelope.approval_decision_id is not None

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(InvestigationBudgetRecordRow)
                    .values(
                        investigation_id=seeded.investigation.id,
                        version=1,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[InvestigationBudgetRecordRow.investigation_id]
                    )
                )
                await session.execute(update_investigation_status(seeded.investigation.id))
                session.add(
                    ToolInvocationRow(
                        id=invocation_id,
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        tool_name="advisor.consult",
                        schema_version=ADVISOR_SCHEMA_VERSION,
                        normalized_parameters=arguments,
                        purpose="Synthetic inherited Bridge advisor consultation.",
                        status="running",
                        reserved_budget=reservation_json,
                        consumed_budget={},
                        response_refs=[],
                        created_at=now,
                        updated_at=now,
                        version=1,
                    )
                )
                session.add(
                    InvestigationBudgetReservationRow(
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        status="reserved",
                        sequence=index,
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        InvestigationBudgetReservationAmountRow(
                            investigation_id=seeded.investigation.id,
                            operation_key=operation_key,
                            dimension=dimension.value,
                            reserved_amount=amount,
                            committed_amount=None,
                        )
                        for dimension, amount in reservation_dimensions.items()
                    ]
                )

        if conversation_id is None:
            conversation = await model_conversations.create(
                provider=ModelProvider.OPENAI,
                transport=ConversationTransport.CHATGPT_BRIDGE,
                purpose=ConversationPurpose.PIVOT_RESEARCH,
                title="Synthetic inherited advisor conversation",
                edition_id=None,
                subject_id=seeded.subject.id,
                expected_profile=None,
                requested_model="test-model",
            )
            conversation_id = conversation.id
        turn = await model_conversations.add_turn(
            conversation_id,
            message=(
                f"Synthetic advisor request. Evidence observation {seeded.seed_observation.id}."
            ),
            mode=ConversationMode.FRESH if index == 1 else ConversationMode.CONTINUE,
            external_llm_allowed=True,
            stateless_external=True,
            idempotency_key=f"advisor-turn:{uuid4()}",
            correlation_id=f"advisor-correlation:{uuid4()}",
            bridge_external_effect_binding=BridgeExternalEffectBinding(
                authority=ExternalEffectAuthority(
                    investigation_id=seeded.investigation.id,
                    invocation_id=invocation_id,
                    operation_key=operation_key,
                    reservation_amounts=reservation_json,
                    expected_envelope_version=reserved_envelope.version,
                    approval_decision_id=reserved_envelope.approval_decision_id,
                    invocation_arguments_sha256=hashlib.sha256(
                        canonical_json_bytes(arguments)
                    ).hexdigest(),
                    authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
                    policy_event_id=policy.event_id,
                    policy_version=str(policy.version),
                    pinned_subject_id=seeded.subject.id,
                ),
                family="advisor.consultation.v1",
                provider_scope="bridge.responses",
                normalizer_version="advisor.consultation.v1",
                evidence_refs=evidence_refs,
            ),
        )

        async with uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
            run = await uow.model_runs.get(turn.model_run_id)
            assert envelope is not None and run is not None
            assert run.bridge_request_id is not None
            assert conversation_id is not None
            input_sha256 = turn.input_sha256
            request_id = run.bridge_request_id
            run_sha256 = run.authorized_input_hash
            await uow.commit()
        async with session_factory() as session:
            async with session.begin():
                run_row = await session.get(ModelRunRow, run.id)
                turn_row = await session.get(ModelConversationTurnRow, turn.id)
                conversation_row = await session.get(ModelConversationRow, conversation_id)
                assert run_row is not None and turn_row is not None and conversation_row is not None
                run_row.status = "running"
                run_row.submission_state = "external_state_unknown"
                run_row.submission_attempt = 1
                run_row.finished_at = None
                run_row.error_code = None
                run_row.error_message = None
                run_row.error_details = None
                turn_row.status = "running"
                turn_row.finished_at = None
                turn_row.error_code = None
                turn_row.error_message = None
                turn_row.error_details = None
                conversation_row.status = "busy"
                conversation_row.updated_at = now

        authority = ExternalEffectAuthority(
            investigation_id=seeded.investigation.id,
            invocation_id=invocation_id,
            operation_key=operation_key,
            reservation_amounts=reservation_json,
            expected_envelope_version=envelope.version,
            approval_decision_id=envelope.approval_decision_id,
            invocation_arguments_sha256=hashlib.sha256(canonical_json_bytes(arguments)).hexdigest(),
            bridge_turn_id=turn.id,
            model_run_id=run.id,
            authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
            policy_event_id=policy.event_id,
            policy_version=str(policy.version),
            pinned_subject_id=seeded.subject.id,
        )
        prepared = PreparedExternalRequest.build(
            family="advisor.consultation.v1",
            provider="bridge",
            provider_scope="bridge.responses",
            normalizer_version="advisor.consultation.v1",
            canonical_request={
                "authorized_input_hash": run_sha256,
                "bridge_request_id": request_id,
                "model_input_sha256": input_sha256,
            },
            evidence_refs=evidence_refs,
            application_request=FrozenWireRequest(
                method=b"POST",
                target=b"/responses",
                headers=((b"content-type", b"application/json"),),
                body=canonical_json_bytes(
                    {
                        "input": (
                            "Synthetic advisor request. Evidence observation "
                            f"{seeded.seed_observation.id}."
                        )
                    }
                ),
            ),
        )
        return authority, prepared

    try:
        active_authority, active_prepared = await persist_attempt(
            index=1,
            operation_key=first_operation_key,
            invocation_id=first_invocation_id,
        )
        coordinator = create_external_effect_coordinator(session_factory)
        transport = _CountingTransport()
        sent = await coordinator.send_one(active_authority, active_prepared, transport)
        assert isinstance(sent, Completed)
        assert transport.calls == 1

        assert active_authority.bridge_turn_id is not None
        assert active_authority.model_run_id is not None
        async with session_factory() as session:
            async with session.begin():
                active_turn = await session.get(
                    ModelConversationTurnRow, active_authority.bridge_turn_id
                )
                active_run = await session.get(ModelRunRow, active_authority.model_run_id)
                assert active_turn is not None and active_run is not None
                active_conversation = await session.get(
                    ModelConversationRow, active_turn.conversation_id
                )
                assert active_conversation is not None
                active_turn.status = "succeeded"
                active_turn.finished_at = now
                active_run.status = "succeeded"
                active_run.finished_at = now
                active_conversation.status = "ready"
                active_conversation.updated_at = now

        revoked_authority, revoked_prepared = await persist_attempt(
            index=2,
            operation_key=second_operation_key,
            invocation_id=second_invocation_id,
        )
        revoked = await policy_service.revoke(
            policy_id=policy.policy_id,
            expected_version=policy.version,
            actor_id="human:s3-advisor-integration",
            reason="Stop further synthetic Bridge consultations.",
            correlation_id=f"s3-advisor-revoke:{seeded.investigation.id}:{uuid4()}",
        )
        assert revoked.status.value == "revoked"
        refused = await coordinator.send_one(revoked_authority, revoked_prepared, transport)
        assert isinstance(refused, Refused)
        assert transport.calls == 1

        async with uow_factory() as uow:
            human_decisions = await uow.analyst_decisions.list_for_investigation(
                seeded.investigation.id
            )
            persisted_envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
        async with session_factory() as session:
            claims = list(
                await session.scalars(
                    select(ExternalEffectClaimRow).where(
                        ExternalEffectClaimRow.investigation_id == seeded.investigation.id
                    )
                )
            )
        assert human_decisions == []
        assert inherited_envelope.authority_kind.value == "osint_policy"
        assert persisted_envelope is not None
        assert persisted_envelope.authority_kind.value == "osint_policy"
        assert len(claims) == 1
        assert claims[0].invocation_id == first_invocation_id
        assert claims[0].state == "COMPLETED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["timeout", "http_500"])
async def test_bridge_ambiguous_response_needs_review_and_replay_sends_nothing(
    migrated_postgres_url: str,
    tmp_path: Path,
    failure_kind: str,
) -> None:
    (
        session_factory,
        engine,
        _investigation_id,
        initial_authority,
        prepared,
        _unused_store,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority = replace(
        initial_authority,
        invocation_id=None,
        invocation_arguments_sha256=None,
    )
    coordinator = ExternalEffectCoordinator(_bridge_model_lineage_store(session_factory))
    if failure_kind == "http_500":
        request = httpx.Request("POST", "https://bridge.test/v1/responses")
        response = httpx.Response(500, request=request)
        failure = httpx.HTTPStatusError(
            "synthetic post-handoff HTTP 500", request=request, response=response
        )
    else:
        failure = httpx.ReadTimeout("synthetic response timeout")
    transport = _CountingTransport(response=failure)
    try:
        first = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(first, Ambiguous)
        replay = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(replay, Ambiguous)
        assert transport.calls == 1
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, first.claim_id)
            run = await session.get(ModelRunRow, run_id)
        assert claim is not None and claim.state == "NEEDS_REVIEW"
        assert claim.bridge_turn_id == authority.bridge_turn_id
        assert claim.model_run_id == run_id
        assert run is not None
        assert run.status == "needs_review"
        assert run.submission_state == "external_state_unknown"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bridge_attempt_request_id_mismatch_refuses_without_send_or_increment(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    (
        session_factory,
        engine,
        _investigation_id,
        authority,
        prepared,
        store,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url,
        tmp_path,
        suffix=uuid4().hex[:12],
        persisted_request_id="persisted-bridge-request",
        claimed_request_id="different-claim-request",
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "model_run_not_eligible"
        assert transport.calls == 0
        async with session_factory() as session:
            run = await session.get(ModelRunRow, run_id)
            claim_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == authority.investigation_id)
            )
        assert run is not None
        assert run.submission_state == "external_state_unknown"
        assert run.submission_attempt == 1
        assert claim_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_before_bridge_consume_keeps_unknown_attempt_for_reconciliation(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    (
        session_factory,
        engine,
        _investigation_id,
        _authority,
        _prepared,
        _store,
        run_id,
    ) = await _prepare_model_submission_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    try:
        # ModelGateway has durably allocated the attempt; simulate a process
        # crash before the coordinator creates/consumes its claim.
        async with session_factory() as session:
            run = await session.get(ModelRunRow, run_id)
            claim_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.model_run_id == run_id)
            )
        assert run is not None
        assert run.submission_state == "external_state_unknown"
        assert run.submission_attempt == 1
        assert claim_count == 0
    finally:
        await engine.dispose()


def update_investigation_status(investigation_id: UUID):
    from sqlalchemy import update

    return (
        update(AnalystInvestigationRow)
        .where(AnalystInvestigationRow.id == investigation_id)
        .values(status="running")
    )


@pytest.mark.asyncio
async def test_sql_store_seals_real_pretransport_failure_and_rejects_forged_proofs(
    migrated_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    transport = _CountingTransport()
    external_effects = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(
        external_effects, "time", SimpleNamespace(monotonic=lambda: next(ticks, 12.0))
    )
    original_seal = store.definitely_not_submitted
    captured_proofs: list[object] = []

    async def check_untrusted_proofs_before_seal(
        claim_id: UUID, *, proof: object, reason_code: str
    ) -> bool:
        captured_proofs.append(proof)
        assert not await original_seal(uuid4(), proof=proof, reason_code="wrong_claim")
        assert not await original_seal(claim_id, proof=copy.copy(proof), reason_code="copied_proof")
        assert not await original_seal(claim_id, proof=object(), reason_code="forged_proof")
        return await original_seal(claim_id, proof=proof, reason_code=reason_code)

    monkeypatch.setattr(store, "definitely_not_submitted", check_untrusted_proofs_before_seal)
    coordinator = ExternalEffectCoordinator(store)
    try:
        outcome = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, DefinitelyNotSubmitted)
        assert transport.submit_entries == 0
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, outcome.claim_id)
        assert row is not None
        assert row.state == "DEFINITIVELY_NOT_SUBMITTED"
        assert row.handoff_permit_nonce is None
        assert row.investigation_id == investigation_id
        assert len(captured_proofs) == 1
        assert not await original_seal(
            outcome.claim_id, proof=captured_proofs[0], reason_code="replayed_proof"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_coordinator_cannot_seal_claim_with_foreign_proof(
    migrated_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    _coordinator = ExternalEffectCoordinator(store)
    consumed = await store.consume(
        authority,
        prepared,
        effect_key=effect_key(authority.investigation_id, prepared),
        occurrence_digest=occurrence_sha256(prepared.occurrence_id),
    )
    assert isinstance(consumed, ConsumeCommitted), "fixture must create a committed claim"

    foreign_store = _store(session_factory)
    external_effects = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((20.0, 22.0))
    monkeypatch.setattr(
        external_effects, "time", SimpleNamespace(monotonic=lambda: next(ticks, 22.0))
    )

    class _ClaimReplayProbe:
        def __getattr__(self, name: str) -> Any:
            return getattr(foreign_store, name)

        async def consume(self, *args: Any, **kwargs: Any) -> ConsumeCommitted:
            assert isinstance(consumed, ConsumeCommitted), (
                "claim replay probe requires a committed consume result"
            )
            return consumed

        async def needs_review(self, claim_id: UUID, *, reason_code: str) -> bool:
            # Keep the durable row CONSUMED so this test isolates rejection
            # before the definitive-not-submitted CAS.
            return True

        async def definitely_not_submitted(
            self, claim_id: UUID, *, proof: object, reason_code: str
        ) -> bool:
            return await store.definitely_not_submitted(
                claim_id, proof=proof, reason_code=reason_code
            )

    foreign_coordinator = ExternalEffectCoordinator(_ClaimReplayProbe())  # type: ignore[arg-type]
    transport = _CountingTransport()
    try:
        outcome = await foreign_coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, Ambiguous)
        assert outcome.claim_id == consumed.claim_id
        assert transport.submit_entries == 0
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, consumed.claim_id)
        assert row is not None and row.state == "CONSUMED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_connect_error_after_handoff_is_needs_review_and_never_retried(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    failure = httpx.ConnectError("connection could not be established")
    transport = _CountingTransport(response=failure)
    coordinator = ExternalEffectCoordinator(store)
    try:
        first = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(first, Ambiguous)
        assert transport.submit_entries == 1
        assert transport.calls == 1
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, first.claim_id)
        assert row is not None
        assert row.state == "NEEDS_REVIEW"
        assert row.handoff_permit_nonce is not None
        assert row.last_error_code == "transport_outcome_unknown"

        replay = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(replay, Ambiguous)
        assert transport.submit_entries == 1
        assert transport.calls == 1
        assert replay.claim_id == first.claim_id
        async with session_factory() as session:
            claims = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(
                    ExternalEffectClaimRow.investigation_id == authority.investigation_id,
                    ExternalEffectClaimRow.effect_key
                    == effect_key(authority.investigation_id, prepared),
                )
            )
        assert claims == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_complete_429_retry_uses_new_claim_and_replay_does_not_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12], invocations=2
    )
    first_authority, first_base = authorities[0]
    retry_authority, retry_base = authorities[1]
    first_prepared = replace(first_base, occurrence_id=uuid4())
    retry_prepared = replace(retry_base, occurrence_id=uuid4())
    store = _store(session_factory)
    coordinator = ExternalEffectCoordinator(store)
    first_transport = _CountingTransport(
        response=TransportResponse(outcome_code="bridge_rate_limited")
    )
    retry_transport = _CountingTransport()
    try:
        first = await coordinator.send_one(first_authority, first_prepared, first_transport)
        retry = await coordinator.send_one(retry_authority, retry_prepared, retry_transport)
        assert isinstance(first, Completed) and first.outcome_code == "bridge_rate_limited"
        assert isinstance(retry, Completed) and retry.outcome_code is None
        assert first.claim_id != retry.claim_id
        assert first_transport.calls == 1
        assert retry_transport.calls == 1
        async with session_factory() as session:
            rate_claim = await session.get(ExternalEffectClaimRow, first.claim_id)
            claim_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(
                    ExternalEffectClaimRow.investigation_id == investigation_id,
                    ExternalEffectClaimRow.effect_key.in_(
                        [
                            effect_key(investigation_id, first_prepared),
                            effect_key(investigation_id, retry_prepared),
                        ]
                    ),
                )
            )
        assert rate_claim is not None
        assert rate_claim.state == "COMPLETED"
        assert rate_claim.last_error_code == "bridge_rate_limited"
        assert claim_count == 2

        replay = await coordinator.send_one(first_authority, first_prepared, first_transport)
        assert isinstance(replay, Completed)
        assert replay.outcome_code == "bridge_rate_limited"
        assert first_transport.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postwrite_timeout_seals_needs_review_and_never_resends(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    transport = _CountingTransport(response=httpx.ReadTimeout("response timed out"))
    coordinator = ExternalEffectCoordinator(store)
    try:
        first = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(first, Ambiguous)
        assert transport.calls == 1
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, first.claim_id)
        assert row is not None and row.state == "NEEDS_REVIEW"
        replay = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(replay, Ambiguous)
        assert transport.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_credential_redacted_digest_is_the_only_wire_digest_persisted(
    migrated_postgres_url: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    api_key = "ephemeral-api-key-value-do-not-persist"
    wire = FrozenWireRequest(
        method=b"POST",
        target=(
            b"/responses?api_key=first&access_key=first&x_api_key=first&hash=abc"
            b"#refresh_token=first"
        ),
        headers=(
            (b"Authorization", b"Bearer " + api_key.encode()),
            (b"x-api-key", api_key.encode()),
            (b"Api-Key", api_key.encode()),
            (b"X-Auth-Token", api_key.encode()),
            (b"X-Whatever-Secret", api_key.encode()),
        ),
        body=b'{"input":"offline fake"}',
    )
    changed_credentials = FrozenWireRequest(
        method=wire.method,
        target=wire.target.replace(b"first", b"second"),
        headers=tuple(
            (name, value.replace(b"first", b"second").replace(api_key.encode(), b"another-key"))
            for name, value in wire.headers
        ),
        body=wire.body,
    )
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=wire,
        occurrence_id=uuid4(),
    )
    store = _store(session_factory)
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(outcome, Completed)
        assert transport.calls == 1
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, outcome.claim_id)
        assert row is not None
        persisted = repr(
            {
                "digest": row.application_request_sha256,
                "canonical_request": row.canonical_request,
                "request_ref": row.request_ref,
                "evidence_refs": row.evidence_refs,
                "last_error_code": row.last_error_code,
            }
        )
        assert row.application_request_sha256 == application_request_sha256(wire)
        assert row.application_request_sha256 == application_request_sha256(changed_credentials)
        assert row.application_request_sha256 != _wire_request_sha256(wire)
        assert api_key not in persisted
        assert _wire_request_sha256(wire) not in persisted
        assert api_key not in caplog.text
        assert _wire_request_sha256(wire) not in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_refuses_committed_external_effect_rows(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    await asyncio.to_thread(
        command.upgrade,
        _alembic_config(temporary_postgres_url),
        "head",
    )
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        temporary_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    try:
        consumed = await store.consume(
            authority,
            prepared,
            effect_key=effect_key(investigation_id, prepared),
            occurrence_digest=occurrence_sha256(prepared.occurrence_id),
        )
        assert isinstance(consumed, ConsumeCommitted)
        await asyncio.to_thread(
            command.downgrade,
            _alembic_config(temporary_postgres_url),
            "0004_external_effect_claims",
        )
        with pytest.raises(RuntimeError, match="refusing to drop external effect"):
            await asyncio.to_thread(
                command.downgrade,
                _alembic_config(temporary_postgres_url),
                "0003_osint_open_policy",
            )
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, consumed.claim_id)
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert claim is not None and claim.state == "CONSUMED"
        assert version == "0004_external_effect_claims"
    finally:
        await engine.dispose()


def _store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    policy_guard: Any = None,
    credential_parameter_names: frozenset[str] = frozenset(),
    model_submission: bool = False,
) -> SqlAlchemyExternalEffectClaimStore:
    return SqlAlchemyExternalEffectClaimStore(
        session_factory,
        policy_guard=policy_guard,
        family_bindings={
            _FAMILY: ExternalEffectFamilyBinding(
                provider=_PROVIDER,
                provider_scopes=frozenset({"tenant-test"}),
                normalizer_versions=frozenset({_NORMALIZER}),
                invocation_tool_binding=(_FAMILY, "v1"),
                model_submission=model_submission,
                credential_parameter_names=credential_parameter_names,
            )
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_parameter", ["key", "apikey"])
async def test_family_declared_key_parameter_is_required_before_submission(
    migrated_postgres_url: str, tmp_path: Path, credential_parameter: str
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    wire = FrozenWireRequest(
        method=b"GET",
        target=f"/v1/report?hash=abc&{credential_parameter}=credential-probe".encode(),
        headers=((b"accept", b"application/json"),),
        body=b"",
    )
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=wire,
        credential_parameter_names=(credential_parameter,),
    )
    unconfigured_transport = _CountingTransport()
    try:
        refused = await ExternalEffectCoordinator(_store(session_factory)).send_one(
            authority, prepared, unconfigured_transport
        )
        assert isinstance(refused, Refused)
        assert refused.reason == "family_credential_declaration_mismatch"
        assert unconfigured_transport.calls == 0

        configured_transport = _CountingTransport()
        accepted = await ExternalEffectCoordinator(
            _store(
                session_factory,
                credential_parameter_names=frozenset({credential_parameter}),
            )
        ).send_one(authority, prepared, configured_transport)
        assert isinstance(accepted, Completed)
        assert configured_transport.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_declared_query_credential_in_canonical_request_is_rejected_before_persist(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, _authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    try:
        with pytest.raises(ExternalEffectValidationError, match=r"canonical_request\.key"):
            PreparedExternalRequest.build(
                family=_FAMILY,
                provider=_PROVIDER,
                provider_scope="tenant-test",
                normalizer_version=_NORMALIZER,
                canonical_request={"hash": "abc", "key": "SECRET"},
                application_request=FrozenWireRequest(
                    method=b"GET",
                    target=b"/v1/report?hash=abc&key=SECRET",
                    headers=((b"accept", b"application/json"),),
                    body=b"",
                ),
                credential_parameter_names=("key",),
            )
        async with session_factory() as session:
            claim_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
        assert claim_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restriction", "expected_reason", "family"),
    [
        ("tlp", "source_document_restricted", _FAMILY),
        ("external_llm_allowed", "source_document_external_llm_not_allowed", _FAMILY),
        ("do_not_submit", None, _FAMILY),
        ("clear", None, _FAMILY),
        pytest.param(
            "do_not_submit",
            "source_document_restricted",
            _NON_BRIDGE_MODEL_UPLOAD_FAMILY,
            id="non-bridge-model-upload-do-not-submit",
        ),
    ],
)
@pytest.mark.parametrize("reference_kind", ["evidence_observation", "source_document"])
async def test_source_document_restrictions_are_checked_at_consume(
    migrated_postgres_url: str,
    tmp_path: Path,
    restriction: str,
    expected_reason: str | None,
    reference_kind: str,
    family: str,
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    provider = _PROVIDER if family == _FAMILY else "openai"
    provider_scope = "tenant-test" if family == _FAMILY else "model-test"
    normalizer_version = _NORMALIZER if family == _FAMILY else "openai.external-upload.v1"
    observation_id = uuid4()
    observation_sha256 = "c" * 64
    observation_version = "source-v1"
    async with session_factory() as session:
        async with session.begin():
            if provider != _PROVIDER:
                envelope = await session.get(InvestigationEnvelopeRow, investigation_id)
                assert envelope is not None
                envelope.allowed_providers = [provider]
            blob = await session.scalar(select(BlobRow).order_by(BlobRow.id).limit(1))
            assert blob is not None
            document = SourceDocumentRow(
                id=uuid4(),
                subject_id=subject_id,
                blob_id=blob.id,
                original_name="external-effects-source.txt",
                origin="test",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp="CLEAR",
                do_not_submit=False,
                external_llm_allowed=True,
                logical_filename=None,
                source_collection_id=None,
                source_candidate_id=None,
                decoded_blob_id=None,
                title="Test source",
                publisher=None,
                published_at=None,
                final_url=None,
                declared_mime_type=None,
                detected_mime_type=None,
                encoded_sha256=None,
                decoded_sha256=None,
                encoded_size=None,
                decoded_size=None,
                created_at=datetime.now(UTC),
            )
            session.add(document)
            await session.flush()
            if restriction == "tlp":
                document.tlp = "RED"
            elif restriction == "external_llm_allowed":
                document.external_llm_allowed = False
            elif restriction == "do_not_submit":
                document.do_not_submit = True
            document_id = document.id
            document_blob_id = document.blob_id
            document_blob_sha256 = blob.sha256
            session.add(
                EvidenceObservationRow(
                    id=observation_id,
                    investigation_id=investigation_id,
                    kind="other",
                    blob_id=document.blob_id,
                    sha256=observation_sha256,
                    source="test.source",
                    source_version=observation_version,
                    observed_at=datetime.now(UTC),
                    object_type="source_document",
                    object_id=str(document.id),
                    limitations=[],
                    invocation_ref=None,
                )
            )
    reference = (
        EvidenceReference(
            kind="evidence_observation",
            id=str(observation_id),
            version=observation_version,
            sha256=observation_sha256,
        )
        if reference_kind == "evidence_observation"
        else EvidenceReference(
            kind="source_document",
            id=str(document_id),
            version=f"blob:{document_blob_id}",
            sha256=document_blob_sha256,
        )
    )
    prepared = PreparedExternalRequest.build(
        family=family,
        provider=provider,
        provider_scope=provider_scope,
        normalizer_version=normalizer_version,
        canonical_request=(
            base.canonical_request
            if family == _FAMILY
            else {"artifact": "synthetic-source-document"}
        ),
        application_request=(
            base.application_request
            if family == _FAMILY
            else FrozenWireRequest(
                method=b"POST",
                target=b"/v1/external-upload",
                headers=((b"content-type", b"application/json"),),
                body=b'{"artifact":"synthetic-source-document"}',
            )
        ),
        evidence_refs=(reference,),
    )
    transport = _CountingTransport()
    store = (
        _store(session_factory, model_submission=True)
        if family == _FAMILY
        else SqlAlchemyExternalEffectClaimStore(
            session_factory,
            family_bindings={
                family: ExternalEffectFamilyBinding(
                    provider=provider,
                    provider_scopes=frozenset({provider_scope}),
                    normalizer_versions=frozenset({normalizer_version}),
                    invocation_tool_binding=(_FAMILY, "v1"),
                    model_submission=True,
                )
            },
        )
    )
    try:
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        if expected_reason is None:
            assert isinstance(outcome, Completed)
            assert transport.calls == 1
        else:
            assert isinstance(outcome, Refused)
            assert outcome.reason == expected_reason
            assert transport.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unresolvable_evidence_observation_refuses_model_submission(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=base.application_request,
        evidence_refs=(
            EvidenceReference(
                kind="evidence_observation",
                id=str(uuid4()),
                version="source-v1",
                sha256="d" * 64,
            ),
        ),
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(
            _store(session_factory, model_submission=True)
        ).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "evidence_observation_missing"
        assert transport.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_observation_hash_with_null_sample_association_refuses_model_submission(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    sample_sha256 = "c" * 64
    observation_id = uuid4()
    observation_sha256 = "d" * 64
    observation_version = "source-v1"
    async with session_factory() as session:
        async with session.begin():
            blob = await session.scalar(select(BlobRow).order_by(BlobRow.id).limit(1))
            assert blob is not None
            session.add(
                SampleAssociationRow(
                    id=uuid4(),
                    investigation_id=investigation_id,
                    sample_id=None,
                    sample_sha256=sample_sha256,
                    origin="manual",
                )
            )
            session.add(
                EvidenceObservationRow(
                    id=observation_id,
                    investigation_id=investigation_id,
                    kind="other",
                    blob_id=blob.id,
                    sha256=observation_sha256,
                    source="test.source",
                    source_version=observation_version,
                    observed_at=datetime.now(UTC),
                    object_type="file",
                    object_id=sample_sha256,
                    limitations=[],
                    invocation_ref=None,
                )
            )
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=base.application_request,
        evidence_refs=(
            EvidenceReference(
                kind="evidence_observation",
                id=str(observation_id),
                version=observation_version,
                sha256=observation_sha256,
            ),
        ),
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(
            _store(session_factory, model_submission=True)
        ).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "sample_hash_unresolvable"
        assert transport.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_evidence_observation_resolves_restricted_sample_hash_at_consume(
    migrated_postgres_url: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    sample_id = uuid4()
    sample_sha256 = "f" * 64
    restriction_text = "license restriction canary must not persist"
    observation_id = uuid4()
    observation_sha256 = "e" * 64
    observation_version = "source-v1"
    async with session_factory() as session:
        async with session.begin():
            blob = await session.scalar(select(BlobRow).order_by(BlobRow.id).limit(1))
            assert blob is not None
            session.add(
                SampleRow(
                    id=sample_id,
                    subject_id=subject_id,
                    blob_id=blob.id,
                    original_name="restricted-sample.bin",
                    origin="test",
                    acquired_at=datetime.now(UTC),
                    license_restriction=restriction_text,
                    tlp="CLEAR",
                    do_not_submit=False,
                    external_llm_allowed=True,
                    origin_kind="manual",
                    state="validated",
                    source_service=None,
                    source_object_id=None,
                    expected_hash=sample_sha256,
                    validation_actor=None,
                    validation_date=None,
                    validation_reason=None,
                    imphash=None,
                    ssdeep=None,
                    tlsh=None,
                    rich_header_hash=None,
                    vhash=None,
                    main_icon_dhash=None,
                    imphash_source=None,
                    ssdeep_source=None,
                    tlsh_source=None,
                    rich_header_hash_source=None,
                    vhash_source=None,
                    main_icon_dhash_source=None,
                    created_at=datetime.now(UTC),
                )
            )
            session.add(
                SampleAssociationRow(
                    id=uuid4(),
                    investigation_id=investigation_id,
                    sample_id=sample_id,
                    sample_sha256=sample_sha256,
                    origin="manual",
                )
            )
            session.add(
                EvidenceObservationRow(
                    id=observation_id,
                    investigation_id=investigation_id,
                    kind="other",
                    blob_id=blob.id,
                    sha256=observation_sha256,
                    source="test.source",
                    source_version=observation_version,
                    observed_at=datetime.now(UTC),
                    object_type="file",
                    object_id=sample_sha256,
                    limitations=[],
                    invocation_ref=None,
                )
            )
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=base.application_request,
        evidence_refs=(
            EvidenceReference(
                kind="evidence_observation",
                id=str(observation_id),
                version=observation_version,
                sha256=observation_sha256,
            ),
        ),
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(
            _store(session_factory, model_submission=True)
        ).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "sample_source_restricted"
        assert transport.calls == 0
        assert restriction_text not in repr(outcome)
        assert restriction_text not in caplog.text
        async with session_factory() as session:
            claims = list(
                await session.scalars(
                    select(ExternalEffectClaimRow).where(
                        ExternalEffectClaimRow.investigation_id == investigation_id
                    )
                )
            )
        persisted_claims = [
            {
                column.name: getattr(claim, column.name)
                for column in ExternalEffectClaimRow.__table__.columns
            }
            for claim in claims
        ]
        assert restriction_text not in repr(persisted_claims)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_human_g2_vt_read_permits_acquired_sample_external_llm_flag(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    sample_id = uuid4()
    sample_sha256 = "b" * 64
    observation_id = uuid4()
    observation_sha256 = "e" * 64
    observation_version = "source-v1"
    async with session_factory() as session:
        async with session.begin():
            blob = await session.scalar(select(BlobRow).order_by(BlobRow.id).limit(1))
            assert blob is not None
            session.add(
                SampleRow(
                    id=sample_id,
                    subject_id=subject_id,
                    blob_id=blob.id,
                    original_name="human-g2-restricted-sample.bin",
                    origin="test",
                    acquired_at=datetime.now(UTC),
                    license_restriction=None,
                    tlp="CLEAR",
                    do_not_submit=False,
                    external_llm_allowed=False,
                    origin_kind="manual",
                    state="validated",
                    source_service=None,
                    source_object_id=sample_sha256,
                    expected_hash=sample_sha256,
                    validation_actor=None,
                    validation_date=None,
                    validation_reason=None,
                    imphash=None,
                    ssdeep=None,
                    tlsh=None,
                    rich_header_hash=None,
                    vhash=None,
                    main_icon_dhash=None,
                    imphash_source=None,
                    ssdeep_source=None,
                    tlsh_source=None,
                    rich_header_hash_source=None,
                    vhash_source=None,
                    main_icon_dhash_source=None,
                    created_at=datetime.now(UTC),
                )
            )
            session.add(
                SampleAssociationRow(
                    id=uuid4(),
                    investigation_id=investigation_id,
                    sample_id=sample_id,
                    sample_sha256=sample_sha256,
                    origin="manual",
                )
            )
            session.add(
                EvidenceObservationRow(
                    id=observation_id,
                    investigation_id=investigation_id,
                    kind="other",
                    blob_id=blob.id,
                    sha256=observation_sha256,
                    source="test.source",
                    source_version=observation_version,
                    observed_at=datetime.now(UTC),
                    object_type="file",
                    object_id=sample_sha256,
                    limitations=[],
                    invocation_ref=None,
                )
            )
    source_refs = (
        EvidenceReference("sample_hash", sample_sha256, "sample-hash.v1", sample_sha256),
        EvidenceReference(
            "evidence_observation",
            str(observation_id),
            observation_version,
            observation_sha256,
        ),
    )
    request_path = f"/api/v3/files/{sample_sha256}"
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request={"path": request_path},
        application_request=FrozenWireRequest(
            method=b"GET",
            target=request_path.encode(),
            headers=((b"accept", b"application/json"),),
            body=b"",
        ),
        evidence_refs=source_refs,
    )
    family_binding = ExternalEffectFamilyBinding(
        provider=base.provider,
        provider_scopes=frozenset({base.provider_scope}),
        normalizer_versions=frozenset({base.normalizer_version}),
        invocation_tool_binding=(base.family, "v1"),
        requires_source_refs=True,
        source_reference_resolver=lambda _parameters: source_refs,
    )
    store = SqlAlchemyExternalEffectClaimStore(
        session_factory,
        policy_guard=SqlAlchemyVirusTotalPolicyGuard(),
        family_bindings={base.family: family_binding},
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert not isinstance(outcome, Refused)
        assert transport.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference_kind", "expected_reason"),
    [
        ("sample_association", "sample_association_unresolvable"),
        ("sample_hash", "sample_hash_unresolvable"),
    ],
)
async def test_model_submission_rejects_unresolved_null_sample_association_refs(
    migrated_postgres_url: str,
    tmp_path: Path,
    reference_kind: str,
    expected_reason: str,
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    association_id = uuid4()
    sample_sha256 = "a" * 64
    async with session_factory() as session:
        async with session.begin():
            session.add(
                SampleAssociationRow(
                    id=association_id,
                    investigation_id=investigation_id,
                    sample_id=None,
                    sample_sha256=sample_sha256,
                    origin="manual",
                )
            )
    reference = EvidenceReference(
        kind=reference_kind,
        id=str(association_id) if reference_kind == "sample_association" else sample_sha256,
        version="association-v1" if reference_kind == "sample_association" else "sample-hash-v1",
        sha256=sample_sha256,
    )
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=base.application_request,
        evidence_refs=(reference,),
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(
            _store(session_factory, model_submission=True)
        ).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == expected_reason
        assert transport.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_local_association", [True, False])
async def test_sample_hash_association_lookup_is_scoped_to_claim_investigation(
    migrated_postgres_url: str, tmp_path: Path, include_local_association: bool
) -> None:
    first_factory, first_engine, first_investigation, _subject, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    second_factory, second_engine, second_investigation, _subject2, _other = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, base = authorities[0]
    sample_sha256 = "b" * 64
    async with second_factory() as session:
        async with session.begin():
            if include_local_association:
                session.add(
                    SampleAssociationRow(
                        id=uuid4(),
                        investigation_id=first_investigation,
                        sample_id=None,
                        sample_sha256=sample_sha256,
                        origin="manual",
                    )
                )
            session.add(
                SampleAssociationRow(
                    id=uuid4(),
                    investigation_id=second_investigation,
                    sample_id=None,
                    sample_sha256=sample_sha256,
                    origin="manual",
                )
            )
    prepared = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope=base.provider_scope,
        normalizer_version=base.normalizer_version,
        canonical_request=base.canonical_request,
        application_request=base.application_request,
        evidence_refs=(
            EvidenceReference(
                kind="sample_hash",
                id=sample_sha256,
                version="sample-hash-v1",
                sha256=sample_sha256,
            ),
        ),
    )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(_store(first_factory)).send_one(
            authority, prepared, transport
        )
        if include_local_association:
            assert isinstance(outcome, Completed)
            assert transport.calls == 1
        else:
            assert isinstance(outcome, Refused)
            assert outcome.reason == "sample_hash_not_associated_with_investigation"
            assert transport.calls == 0
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_empty_source_refs_do_not_match_unrelated_null_sample_associations(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    first_factory, first_engine, _first_investigation, _subject, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    second_factory, second_engine, second_investigation, _subject2, _other = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    async with second_factory() as session:
        async with session.begin():
            session.add(
                SampleAssociationRow(
                    id=uuid4(),
                    investigation_id=second_investigation,
                    sample_id=None,
                    sample_sha256="e" * 64,
                    origin="manual",
                )
            )
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(_store(first_factory)).send_one(
            authority, prepared, transport
        )
        assert isinstance(outcome, Completed)
        assert transport.calls == 1
    finally:
        await first_engine.dispose()
        await second_engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_effect_and_replay_make_one_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url,
        tmp_path,
        suffix=uuid4().hex[:12],
        invocations=2,
    )
    store = _store(session_factory)
    coordinator = ExternalEffectCoordinator(
        _ClaimBarrierStore(store, asyncio.Barrier(len(authorities)))
    )
    transport = _CountingTransport()
    try:
        results = await asyncio.gather(
            *(
                coordinator.send_one(authority, prepared, transport)
                for authority, prepared in authorities
            )
        )
        assert transport.calls == 1
        completed_result = next(result for result in results if isinstance(result, Completed))
        assert sum(isinstance(result, Completed) for result in results) == 1
        assert sum(isinstance(result, (Refused, Ambiguous)) for result in results) == 1
        first_authority, first_prepared = authorities[0]
        replay = await coordinator.send_one(first_authority, first_prepared, transport)
        assert isinstance(replay, Refused)
        assert replay.reason == "effect_occurrence_required"
        assert transport.calls == 1
        async with session_factory() as session:
            completed_claim = await session.get(ExternalEffectClaimRow, completed_result.claim_id)
            first_reservation = await session.get(
                InvestigationBudgetReservationRow,
                (investigation_id, first_authority.operation_key),
            )
            envelope = await session.get(InvestigationEnvelopeRow, investigation_id)
            claim_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(
                    ExternalEffectClaimRow.investigation_id == investigation_id,
                    ExternalEffectClaimRow.effect_key
                    == effect_key(investigation_id, first_prepared),
                )
            )
            sent_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectUseRow)
                .where(
                    ExternalEffectUseRow.claim_id
                    == next(result.claim_id for result in results if isinstance(result, Completed)),
                    ExternalEffectUseRow.use_kind == "SENT",
                )
            )
        assert completed_claim is not None
        assert first_reservation is not None
        assert envelope is not None
        first_budget_reservation = next(
            item
            for item in envelope.budget_snapshot["reservations"]
            if item["operation_key"] == first_authority.operation_key
        )
        if first_authority.invocation_id == completed_claim.invocation_id:
            assert first_reservation.status == "reserved"
            assert first_budget_reservation["status"] == "reserved"
            assert envelope.version == 4
        else:
            assert first_reservation.status == "released"
            assert first_budget_reservation["status"] == "released"
            assert envelope.version == 5
        assert claim_count == 1
        assert sent_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_new_occurrence_creates_claim_after_recorded_reuse(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url,
        tmp_path,
        suffix=uuid4().hex[:12],
        invocations=3,
    )

    class _AllowReuse:
        async def authorize_reuse(
            self,
            claim: Any,
            _authority: ExternalEffectAuthority,
            _prepared: PreparedExternalRequest,
        ) -> ReuseDecision:
            return ReuseDecision(source_invocation_id=claim.invocation_id, observation_refs=())

    coordinator = ExternalEffectCoordinator(
        _store(session_factory), reuse_policies={_FAMILY: _AllowReuse()}
    )
    transport = _CountingTransport()
    original_authority, original_prepared = authorities[0]
    try:
        original = await coordinator.send_one(original_authority, original_prepared, transport)
        assert isinstance(original, Completed)
        assert transport.calls == 1

        reuse_authority, reuse_prepared = authorities[1]
        reused = await coordinator.send_one(
            replace(reuse_authority, expected_envelope_version=4),
            reuse_prepared,
            transport,
        )
        assert isinstance(reused, Reused)
        assert reused.claim_id == original.claim_id
        assert transport.calls == 1

        occurrence_authority, _ = authorities[2]
        new_prepared = replace(original_prepared, occurrence_id=uuid4())
        new_effect = await coordinator.send_one(
            replace(occurrence_authority, expected_envelope_version=4),
            new_prepared,
            transport,
        )
        assert isinstance(new_effect, Completed)
        assert new_effect.claim_id != original.claim_id
        assert transport.calls == 2

        async with session_factory() as session:
            original_claim = await session.get(ExternalEffectClaimRow, original.claim_id)
            new_claim = await session.get(ExternalEffectClaimRow, new_effect.claim_id)
            claims = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
            original_uses = list(
                (
                    await session.scalars(
                        select(ExternalEffectUseRow).where(
                            ExternalEffectUseRow.claim_id == original.claim_id
                        )
                    )
                ).all()
            )
            new_uses = list(
                (
                    await session.scalars(
                        select(ExternalEffectUseRow).where(
                            ExternalEffectUseRow.claim_id == new_effect.claim_id
                        )
                    )
                ).all()
            )
        assert original_claim is not None and original_claim.state == "COMPLETED"
        assert new_claim is not None and new_claim.state == "COMPLETED"
        assert claims == 2
        assert sorted(use.use_kind for use in original_uses) == ["REUSED", "SENT"]
        assert [use.use_kind for use in new_uses] == ["SENT"]
    finally:
        await engine.dispose()


def _policy_authority(
    base: ExternalEffectAuthority, *, subject_id: UUID, policy_event_id: UUID
) -> ExternalEffectAuthority:
    return ExternalEffectAuthority(
        investigation_id=base.investigation_id,
        invocation_id=base.invocation_id,
        operation_key=base.operation_key,
        reservation_amounts=base.reservation_amounts,
        expected_envelope_version=base.expected_envelope_version,
        approval_decision_id=base.approval_decision_id,
        invocation_arguments_sha256=base.invocation_arguments_sha256,
        authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
        policy_event_id=policy_event_id,
        policy_version="osint-open.v1",
        pinned_subject_id=subject_id,
    )


@pytest.mark.asyncio
async def test_revocation_first_guard_denies_with_zero_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    base, prepared = authorities[0]
    guard = _BarrierPolicyGuard(uuid4())
    authority = _policy_authority(
        base, subject_id=subject_id, policy_event_id=guard.policy_event_id
    )
    store = _store(session_factory, policy_guard=guard)
    transport = _CountingTransport()
    try:
        await guard.revoke(session_factory, investigation_id)
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "osint_policy_denied"
        assert guard.events == ["policy_lock"]
        assert transport.calls == 0
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consume_first_policy_lock_wins_before_revocation(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    base, prepared = authorities[0]
    guard = _BarrierPolicyGuard(uuid4(), block_consume=True)
    authority = _policy_authority(
        base, subject_id=subject_id, policy_event_id=guard.policy_event_id
    )
    store = _store(session_factory, policy_guard=guard)
    transport = _CountingTransport()
    try:
        send_task = asyncio.create_task(
            ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        )
        await asyncio.wait_for(guard.consume_locked.wait(), timeout=10)
        revoke_task = asyncio.create_task(guard.revoke(session_factory, investigation_id))
        await asyncio.wait_for(guard.revoke_started.wait(), timeout=10)
        guard.release_consume.set()
        outcome = await send_task
        await revoke_task
        assert isinstance(outcome, Completed)
        assert transport.calls == 1
        assert guard.events == ["policy_lock", "policy_authorize"]
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, outcome.claim_id)
        assert claim is not None and claim.state == "COMPLETED"
    finally:
        guard.release_consume.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_before_consume_leaves_no_claim(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    failing_store = _PreConsumeFailureStore(store)
    transport = _CountingTransport()
    coordinator = ExternalEffectCoordinator(failing_store)  # type: ignore[arg-type]
    try:
        outcome = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "consume_unavailable"
        assert failing_store.consume_calls == 1
        assert transport.calls == 0
        assert transport.submit_entries == 0
        assert "_permits" not in dir(coordinator)
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
        assert count == 0
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            service = InvestigationService(
                post_synthesis=None,
                investigations=uow.analyst_investigations,
                states=uow.investigation_states,
                context_snapshots=None,
                envelopes=uow.investigation_envelopes,
                context_builder=None,
                bridge=None,
                registry=None,
                dispatcher=None,
                invocations=uow.tool_invocations,
                observations=uow.evidence_observations,
                findings=uow.investigation_findings,
                opinions=uow.analyst_opinions,
                commit_uow=uow.commit,
            )
            with pytest.raises(ValueError, match="Ambiguous tool invocations"):
                await service.resume(investigation_id)
        async with session_factory() as session:
            invocation = await session.get(ToolInvocationRow, authority.invocation_id)
            reservation = await session.get(
                InvestigationBudgetReservationRow,
                (investigation_id, authority.operation_key),
            )
            amount = await session.get(
                InvestigationBudgetReservationAmountRow,
                (investigation_id, authority.operation_key, "vt_units"),
            )
        assert invocation is not None and invocation.status == "needs_review"
        assert reservation is not None and reservation.status == "reserved"
        assert amount is not None and amount.reserved_amount == 1
        assert amount.committed_amount is None
        assert transport.calls == 0
        assert transport.submit_entries == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_after_consume_seals_review_and_replay_never_sends(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    transport = _CountingTransport()
    try:
        consumed = await store.consume(
            authority,
            prepared,
            effect_key=effect_key(investigation_id, prepared),
            occurrence_digest=occurrence_sha256(prepared.occurrence_id),
        )
        assert isinstance(consumed, ConsumeCommitted)
        # Simulate restart recovery after the consume commit and before handoff.
        # Replaying this consumed occurrence must seal it to NEEDS_REVIEW itself.
        replay = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(replay, Ambiguous)
        assert transport.calls == 0
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, consumed.claim_id)
            reservation = await session.get(
                InvestigationBudgetReservationRow,
                (investigation_id, authority.operation_key),
            )
            amount = await session.get(
                InvestigationBudgetReservationAmountRow,
                (investigation_id, authority.operation_key, "vt_units"),
            )
        assert claim is not None and claim.state == "NEEDS_REVIEW"
        assert reservation is not None and reservation.status == "reserved"
        assert (
            amount is not None and amount.reserved_amount == 1 and amount.committed_amount is None
        )
    finally:
        await engine.dispose()
