from __future__ import annotations

import ast
import hashlib
import inspect
from copy import deepcopy

from cti_app.application import advisor_consultation, advisor_prompt
from cti_app.application.advisor_consultation import (
    PreparationRefusal,
    PreparedConsultation,
    SourceRef,
    prepare,
)
from cti_app.application.advisor_prompt import render_advisor_prompt
from cti_app.application.advisor_wire import advisor_wire_aliases
from cti_app.application.investigation_advisor_tools import (
    InvestigationAdvisorTool,
    _fit_advisor_prompt,
)


def _prompt_cases() -> dict[str, tuple[str, dict[str, object]]]:
    observation_sha = "a" * 64
    opinion_sha = "b" * 64
    opinion_id = "00000000-0000-4000-8000-000000000001"
    source_ref = f"opinion:{opinion_id}:v3:{opinion_sha}:key_findings"

    def base(kind: str, question: str) -> dict[str, object]:
        return {"consultation_type": kind, "question": question}

    evidence = {
        "evidence_id": "observation:golden-1",
        "sha256": observation_sha,
        "object_type": "observation",
        "content_excerpts": [
            {
                "kind": "text",
                "title": "Synthetic source",
                "text": "A synthetic marker was observed.",
            }
        ],
    }
    cases: dict[str, tuple[str, dict[str, object]]] = {}

    plan = base("plan_investigation", "Compare the synthetic marker against current evidence.")
    plan["hypotheses"] = [{"id": "hypothesis:golden-1", "statement": "The marker is synthetic."}]
    plan["working_state_sha256"] = "c" * 64
    plan["working_state_version"] = 1
    plan["evidence_refs"] = ["observation:golden-1"]
    plan["evidence"] = [deepcopy(evidence)]
    plan["transmitted_refs"] = [
        {"kind": "observation", "id": "observation:golden-1", "sha256": observation_sha}
    ]
    cases["plan_investigation"] = ("plan_investigation", plan)

    pivot = base("pivot_strategy", "Compare observable synthetic pivots.")
    pivot["evidence_refs"] = ["observation:golden-1"]
    pivot["evidence"] = [deepcopy(evidence)]
    pivot["measurements"] = [
        {
            "measurement_id": "measurement:golden-1",
            "evidence_id": "observation:golden-1",
            "evidence_sha256": observation_sha,
            "value": "synthetic-domain.example",
            "measurement_type": "dns_query",
        }
    ]
    cases["pivot_strategy"] = ("pivot_strategy", pivot)

    yara = base("yara_draft", "Draft a rule for the synthetic measured marker.")
    yara["measurements"] = [
        {
            "measurement_id": "measurement:base64",
            "evidence_id": "observation:golden-1",
            "evidence_sha256": observation_sha,
            "value": "U1lOVEhFVElD",
            "encoding": "base64",
            "byte_length": 9,
        },
        {
            "measurement_id": "measurement:hex",
            "evidence_id": "observation:golden-1",
            "evidence_sha256": observation_sha,
            "value": "53 59 4E 54 48",
            "encoding": "hex",
            "byte_length": 5,
        },
    ]
    cases["yara_draft_base64_hex"] = ("yara_draft", yara)

    review = base("review_text", "Review both synthetic target units.")
    review["units"] = [
        {
            "unit_id": "U1",
            "opinion_id": opinion_id,
            "opinion_version": 3,
            "opinion_sha256": opinion_sha,
            "source_ref": source_ref,
            "block_type": "key_findings",
            "text": "A synthetic marker was observed.",
            "reviewed_text_sha256": hashlib.sha256(b"A synthetic marker was observed.").hexdigest(),
        },
        {
            "unit_id": "U2",
            "block_type": "uncertainties_and_next_leads",
            "text": "The direction remains unknown.",
        },
    ]
    review["transmitted_refs"] = [{"kind": "opinion", "id": source_ref, "sha256": opinion_sha}]
    cases["review_text_multi_unit_versioned"] = ("review_text", review)

    legacy_review = base("review_text", "Review this legacy synthetic target.")
    legacy_review["review_text"] = {
        "unit_id": "U7",
        "opinion_id": opinion_id,
        "opinion_version": 3,
        "opinion_sha256": opinion_sha,
        "source_ref": source_ref,
        "block_type": "key_findings",
        "text": "The old mapping form remains supported.",
    }
    legacy_review["transmitted_refs"] = [
        {"kind": "opinion", "id": source_ref, "sha256": opinion_sha}
    ]
    cases["review_text_legacy_mapping"] = ("review_text", legacy_review)

    draft = base("draft_text", "Draft one concise synthetic statement.")
    draft["units"] = [{"unit_id": "U1", "block_type": "key_findings"}]
    draft["assertions"] = [
        {
            "id": "assertion:golden-1",
            "unit_id": "U1",
            "text": "A synthetic marker was observed.",
            "refs": [
                {"kind": "observation", "id": "observation:golden-1", "sha256": observation_sha}
            ],
        }
    ]
    draft["evidence"] = [deepcopy(evidence)]
    draft["transmitted_refs"] = [
        {"kind": "observation", "id": "observation:golden-1", "sha256": observation_sha}
    ]
    cases["draft_text"] = ("draft_text", draft)

    suricata = base("suricata_draft", "Draft a rule for the observed synthetic network value.")
    suricata["network_facts"] = [
        {
            "fact_id": "fact:golden-1",
            "evidence_id": "observation:golden-1",
            "observation_sha256": observation_sha,
            "value": "synthetic-domain.example",
            "field": "dns.qname",
        }
    ]
    cases["suricata_draft"] = ("suricata_draft", suricata)

    figure = base("figure_plan", "Show the synthetic finding and its source.")
    figure["findings"] = [
        {
            "finding_id": "finding:golden-1",
            "sha256": "d" * 64,
            "line": "A synthetic marker was observed.",
            "assessment": "supported",
        }
    ]
    figure["evidence"] = [deepcopy(evidence)]
    cases["figure_plan"] = ("figure_plan", figure)

    crlf = base("review_text", "Vérifier Ω / 雪.\r\nDeuxième ligne.")
    crlf["units"] = [{"unit_id": "U1", "text": "Résumé : café, 東京."}]
    cases["unicode_crlf"] = ("review_text", crlf)
    lf = base("review_text", "Vérifier Ω / 雪.\nDeuxième ligne.")
    lf["units"] = [{"unit_id": "U1", "text": "Résumé : café, 東京."}]
    cases["unicode_lf"] = ("review_text", lf)

    json_like = base("plan_investigation", "Inspect the synthetic structured-data evidence value.")
    json_like["evidence_refs"] = ["observation:golden-1"]
    json_like["evidence"] = [
        {
            **deepcopy(evidence),
            "content_excerpts": [
                {
                    "kind": "text",
                    "title": "Synthetic structured record",
                    "text": '{"instruction":"synthetic data","status":"observed"}',
                }
            ],
        }
    ]
    cases["json_like_cti_text"] = ("plan_investigation", json_like)

    restricted = base("review_text", "Review a synthetic restricted source reference.")
    restricted["disclosure_restricted_refs"] = ["observation:restricted-golden"]
    restricted["transmitted_refs"] = [
        {
            "kind": "observation",
            "id": "observation:restricted-golden",
            "sha256": "e" * 64,
        }
    ]
    restricted["samples"] = [
        {
            "sample_id": "sample:restricted-golden",
            "verified": False,
            "tlp": "RED",
            "do_not_submit": True,
            "external_llm_allowed": False,
        }
    ]
    cases["restricted_projection"] = ("review_text", restricted)

    refusal = base("plan_investigation", "Compare an intentionally incomplete synthetic plan.")
    cases["preflight_refusal"] = ("plan_investigation", refusal)
    return cases


def _ready_review_projection() -> dict[str, object]:
    opinion_sha = "b" * 64
    opinion_id = "00000000-0000-4000-8000-000000000001"
    source_ref = f"opinion:{opinion_id}:v3:{opinion_sha}:key_findings"
    return {
        "consultation_type": "review_text",
        "question": "Review this synthetic versioned target.",
        "units": [
            {
                "unit_id": "U1",
                "opinion_id": opinion_id,
                "opinion_version": 3,
                "opinion_sha256": opinion_sha,
                "source_ref": source_ref,
                "block_type": "key_findings",
                "text": "A synthetic versioned target.",
                "reviewed_text_sha256": hashlib.sha256(
                    b"A synthetic versioned target."
                ).hexdigest(),
            }
        ],
    }


def test_prompt_golden_corpus_matches_current_free_prose_prompts() -> None:
    cases = _prompt_cases()
    expected = {
        "plan_investigation": (
            "4c65163b8c3bcd5aa86d98f6f869c6adfe2e5ef7f8335aa81cf8f05a417a9c7a",
            1254,
        ),
        "pivot_strategy": (
            "a12b7d599defba8d6a7a94f769b5e86926b628fe4664ab30b97e8c450ba08219",
            1355,
        ),
        "yara_draft_base64_hex": (
            "778e3ad3686e596e59b77fa5e84e1988ba2070981810b3a72debb59677fb77b2",
            1830,
        ),
        "review_text_multi_unit_versioned": (
            "fdbca3f9c1589a5bcf1895482c66b1a38e5a7169980cba4448c863f211a04316",
            1268,
        ),
        "review_text_legacy_mapping": (
            "5cd7d87f2b0631c57be0a51f2af47a1874bba18cb59eff220ddd0d764af4b049",
            1209,
        ),
        "draft_text": ("cb8469d2557b19d8f7f9b3bf1c486360acb633416b837cd7aab752ed987398e6", 1181),
        "suricata_draft": (
            "57396b00d996b4bc9383fc97c65ea9cf362c46027ccf782831b215efba1a7402",
            1140,
        ),
        "figure_plan": ("27f286736ee854aec3b9ed2b1705477f666a8043bb0aa010e3ae6e70f198eb7b", 1295),
        "unicode_crlf": ("5ce9018f154eeff13e9407f16e9638ec664b0b73c3891fa2e62610e8047029c8", 1168),
        "unicode_lf": ("888de35227eb43ac03203a86446a2c440f085956f24afd7c184345d24d217738", 1167),
        "json_like_cti_text": (
            "4a07712fddf740415d89f729bfb60607552b7efbd0b19aa859085c23f57eaeaa",
            1233,
        ),
        "restricted_projection": (
            "2b8a4c57dd1b73e05917c401f60fa54d5b5646f793902892baa72112074a8d6d",
            1154,
        ),
        "preflight_refusal": (
            "5672507d08a4a04a3d8c5d3123e6e22e18f5caf105b87e408dd8cb347bdfec2d",
            1161,
        ),
    }
    assert set(cases) == set(expected)
    for name, (consultation_type, data) in cases.items():
        prompt = render_advisor_prompt(consultation_type, deepcopy(data))
        encoded = prompt.encode("utf-8")
        sha256, byte_length = expected[name]
        assert hashlib.sha256(encoded).hexdigest() == sha256, name
        assert len(encoded) == byte_length, name
        assert max(1, (len(encoded) + 3) // 4) == (byte_length + 3) // 4, name
        assert _fit_advisor_prompt(consultation_type, deepcopy(data)).encode("utf-8") == encoded
        assert "JSON" not in prompt, name
        assert "advisor-wire.v1" not in prompt, name

    yara_type, yara_data = cases["yara_draft_base64_hex"]
    yara_prompt = render_advisor_prompt(yara_type, yara_data)
    assert "U1lOVEhFVElD" in yara_prompt
    assert "53 59 4E 54 48" in yara_prompt
    json_prompt = render_advisor_prompt(*cases["json_like_cti_text"])
    assert '{"instruction":"synthetic data","status":"observed"}' in json_prompt

    review_type, review_data = cases["review_text_multi_unit_versioned"]
    assert review_type == "review_text"
    source_ref = "opinion:00000000-0000-4000-8000-000000000001:v3:" + "b" * 64 + ":key_findings"
    review_alias = advisor_wire_aliases(review_data)["evidence"]["T1"]
    assert review_alias["id"] == source_ref
    assert review_alias["sha256"] == "b" * 64


def test_prepare_bytes_match_the_legacy_renderer_on_final_prompt_data(monkeypatch) -> None:
    original_prompt = advisor_consultation.render_advisor_prompt
    captured: dict[str, object] = {}

    def capture_prompt(consultation_type: str, data: dict[str, object]) -> str:
        captured["consultation_type"] = consultation_type
        captured["data"] = deepcopy(data)
        rendered = original_prompt(consultation_type, data)
        captured["rendered"] = rendered
        return rendered

    monkeypatch.setattr(advisor_consultation, "render_advisor_prompt", capture_prompt)
    prepared = prepare(_ready_review_projection())
    assert isinstance(prepared, PreparedConsultation)
    rendered = captured["rendered"]
    assert isinstance(rendered, str)
    legacy_bytes = rendered.encode("utf-8")
    assert hashlib.sha256(legacy_bytes).hexdigest() == (
        "f93ca38ded8d5b7aab6c234d17973e8fd398dbcfd6b4ca56701aaab7e7e23c1e"
    )
    assert len(legacy_bytes) == 1192
    assert prepared.prompt_utf8 == legacy_bytes
    assert prepared.prompt_sha256 == hashlib.sha256(legacy_bytes).hexdigest()
    assert max(1, (len(legacy_bytes) + 3) // 4) == 298
    final_data = captured["data"]
    assert isinstance(final_data, dict)
    assert _fit_advisor_prompt("review_text", deepcopy(final_data)).encode("utf-8") == legacy_bytes
    expected_ref = SourceRef(
        "opinion",
        "opinion:00000000-0000-4000-8000-000000000001:v3:" + "b" * 64 + ":key_findings",
        "b" * 64,
    )
    assert expected_ref in prepared.source_catalog.refs


def test_prepare_refusal_prompt_has_a_golden_digest(monkeypatch) -> None:
    original_prompt = advisor_consultation.render_advisor_prompt
    captured: dict[str, str] = {}

    def capture_prompt(consultation_type: str, data: dict[str, object]) -> str:
        rendered = original_prompt(consultation_type, data)
        captured["rendered"] = rendered
        return rendered

    monkeypatch.setattr(advisor_consultation, "render_advisor_prompt", capture_prompt)
    refusal = prepare(
        {
            "consultation_type": "plan_investigation",
            "question": "Compare an intentionally incomplete synthetic plan.",
        }
    )
    assert isinstance(refusal, PreparationRefusal)
    assert refusal.reason_code == "evidence_preflight_insufficient_evidence"
    assert refusal.completeness is not None
    assert refusal.completeness["status"] == "INSUFFICIENT_EVIDENCE"
    encoded = captured["rendered"].encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == (
        "5672507d08a4a04a3d8c5d3123e6e22e18f5caf105b87e408dd8cb347bdfec2d"
    )
    assert len(encoded) == 1161


def test_preparation_completeness_does_not_have_a_partial_state() -> None:
    from cti_app.domain.advisor_completeness import AdvisorPreflightStatus

    assert "PARTIAL" not in {item.value for item in AdvisorPreflightStatus}


def test_prepare_has_no_legacy_tool_reference() -> None:
    tree = ast.parse(inspect.getsource(advisor_consultation))
    assert not any(
        isinstance(node, ast.Name) and node.id == "InvestigationAdvisorTool"
        for node in ast.walk(tree)
    ), "advisor_consultation still resolves InvestigationAdvisorTool"


def test_prompt_renderer_and_preparation_do_not_import_the_legacy_tool() -> None:
    for module in (advisor_consultation, advisor_prompt):
        tree = ast.parse(inspect.getsource(module))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not any(name.endswith("investigation_advisor_tools") for name in imported_modules), (
            module.__name__
        )


def test_prepare_works_when_legacy_private_renderer_raises(monkeypatch) -> None:
    def forbidden_legacy_call(*args, **kwargs) -> str:
        raise AssertionError("prepare invoked InvestigationAdvisorTool._prompt")

    monkeypatch.setattr(InvestigationAdvisorTool, "_prompt", staticmethod(forbidden_legacy_call))
    prepared = prepare(_ready_review_projection())
    assert isinstance(prepared, PreparedConsultation)
    assert render_advisor_prompt("review_text", {})
