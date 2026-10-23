from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cti_app.application.invariant_proposals as p10
from cti_app.application.invariant_proposals import ProposalContractError
from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool
from cti_app.domain.classification import TLP
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


def test_p10_dev_override_requires_development_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv(p10._DEV_LIMITS_ENABLED_ENV, "1")

    with pytest.raises(ProposalContractError, match="requires APP_ENV=development"):
        _service()


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
