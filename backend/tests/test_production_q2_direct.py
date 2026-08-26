from uuid import uuid4

import httpx
import pytest

from cti_app.application import production_workflow
from cti_app.application.model_gateway import (
    ModelGateway,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
)
from cti_app.application.production_parsers import parse_q2_proposals_markdown
from cti_app.application.production_workflow import _extraction_input_hash, _q2_source_model_run_id
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRunStatus
from cti_app.integrations.models import (
    FakeModelAdapter,
    InMemoryModelOutputStore,
    _bridge_http_error,
)
from tests.model_support import InMemoryModelRunUnitOfWorkFactory


def test_q2_source_model_run_id_is_stable_per_generation_and_source() -> None:
    run_id = uuid4()
    first = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )
    assert first == _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )
    assert first != _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=1,
        source_id="S1",
        canonical_url="https://example.test/report",
    )


def test_q2_source_model_run_id_changes_when_routing_policy_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    before = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )

    monkeypatch.setattr(production_workflow, "Q2_ROUTING_POLICY_VERSION", "next")

    after = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )
    assert after != before


def test_iana_snapshot_bump_recomputes_extraction_without_new_q2_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    before_artifact = _extraction_input_hash(
        subject_id=run_id,
        references_hash="references",
        source_urls=["https://example.test/report"],
        pipeline_generation=0,
    )
    before_q2_run = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )

    monkeypatch.setattr(production_workflow, "IANA_TLD_SNAPSHOT_VERSION", "next-snapshot")

    after_artifact = _extraction_input_hash(
        subject_id=run_id,
        references_hash="references",
        source_urls=["https://example.test/report"],
        pipeline_generation=0,
    )
    after_q2_run = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )

    assert after_artifact != before_artifact
    assert after_q2_run == before_q2_run


async def test_q2_model_gateway_reuses_persisted_model_run_across_worker_replay() -> None:
    """A Q2 worker replay before artifact storage must not post a second request."""
    adapter = FakeModelAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=FakeModelAdapter(),
            fake=adapter,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    production_run_id = uuid4()

    def q2_request(generation: int) -> ModelRequest:
        return ModelRequest(
            text="Extract the source",
            prompt_template_id="production-q2-url",
            prompt_template_version="1",
            evidence_pack_hash="a" * 64,
            external_llm_allowed=False,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=ModelProvider.FAKE,
            web_search=True,
            run_id=_q2_source_model_run_id(
                production_run_id=production_run_id,
                pipeline_generation=generation,
                source_id="S1",
                canonical_url="https://example.test/report",
            ),
        )

    first = await gateway.execute(q2_request(0), ModelRole.RESEARCH)
    same_generation = await gateway.execute(q2_request(0), ModelRole.RESEARCH)
    next_generation = await gateway.execute(q2_request(1), ModelRole.RESEARCH)
    replay_before_artifact = await gateway.execute(q2_request(1), ModelRole.RESEARCH)

    assert first.run.status is ModelRunStatus.SUCCEEDED
    assert same_generation.run.id == first.run.id
    assert next_generation.run.id != first.run.id
    assert replay_before_artifact.run.status is ModelRunStatus.SUCCEEDED
    assert len(adapter.calls) == 2


def test_q2_markdown_unescapes_tokens_without_changing_windows_paths() -> None:
    parsed = parse_q2_proposals_markdown(
        """# FACT
category: infection\\_chain
value: C:\\Windows uses other\\_technical and count\\_success
evidence: C:\\inetpub\\wwwroot
"""
    )
    assert parsed.usable
    assert parsed.value is not None
    fact = parsed.value.facts[0]
    assert fact.category == "infection_chain"
    assert fact.value == "C:\\Windows uses other_technical and count_success"
    assert fact.evidence_quote == "C:\\inetpub\\wwwroot"


def test_bridge_timeout_codes_are_preserved() -> None:
    request = httpx.Request("POST", "https://bridge.test/v1/bridge/runs")
    for code in ("bridge_idle_timeout", "bridge_total_timeout"):
        error = _bridge_http_error(
            httpx.Response(502, request=request, json={"error": {"code": code}}), 1
        )
        assert error.code == code
