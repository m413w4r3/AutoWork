from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_proposal_adoption import EditorialProposalAdoptionError
from cti_app.application.editorial_proposals import (
    EditorialProposalApplicationError,
    _validate_editorial_text_grounding,
)
from cti_app.config import Settings
from cti_app.domain.editorial_proposal import (
    EditorialProposalConflict,
    EditorialProposalOrigin,
)
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation
from tests.test_editorial_proposal_adoption import _fixture as _adoption_fixture
from tests.test_editorial_proposal_preparation import (
    _install_paragraph_proposal_context,
    _install_real_workspace_resolver,
    _setup,
)

_FULLWIDTH_CVE = "ＣＶＥ－２０２４－１２３４"  # noqa: RUF001


@pytest.mark.asyncio
async def test_manual_patch_uses_same_validator_and_passes_exact_proposal_to_adoption() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch

    setup = _setup()
    preparation, _entries, _content = _install_real_workspace_resolver(setup)
    validated: list[object] = []
    validate = setup.service._validate_result

    def capture(candidate):
        validated.append(candidate)
        return validate(candidate)

    setup.service._validate_result = capture
    proposal = await setup.service.prepare_manual_patch(
        subject_id=setup.subject_id,
        snapshot_id=preparation.snapshot_id,
        occurrence_id=setup.occurrence_id,
        actor_id=setup.actor_id,
        idempotency_key="manual-client-key-0001",
        patch=ManualPatch(
            typed_patch=ManualTextPatch(
                operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                text="[Synthetic actor] Manual report",
            )
        ),
    )

    assert len(validated) == 1
    assert proposal.origin is EditorialProposalOrigin.MANUAL_PATCH
    assert proposal.state.value == "proposed"
    assert proposal.thread_id is None
    assert proposal.model_run_id is None
    assert proposal.model_turn_id is None
    assert setup.repository.model_runs == []
    assert setup.thread_events == []

    class AdoptionProbe:
        async def adopt(self, _subject_id, _occurrence_id, proposal_id, **_kwargs):
            assert proposal_id == proposal.id
            return SimpleNamespace(proposal=proposal)

    accepted = await AdoptionProbe().adopt(
        proposal.subject_id,
        proposal.occurrence_id,
        proposal.id,
        actor_id=setup.actor_id,
    )
    assert accepted.proposal is proposal


@pytest.mark.parametrize(
    "text",
    (
        "203 . 0 . 113 . 17 is the new C2 server.",
        "Contact hxxp://evil[.]example for instructions.",
        f"The issue is {_FULLWIDTH_CVE}.",
    ),
)
def test_editorial_grounding_detects_normalized_and_defanged_literals(text: str) -> None:
    with pytest.raises(EditorialProposalApplicationError) as error:
        _validate_editorial_text_grounding(text, admitted_evidence_texts=())

    assert error.value.code == "manual_edit_new_fact_unadmitted"


def test_editorial_grounding_matches_admitted_evidence_after_normalization() -> None:
    _validate_editorial_text_grounding(
        "The issue is CVE-2024-1234.",
        admitted_evidence_texts=(f"Observed {_FULLWIDTH_CVE} in the report.",),
    )


@pytest.mark.asyncio
async def test_manual_patch_preserves_unresolved_publication_kind_cause() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch

    setup = _setup()
    preparation, _entries, _content = _install_real_workspace_resolver(setup)
    workspace_error = EditorialWorkspaceError("publication_kind_unresolved")

    async def resolve_revision_target(**_kwargs):
        raise workspace_error

    setup.service._workspace_service.resolve_revision_target = resolve_revision_target

    with pytest.raises(EditorialProposalApplicationError) as caught:
        await setup.service.prepare_manual_patch(
            subject_id=setup.subject_id,
            snapshot_id=preparation.snapshot_id,
            occurrence_id=setup.occurrence_id,
            actor_id=setup.actor_id,
            idempotency_key="manual-client-key-format-01",
            patch=ManualPatch(
                typed_patch=ManualTextPatch(
                    operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                    text="[Synthetic actor] Manual report",
                )
            ),
        )

    assert caught.value.code == "publication_kind_unresolved"
    assert caught.value.__cause__ is workspace_error
    assert setup.repository.records == []
    assert setup.blob_bytes == {}


@pytest.mark.parametrize("text", ("nul\x00byte", "right\u202etoleft", "zero\u200bwidth"))
def test_editorial_grounding_rejects_control_and_invisible_format_characters(
    text: str,
) -> None:
    with pytest.raises(EditorialProposalApplicationError) as error:
        _validate_editorial_text_grounding(text, admitted_evidence_texts=())

    assert error.value.code == "manual_edit_patch_invalid"


def test_editorial_grounding_allows_newlines_and_tabs_for_paragraph_text() -> None:
    _validate_editorial_text_grounding(
        "A stylistic correction\nwith a tab\there.",
        admitted_evidence_texts=(),
        allow_newline_tab=True,
    )


def test_editorial_grounding_rejects_newlines_and_tabs_for_title_text() -> None:
    with pytest.raises(EditorialProposalApplicationError) as error:
        _validate_editorial_text_grounding(
            "A title\nwith a tab\there.",
            admitted_evidence_texts=(),
        )

    assert error.value.code == "manual_edit_patch_invalid"


@pytest.mark.parametrize(
    "origin",
    (EditorialProposalOrigin.MANUAL_PATCH, EditorialProposalOrigin.BRIDGE_DRAFT),
)
def test_title_rewrite_rejects_unknown_technical_literals_for_every_origin(origin) -> None:
    from cti_app.application.editorial_proposals import _manual_wire_response

    setup = _setup()
    preparation, _handle = _install_paragraph_proposal_context(_setup())
    title = setup.preparation()
    raw_response = _manual_wire_response(
        target_path="synthesis.title",
        operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
        base_sha256=title.base_canonical_sha256,
        before_hash=title.before_hash,
        after="[Synthetic actor] CVE-2024-1234 incident",
    )
    candidate = replace(
        title,
        strict_wire=True,
        origin=origin,
        paragraph_evidence_context=preparation.paragraph_evidence_context,
        raw_response=raw_response,
    )

    _patch, state, _event, diagnostics = setup.service._validate_result(candidate)

    assert state.value == "needs_review"
    assert diagnostics["code"] == "manual_edit_new_fact_unadmitted"


@pytest.mark.asyncio
async def test_manual_idempotency_hashes_the_exact_received_text() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch

    setup = _setup()
    preparation, _entries, _content = _install_real_workspace_resolver(setup)
    request = {
        "subject_id": setup.subject_id,
        "snapshot_id": preparation.snapshot_id,
        "occurrence_id": setup.occurrence_id,
        "actor_id": setup.actor_id,
        "idempotency_key": "manual-client-key-exact-01",
    }

    def patch(text: str) -> ManualPatch:
        return ManualPatch(
            typed_patch=ManualTextPatch(
                operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                text=text,
            )
        )

    await setup.service.prepare_manual_patch(
        **request, patch=patch("[Synthetic actor] Manual report")
    )

    with pytest.raises(EditorialProposalConflict) as error:
        await setup.service.prepare_manual_patch(
            **request,
            patch=patch(" [Synthetic actor] Manual report "),
        )

    assert getattr(error.value, "code", None) == "proposal_idempotency_conflict"


@pytest.mark.asyncio
async def test_manual_edit_makes_zero_gateway_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class Gateway:
        calls = 0

        def __getattr__(self, _name: str):
            def unexpected(*_args, **_kwargs):
                self.calls += 1
                raise AssertionError("manual editing must not call a model gateway")

            return unexpected

    gateway = Gateway()
    setup = _setup()
    preparation, _entries, _content = _install_real_workspace_resolver(setup)
    prepared = []
    acceptance = await _adoption_fixture().adopt()

    class ProposalService:
        async def prepare_manual_patch(self, **kwargs):
            proposal = await setup.service.prepare_manual_patch(**kwargs)
            prepared.append(proposal)
            return proposal

    class AdoptionService:
        async def adopt(self, _subject_id, _occurrence_id, proposal_id, **_kwargs):
            proposal = prepared[-1]
            assert proposal.id == proposal_id
            return SimpleNamespace(
                proposal=proposal,
                artifact=acceptance.artifact,
                snapshot=acceptance.snapshot,
                impact=acceptance.impact,
            )

    async def no_purge(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(
        editorial_workspace_api,
        "_manual_proposal_service",
        lambda _request: ProposalService(),
        raising=False,
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "_proposal_adoption_service",
        lambda _request: AdoptionService(),
    )
    monkeypatch.setattr(editorial_workspace_api, "_purge_decided_server_draft", no_purge)

    app = FastAPI()
    app.include_router(editorial_workspace_api.router)

    class IdentityProvider:
        async def current(self):
            return SimpleNamespace(actor_id="dev-analyst")

    app.state.identity_provider = IdentityProvider()
    app.state.model_gateway = gateway

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{setup.subject_id}/editor/blocks/{setup.occurrence_id}/manual-edit",
            headers={"Idempotency-Key": "manual-client-key-0002"},
            json={
                "snapshot_id": str(preparation.snapshot_id),
                "operation": "rewrite_title",
                "text": "[Synthetic actor] Manual report",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "accepted"
    assert len(prepared) == 1
    assert prepared[0].thread_id is None
    assert prepared[0].model_run_id is None
    assert gateway.calls == 0


@pytest.mark.asyncio
async def test_unadmitted_new_fact_shows_actionable_error() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch

    setup = _setup()
    preparation, evidence_handle = _install_paragraph_proposal_context(setup)
    paragraph_context = preparation.paragraph_evidence_context
    assert paragraph_context is not None

    class ContextBuilder:
        async def build_manual(self, _subject_id, _occurrence_id):
            return SimpleNamespace(
                proposal_snapshot_id=preparation.snapshot_id,
                paragraph_evidence_context=paragraph_context,
            )

    setup.service._discussion_context_builder = ContextBuilder()
    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_manual_patch(
            subject_id=setup.subject_id,
            snapshot_id=preparation.snapshot_id,
            occurrence_id=setup.occurrence_id,
            actor_id=setup.actor_id,
            idempotency_key="manual-client-key-0003",
            patch=ManualPatch(
                typed_patch=ManualTextPatch(
                    operation=TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                    text="UnsupportedFamilyX controlled the infected environment.",
                )
            ),
        )

    assert evidence_handle
    assert error.value.code == "manual_edit_new_fact_unadmitted"
    assert setup.repository.records == []
    assert setup.blob_bytes == {}
    assert setup.canonical_writes == []


@pytest.mark.asyncio
async def test_manual_route_enforces_raw_request_byte_limit_with_specific_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_proposal_accept_enabled=True),
    )
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    body = b'{"text":"' + b"x" * editorial_workspace_api._MAX_MANUAL_EDIT_REQUEST_BYTES + b'"}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.post(
            f"/api/subjects/{uuid4()}/editor/blocks/{uuid4()}/manual-edit",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": "manual-client-key-oversize-01",
            },
            content=body,
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "manual_edit_request_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "internal_code", "expected_status", "expected_code"),
    [
        (
            error_type,
            internal_code,
            expected_status,
            expected_code,
        )
        for error_type in (EditorialProposalApplicationError, EditorialProposalAdoptionError)
        for internal_code, expected_status, expected_code in (
            ("publication_kind_unresolved", 422, "manual_edit_format_uneditable"),
            ("proposal_target_mismatch", 422, "manual_edit_target_mismatch"),
            ("proposal_base_stale", 409, "manual_edit_stale"),
            ("proposal_state_conflict", 409, "manual_edit_stale"),
        )
    ],
)
async def test_manual_route_maps_causes_and_logs_code_reason_and_ids(
    error_type: type[EditorialProposalApplicationError] | type[EditorialProposalAdoptionError],
    internal_code: str,
    expected_status: int,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    subject_id, occurrence_id = uuid4(), uuid4()
    stale_reason = "test:base_snapshot" if internal_code == "proposal_base_stale" else None
    internal_error = error_type(internal_code, reason=stale_reason)

    class ProposalService:
        async def prepare_manual_patch(self, **_kwargs):
            if isinstance(internal_error, EditorialProposalApplicationError):
                raise internal_error
            return SimpleNamespace(id=uuid4(), patch_sha256="a" * 64)

    class AdoptionService:
        async def adopt(self, *_args, **_kwargs):
            raise internal_error

    async def actor_id(_request):
        return uuid4()

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", actor_id)
    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", lambda _request: ProposalService()
    )
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: AdoptionService()
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/manual-edit",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": FastAPI(),
        }
    )
    payload = editorial_workspace_api.EditorialManualEditRequest(
        snapshot_id=uuid4(),
        operation="rewrite_title",
        text="Synthetic body text must not be logged",
    )
    caplog.set_level(logging.WARNING, logger=editorial_workspace_api.__name__)

    with pytest.raises(HTTPException) as caught:
        await editorial_workspace_api.manually_edit_editorial_block(
            subject_id,
            occurrence_id,
            request,
            payload,
            "manual-client-key-0001",
        )

    assert caught.value.status_code == expected_status
    assert caught.value.detail["code"] == expected_code
    if internal_code == "proposal_state_conflict":
        assert caught.value.detail["message"] == (
            "Le document a changé. Actualisez l’atelier, puis réappliquez votre brouillon local."  # noqa: RUF001
        )
    assert caught.value.__cause__ is internal_error
    message = caplog.records[-1].getMessage()
    assert internal_code in message
    assert f"reason={stale_reason or 'not_applicable'}" in message
    assert str(subject_id) in message
    assert str(occurrence_id) in message
    assert "Synthetic body text must not be logged" not in message


@pytest.mark.asyncio
async def test_manual_route_keeps_idempotency_conflict_distinct_from_stale(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    subject_id, occurrence_id = uuid4(), uuid4()

    class ProposalService:
        async def prepare_manual_patch(self, **_kwargs):
            raise EditorialProposalConflict()

    async def actor_id(_request):
        return uuid4()

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(editorial_workspace_api, "_thread_actor_id", actor_id)
    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", lambda _request: ProposalService()
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/manual-edit",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": FastAPI(),
        }
    )
    payload = editorial_workspace_api.EditorialManualEditRequest(
        snapshot_id=uuid4(), operation="rewrite_title", text="Synthetic replacement"
    )
    caplog.set_level(logging.WARNING, logger=editorial_workspace_api.__name__)

    with pytest.raises(HTTPException) as caught:
        await editorial_workspace_api.manually_edit_editorial_block(
            subject_id,
            occurrence_id,
            request,
            payload,
            "manual-client-key-0001",
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "manual_edit_idempotency_conflict"
    assert (
        caplog.records[-1]
        .getMessage()
        .startswith("manual_edit_rejected code=proposal_idempotency_conflict ")
    )


@pytest.mark.asyncio
async def test_manual_route_unexpected_error_logs_safe_traceback_origins(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    sensitive_text = "SYNTHETIC-PRIVATE-MANUAL-TEXT"
    subject_id, occurrence_id = uuid4(), uuid4()

    class IdentityProvider:
        async def current(self):
            return SimpleNamespace(actor_id="dev-analyst")

    class ProposalService:
        async def prepare_manual_patch(self, **kwargs):
            assert kwargs["actor_id"] == uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")
            raise ValueError(sensitive_text)

    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    monkeypatch.setattr(
        editorial_workspace_api, "_manual_proposal_service", lambda _request: ProposalService()
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/manual-edit",
            "headers": [],
            "query_string": b"",
            "server": ("editorial.test", 80),
            "client": ("test", 1),
            "app": FastAPI(),
        }
    )
    request.app.state.identity_provider = IdentityProvider()
    payload = editorial_workspace_api.EditorialManualEditRequest(
        snapshot_id=uuid4(), operation="rewrite_title", text=sensitive_text
    )
    caplog.set_level(logging.WARNING, logger=editorial_workspace_api.__name__)

    with pytest.raises(HTTPException) as caught:
        await editorial_workspace_api.manually_edit_editorial_block(
            subject_id,
            occurrence_id,
            request,
            payload,
            "manual-client-key-0001",
        )

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "manual_edit_unavailable"
    message = caplog.records[-1].getMessage()
    assert "origin=test_editorial_manual_patch.py:" in message
    assert ":prepare_manual_patch" in message
    assert "previous_origin=editorial_workspace.py:" in message
    assert ":manually_edit_editorial_block" in message
    assert sensitive_text not in message


@pytest.mark.asyncio
async def test_manual_paragraph_rewrite_keeps_admitted_evidence_and_uses_shared_validator() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch

    setup = _setup()
    preparation, evidence_handle = _install_paragraph_proposal_context(setup)
    paragraph_context = preparation.paragraph_evidence_context
    assert paragraph_context is not None

    class ContextBuilder:
        async def build_manual(self, _subject_id, _occurrence_id):
            return SimpleNamespace(
                proposal_snapshot_id=preparation.snapshot_id,
                paragraph_evidence_context=paragraph_context,
            )

    setup.service._discussion_context_builder = ContextBuilder()
    proposal = await setup.service.prepare_manual_patch(
        subject_id=setup.subject_id,
        snapshot_id=preparation.snapshot_id,
        occurrence_id=setup.occurrence_id,
        actor_id=setup.actor_id,
        idempotency_key="manual-client-key-0006",
        patch=ManualPatch(
            typed_patch=ManualTextPatch(
                operation=TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                text="FooRAT appears in the report, as documented.",
            )
        ),
    )

    assert proposal.origin is EditorialProposalOrigin.MANUAL_PATCH
    assert proposal.state.value == "proposed"
    assert proposal.evidence_refs[0]["evidence_key"]
    assert evidence_handle
    assert setup.repository.model_runs == []


@pytest.mark.asyncio
async def test_manual_style_rewrite_allows_paragraph_without_admitted_references() -> None:
    from cti_app.application.editorial_workspace.registry import registry_hash
    from cti_app.application.production_artifact_store import ProductionArtifactStore
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch
    from cti_app.domain.editorial_workspace import (
        EditorialBlockKind,
        bootstrap_block_registry,
    )

    setup = _setup()
    preparation, _handle = _install_paragraph_proposal_context(setup)
    context = preparation.paragraph_evidence_context
    assert context is not None
    context_without_refs = replace(context, evidence_refs=(), evidence_handles=())
    base_content = dict(preparation.base_content)
    base_content["lead"] = [{**base_content["lead"][0], "evidence_refs": []}]
    base_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(base_content)
    ).hexdigest()
    entries = list(
        bootstrap_block_registry(
            artifact_id=preparation.base_artifact_id,
            canonical_sha256=base_sha256,
            source_stage="synthesis",
            canonical_content=base_content,
        )
    )
    lead_entry = next(entry for entry in entries if entry.kind is EditorialBlockKind.LEAD_PARAGRAPH)
    entries[entries.index(lead_entry)] = replace(lead_entry, block_id=setup.occurrence_id)
    setup.current_snapshot.block_registry = tuple(entries)
    setup.current_snapshot.synthesis_canonical_sha256 = base_sha256
    setup.current_snapshot.registry_hash = registry_hash(tuple(entries))

    async def load_current(_subject_id):
        return {}, setup.current_snapshot, base_content

    setup.service._workspace_service._load_current = load_current

    class ContextBuilder:
        async def build_manual(self, _subject_id, _occurrence_id):
            return SimpleNamespace(
                proposal_snapshot_id=preparation.snapshot_id,
                paragraph_evidence_context=context_without_refs,
            )

    setup.service._discussion_context_builder = ContextBuilder()
    proposal = await setup.service.prepare_manual_patch(
        subject_id=setup.subject_id,
        snapshot_id=preparation.snapshot_id,
        occurrence_id=setup.occurrence_id,
        actor_id=setup.actor_id,
        idempotency_key="manual-client-key-empty-refs-01",
        patch=ManualPatch(
            typed_patch=ManualTextPatch(
                operation=TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                text="A stylistic correction without a new technical fact.",
            )
        ),
    )

    assert proposal.state.value == "proposed"
    assert proposal.evidence_refs == ()
    assert setup.repository.model_runs == []


@pytest.mark.asyncio
async def test_manual_patch_rejects_noop_and_stale_snapshot_without_writes() -> None:
    from cti_app.domain.editorial_proposal import ManualPatch, ManualTextPatch

    setup = _setup()
    preparation, _entries, content = _install_real_workspace_resolver(setup)
    workspace = setup.service._workspace_service
    resolve_current = workspace.resolve_revision_target

    async def resolve_revision_target(**kwargs):
        if kwargs["snapshot_id"] != preparation.snapshot_id:
            from cti_app.domain.editorial_workspace import EditorialWorkspaceError

            raise EditorialWorkspaceError("editorial_snapshot_stale")
        return await resolve_current(**kwargs)

    workspace.resolve_revision_target = resolve_revision_target
    current_title = content["title"]
    with pytest.raises(EditorialProposalApplicationError) as no_op:
        await setup.service.prepare_manual_patch(
            subject_id=setup.subject_id,
            snapshot_id=preparation.snapshot_id,
            occurrence_id=setup.occurrence_id,
            actor_id=setup.actor_id,
            idempotency_key="manual-client-key-0004",
            patch=ManualPatch(
                typed_patch=ManualTextPatch(
                    operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                    text=current_title,
                )
            ),
        )
    assert no_op.value.code == "manual_edit_no_change"

    with pytest.raises(EditorialProposalApplicationError) as stale:
        await setup.service.prepare_manual_patch(
            subject_id=setup.subject_id,
            snapshot_id=uuid4(),
            occurrence_id=setup.occurrence_id,
            actor_id=setup.actor_id,
            idempotency_key="manual-client-key-0005",
            patch=ManualPatch(
                typed_patch=ManualTextPatch(
                    operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
                    text="[Synthetic actor] Manual report",
                )
            ),
        )
    assert stale.value.code == "proposal_base_stale"
    assert stale.value.reason == "workspace:snapshot_stale"
    assert "Manual report" not in stale.value.reason
    assert setup.repository.records == []
    assert setup.blob_bytes == {}
