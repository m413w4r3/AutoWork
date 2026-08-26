from uuid import uuid4

import httpx

from cti_app.application.production_parsers import parse_q2_proposals_markdown
from cti_app.application.production_workflow import _q2_source_model_run_id
from cti_app.integrations.models import _bridge_http_error


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
