from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cti_app.application.invariant_proposals as p10
from cti_app.application.invariant_proposals import ProposalContractError
from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool
from cti_app.domain.classification import TLP
from cti_app.domain.invariants import ReportClaimProvenance
from cti_app.domain.investigation import ToolInvocationStatus

_HELPERS_SPEC = importlib.util.spec_from_file_location(
    "_mission01_test_invariant_proposals", Path(__file__).with_name("test_invariant_proposals.py")
)
assert _HELPERS_SPEC is not None and _HELPERS_SPEC.loader is not None
_HELPERS = importlib.util.module_from_spec(_HELPERS_SPEC)
sys.modules[_HELPERS_SPEC.name] = _HELPERS
_HELPERS_SPEC.loader.exec_module(_HELPERS)
INVESTIGATION_ID = _HELPERS.INVESTIGATION_ID
_service = _HELPERS._service


def _utf8_padding(byte_count: int) -> str:
    assert byte_count >= 0
    return "é" * (byte_count // 2) + ("x" if byte_count % 2 else "")


def _provenance_refs(context: dict[str, object]) -> tuple[str, ...]:
    values = context.get("provenances", ())
    return tuple(
        str(item["provenance_ref"])
        for item in values
        if isinstance(item, dict) and isinstance(item.get("provenance_ref"), str)
    )


@pytest.mark.asyncio
async def test_p10_default_bound_is_utf8_byte_exact_and_refuses_before_fake_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(p10._DEV_LIMITS_ENABLED_ENV, raising=False)
    monkeypatch.delenv(p10._DEV_P10_PROMPT_BYTES_ENV, raising=False)
    assert p10._p10_prompt_max_bytes() == 96_000

    original_render_facts = p10._render_p10_facts
    service, _, conversation, _ = _service()
    first = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)
    baseline_prompt = conversation.prompts[0]
    baseline_size = len(baseline_prompt.encode("utf-8"))
    assert baseline_size < 95_999

    baseline_rendered, aliases = p10._render_p10_prompt(first.snapshot)
    assert baseline_rendered == baseline_prompt
    alias_by_ref = {ref: alias for alias, ref in aliases.items()}
    baseline_facts = original_render_facts(first.snapshot.context, alias_by_ref)
    expected_refs = _provenance_refs(first.snapshot.context)
    assert expected_refs
    assert all(f"[{alias}]" in baseline_prompt for alias in aliases)

    targets = iter((95_999, 96_000, 96_001))
    attempted_sizes: list[int] = []
    rendered_refs: list[tuple[str, ...]] = []

    def padded_render_facts(context: dict[str, object], alias_map: dict[str, str]) -> str:
        target = next(targets)
        facts = original_render_facts(context, alias_map)
        assert facts == baseline_facts
        rendered_refs.append(_provenance_refs(context))
        padding_bytes = target - baseline_size
        facts = facts + _utf8_padding(padding_bytes)
        attempted_sizes.append(baseline_size + len(_utf8_padding(padding_bytes).encode("utf-8")))
        return facts

    monkeypatch.setattr(p10, "_render_p10_facts", padded_render_facts)
    below = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=2)
    at = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=3)
    assert len(conversation.prompts[-2].encode("utf-8")) == 95_999
    assert len(conversation.prompts[-1].encode("utf-8")) == 96_000
    assert _provenance_refs(below.snapshot.context) == expected_refs
    assert _provenance_refs(at.snapshot.context) == expected_refs

    calls_before_refusal = conversation.external_calls
    prompts_before_refusal = len(conversation.prompts)
    with pytest.raises(ProposalContractError, match=r"96001 UTF-8 bytes.*96000 bytes"):
        await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=4)

    assert attempted_sizes == [95_999, 96_000, 96_001]
    assert rendered_refs == [expected_refs, expected_refs, expected_refs]
    assert conversation.external_calls == calls_before_refusal
    assert len(conversation.prompts) == prompts_before_refusal


@pytest.mark.asyncio
async def test_p10_dev_override_accepts_within_hard_memory_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    original_render_facts = p10._render_p10_facts
    service, _, conversation, _ = _service()
    first = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)
    baseline_size = len(conversation.prompts[0].encode("utf-8"))
    target_size = p10._MAX_DEV_P10_PROMPT_BYTES
    assert baseline_size < target_size <= p10._MAX_DEV_P10_PROMPT_BYTES

    assert p10._p10_prompt_max_bytes() == target_size
    requested_size = target_size

    def padded_render_facts(context: dict[str, object], alias_map: dict[str, str]) -> str:
        facts = original_render_facts(context, alias_map)
        assert _provenance_refs(context) == _provenance_refs(first.snapshot.context)
        return facts + _utf8_padding(requested_size - baseline_size)

    monkeypatch.setattr(p10, "_render_p10_facts", padded_render_facts)
    result = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=2)
    assert len(conversation.prompts[-1].encode("utf-8")) == target_size
    assert _provenance_refs(result.snapshot.context) == _provenance_refs(first.snapshot.context)
    assert conversation.external_calls == 2

    requested_size = target_size + 1
    calls_before_refusal = conversation.external_calls
    prompts_before_refusal = len(conversation.prompts)
    with pytest.raises(ProposalContractError, match=r"1048577 UTF-8 bytes.*1048576 bytes"):
        await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=3)
    assert conversation.external_calls == calls_before_refusal
    assert len(conversation.prompts) == prompts_before_refusal


@pytest.mark.parametrize(
    "configured",
    ("", "0", "-1", "1_000", "+5", " 5", "5 ", "1.5", "1048577"),
)
def test_p10_dev_override_rejects_noncanonical_or_over_bound_values_at_service_creation(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, configured)

    with pytest.raises(ProposalContractError, match=p10._DEV_P10_PROMPT_BYTES_ENV):
        _service()


def test_p10_dev_override_is_ignored_without_explicit_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv(p10._DEV_LIMITS_ENABLED_ENV, raising=False)
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))

    service, _, _, _ = _service()

    assert p10._p10_prompt_max_bytes() == p10._MAX_P10_PROMPT_BYTES
    assert service._max_p10_prompt_bytes == p10._MAX_P10_PROMPT_BYTES


def test_p10_dev_override_is_ignored_outside_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))

    service, _, _, _ = _service()

    assert service._max_p10_prompt_bytes == p10._MAX_P10_PROMPT_BYTES


@pytest.mark.parametrize("enabled", ("0", "true", "yes", " 1"))
def test_p10_dev_override_requires_flag_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
    enabled: str,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, enabled)
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))

    service, _, _, _ = _service()

    assert service._max_p10_prompt_bytes == p10._MAX_P10_PROMPT_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("do_not_submit", "tlp"),
    ((True, TLP.GREEN), (False, TLP.RED)),
)
async def test_p10_aggregate_policy_refuses_before_any_route_or_transport(
    monkeypatch: pytest.MonkeyPatch,
    do_not_submit: bool,
    tlp: TLP,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    sample = _HELPERS._sample(
        _HELPERS.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        external=True,
    )
    sample.do_not_submit = do_not_submit
    sample.tlp = tlp
    state = _HELPERS._State(samples=[sample])
    service, _, conversation, _ = _service(state)
    assert service._max_p10_prompt_bytes == p10._MAX_DEV_P10_PROMPT_BYTES

    with pytest.raises(ProposalContractError, match="refused by the aggregate policy"):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.external_calls == 0
    assert conversation.local_calls == 0
    assert conversation.prompts == []
    assert conversation.conversations == {}
    assert state.investigations.investigation.pivot_conversation_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_tlp", "source_do_not_submit", "source_external_allowed"),
    (
        (TLP.RED, False, True),
        (TLP.GREEN, True, True),
        (TLP.GREEN, False, False),
    ),
)
async def test_restricted_claim_source_refuses_even_with_development_prompt_override(
    monkeypatch: pytest.MonkeyPatch,
    source_tlp: TLP,
    source_do_not_submit: bool,
    source_external_allowed: bool,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    sample = _HELPERS._sample(
        _HELPERS.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        external=True,
    )
    source_document = SimpleNamespace(
        id=_HELPERS.SOURCE_DOCUMENT_ID,
        subject_id=_HELPERS.SUBJECT_ID,
        tlp=source_tlp,
        do_not_submit=source_do_not_submit,
        external_llm_allowed=source_external_allowed,
    )
    state = _HELPERS._State(
        samples=[sample],
        source_documents={_HELPERS.SOURCE_DOCUMENT_ID: source_document},
    )
    service, _, conversation, _ = _service(state)

    with pytest.raises(ProposalContractError, match="refused by the aggregate policy"):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.conversations == {}
    assert conversation.prompts == []
    assert conversation.external_calls == 0
    assert conversation.local_calls == 0
    assert state.investigations.investigation.pivot_conversation_id is None


@pytest.mark.asyncio
async def test_missing_claim_source_refuses_before_route_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    state = _HELPERS._State(
        samples=[
            _HELPERS._sample(
                _HELPERS.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                external=True,
            )
        ],
        source_documents={},
    )
    service, _, conversation, _ = _service(state)

    with pytest.raises(ProposalContractError, match=r"source document .* does not exist"):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.conversations == {}
    assert conversation.prompts == []
    assert conversation.external_calls == 0
    assert conversation.local_calls == 0
    assert state.investigations.investigation.pivot_conversation_id is None


@pytest.mark.asyncio
async def test_unresolved_claim_source_policy_refuses_before_route_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    source_document = SimpleNamespace(
        id=_HELPERS.SOURCE_DOCUMENT_ID,
        subject_id=_HELPERS.SUBJECT_ID,
        tlp=None,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    state = _HELPERS._State(source_documents={_HELPERS.SOURCE_DOCUMENT_ID: source_document})
    service, _, conversation, _ = _service(state)

    with pytest.raises(ProposalContractError, match="unresolved disclosure policy"):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.conversations == {}
    assert conversation.prompts == []
    assert conversation.external_calls == 0
    assert conversation.local_calls == 0
    assert state.investigations.investigation.pivot_conversation_id is None


@pytest.mark.asyncio
async def test_green_claim_source_is_included_and_passes_with_development_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    service, _, conversation, _ = _service()

    await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.external_calls == 1
    assert conversation.local_calls == 0
    assert len(conversation.prompts) == 1
    assert "bounded claim" in conversation.prompts[0]


@pytest.mark.asyncio
async def test_existing_invariant_claim_provenance_is_checked_when_claim_is_not_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    claim = SimpleNamespace(
        id=_HELPERS.CLAIM_ID,
        subject_id=_HELPERS.SUBJECT_ID,
        source_document_id=_HELPERS.SOURCE_DOCUMENT_ID,
    )
    provenance = ReportClaimProvenance(
        claim_id=_HELPERS.CLAIM_ID,
        source_document=_HELPERS.SOURCE_DOCUMENT_ID,
    )
    invariant = SimpleNamespace(
        id=_HELPERS.UUID("66666666-6666-6666-6666-666666666666"),
        pattern="sensitive-pattern-from-restricted-source",
        provenances=(provenance,),
    )
    source_document = SimpleNamespace(
        id=_HELPERS.SOURCE_DOCUMENT_ID,
        subject_id=_HELPERS.SUBJECT_ID,
        tlp=TLP.RED,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    state = _HELPERS._State(
        claims=[],
        known_claims=[claim],
        invariants=[invariant],
        source_documents={_HELPERS.SOURCE_DOCUMENT_ID: source_document},
    )
    service, _, conversation, _ = _service(state)

    with pytest.raises(ProposalContractError, match="refused by the aggregate policy"):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.conversations == {}
    assert conversation.prompts == []
    assert conversation.external_calls == 0
    assert conversation.local_calls == 0
    assert state.investigations.investigation.pivot_conversation_id is None


@pytest.mark.asyncio
async def test_historical_snapshot_claim_absent_from_current_context_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    provenance = ReportClaimProvenance(
        claim_id=_HELPERS.CLAIM_ID,
        source_document=_HELPERS.SOURCE_DOCUMENT_ID,
    )
    provenance_ref = p10._provenance_ref(provenance)
    output = _HELPERS._empty_response()
    output["candidate_invariants"] = [
        _HELPERS._candidate(provenance_ref=provenance_ref)
    ]
    conversation = _HELPERS._ConversationService(output=json.dumps(output))
    invariant = SimpleNamespace(pattern="claim-derived-pattern", provenances=(provenance,))
    state = _HELPERS._State(invariants=[invariant])
    service, _, _, registry = _service(state, conversation=conversation)

    first = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)
    turn, _, output_text = conversation.turns_by_id[first.turn.id]
    historical_input = (
        "BEGIN_P10_SNAPSHOT_DATA\n"
        f"{first.snapshot.canonical_serialization()}\n"
        "END_P10_SNAPSHOT_DATA"
    )
    conversation.turns_by_id[first.turn.id] = (turn, historical_input, output_text)
    state.claims.claims.clear()
    state.invariants.invariants.clear()

    with pytest.raises(ProposalContractError, match="absent from the current context"):
        await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)

    assert len(conversation.prompts) == 1
    assert conversation.external_calls == 1
    assert conversation.local_calls == 0
    assert len(registry.calls) == 1


@pytest.mark.asyncio
async def test_historical_snapshot_source_policy_is_reloaded_before_p09(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    provenance = ReportClaimProvenance(
        claim_id=_HELPERS.CLAIM_ID,
        source_document=_HELPERS.SOURCE_DOCUMENT_ID,
    )
    output = _HELPERS._empty_response()
    output["candidate_invariants"] = [
        _HELPERS._candidate(provenance_ref=p10._provenance_ref(provenance))
    ]
    conversation = _HELPERS._ConversationService(output=json.dumps(output))
    state = _HELPERS._State(
        invariants=[SimpleNamespace(pattern="claim-derived-pattern", provenances=(provenance,))]
    )
    service, _, _, registry = _service(state, conversation=conversation)

    first = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)
    turn, _, output_text = conversation.turns_by_id[first.turn.id]
    historical_input = (
        "BEGIN_P10_SNAPSHOT_DATA\n"
        f"{first.snapshot.canonical_serialization()}\n"
        "END_P10_SNAPSHOT_DATA"
    )
    conversation.turns_by_id[first.turn.id] = (turn, historical_input, output_text)

    original_get = state.source_documents.get
    source_reads = 0

    async def source_becomes_red(document_id: _HELPERS.UUID) -> object | None:
        nonlocal source_reads
        source_reads += 1
        document = await original_get(document_id)
        if source_reads == 2 and document is not None:
            return SimpleNamespace(
                id=document.id,
                subject_id=document.subject_id,
                tlp=TLP.RED,
                do_not_submit=document.do_not_submit,
                external_llm_allowed=document.external_llm_allowed,
            )
        return document

    monkeypatch.setattr(state.source_documents, "get", source_becomes_red)

    with pytest.raises(ProposalContractError, match="historical snapshot replay is refused"):
        await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)

    assert source_reads == 2
    assert conversation.external_calls == 1
    assert conversation.local_calls == 0
    assert len(registry.calls) == 1


@pytest.mark.asyncio
async def test_report_claim_provenance_with_missing_claim_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    state = _HELPERS._State(known_claims=[])
    service, _, conversation, _ = _service(state)

    with pytest.raises(ProposalContractError, match=r"claim .* does not exist"):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.conversations == {}
    assert conversation.prompts == []
    assert conversation.external_calls == 0
    assert conversation.local_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim_row", "source_document", "repository_error", "error_match"),
    (
        (
            {"id": "55555555-5555-5555-5555-555555555555", "text": "claim"},
            None,
            None,
            "source document id",
        ),
        (
            None,
            SimpleNamespace(
                id=_HELPERS.SOURCE_DOCUMENT_ID,
                subject_id=_HELPERS.UUID("77777777-7777-7777-7777-777777777777"),
                tlp=TLP.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            None,
            "does not belong to the investigation subject",
        ),
        (None, None, RuntimeError("repository unavailable"), "could not be resolved"),
    ),
)
async def test_unresolvable_claim_source_provenance_fails_closed_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    claim_row: dict[str, str] | None,
    source_document: SimpleNamespace | None,
    repository_error: Exception | None,
    error_match: str,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    documents = (
        {_HELPERS.SOURCE_DOCUMENT_ID: source_document}
        if source_document is not None
        else None
    )
    claims = [claim_row] if claim_row is not None else None
    state = _HELPERS._State(
        claims=claims,
        source_documents=documents,
        source_document_error=repository_error,
    )
    service, _, conversation, _ = _service(state)

    with pytest.raises(ProposalContractError, match=error_match):
        await service.propose(investigation_id=INVESTIGATION_ID)

    assert conversation.conversations == {}
    assert conversation.prompts == []
    assert conversation.external_calls == 0
    assert conversation.local_calls == 0
    assert state.investigations.investigation.pivot_conversation_id is None


@pytest.mark.asyncio
async def test_claim_source_policy_is_rechecked_before_idempotent_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")
    monkeypatch.setenv(p10._DEV_P10_PROMPT_BYTES_ENV, str(p10._MAX_DEV_P10_PROMPT_BYTES))
    state = _HELPERS._State()
    service, _, conversation, _ = _service(state)
    first = await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)
    source_document = state.source_documents.documents[_HELPERS.SOURCE_DOCUMENT_ID]
    source_document.tlp = TLP.RED

    with pytest.raises(ProposalContractError, match="refused by the aggregate policy"):
        await service.propose(investigation_id=INVESTIGATION_ID, cycle_number=1)

    assert len(conversation.conversations) == 1
    assert first.conversation_id in conversation.conversations
    assert len(conversation.prompts) == 1
    assert conversation.external_calls == 1
    assert conversation.local_calls == 0


def test_unused_advisor_environment_is_absent_from_compose_and_source() -> None:
    root = Path(__file__).resolve().parents[2]
    unused_names = (
        "OPERATOR_ADVISOR_MAX_TURNS_PER_INVESTIGATION",
        "OPERATOR_ADVISOR_MIN_CONSULTATION_INTERVAL_SECONDS",
    )
    for name in unused_names:
        assert name not in (root / "compose.yaml").read_text(encoding="utf-8")
        assert name not in (root / ".env.example").read_text(encoding="utf-8")

    search_roots = (
        root / "backend/src",
        root / "backend/src/cti_app/workers",
        root / "frontend/src",
        root / "scripts",
        root / "workers",
        root / "Makefile",
    )
    for search_root in search_roots:
        if not search_root.exists():
            continue
        files = (search_root,) if search_root.is_file() else search_root.rglob("*")
        for path in files:
            if not path.is_file():
                continue
            try:
                contents = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for name in unused_names:
                assert name not in contents, f"unexpected consumer/reference in {path}"


@pytest.mark.asyncio
async def test_g2_bridge_turn_budget_remains_an_authorization_guard() -> None:
    tool = object.__new__(InvestigationAdvisorTool)
    tool._observations = SimpleNamespace()

    result = await tool._check_advisor_limits(
        INVESTIGATION_ID,
        g2_bridge_turns_remaining=0,
    )

    assert result is not None
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.error_code == "g2_bridge_budget_exhausted"
    assert result.output["advisor_turn_limit"] is None
    assert result.output["g2_bridge_turns_remaining"] == 0
