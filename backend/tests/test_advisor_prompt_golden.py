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
            "490caeb6027a1f649831be238a70e281d1e077c71a467cf71aeca68216f0297e",
            1266,
        ),
        "pivot_strategy": (
            "aa9b10637ac96222dc80369901ec64d3dccb9df63985953665006f09f2004fec",
            1241,
        ),
        "yara_draft_base64_hex": (
            "7ccf4d806cf7b0686ab7293aa0e5167eb102bbecc8ff50caca95d66edacbdce2",
            2451,
        ),
        "review_text_multi_unit_versioned": (
            "4957c604edb1ee63e9070f128bff06251386222eb884d0a8cd3a7709847d2213",
            1280,
        ),
        "review_text_legacy_mapping": (
            "eb71c1b57c1ba2af5edcaddc89711f7942fb560d63126a17492a74a56c7982f4",
            1221,
        ),
        "draft_text": ("4ec7622f59f261687794627bc6d448c2d0c357329628b7384cf8fff834d0867f", 1736),
        "suricata_draft": (
            "6e219f616c98ec311024f122cd00d3c6e1caea56bf08e160a211cb823fb99c53",
            1350,
        ),
        "figure_plan": ("7a640d86aa95ad873f2d3230e9f2c75b76e5e67307dd8d76c948b19a061aa7e1", 1307),
        "unicode_crlf": ("7f7d8819f47edabf3f637317308a5d9ea537c2a6594d50576dc2efbde7c88b74", 1180),
        "unicode_lf": ("ab63c973b534e9da87a2f61ebcaa694f1daa99f031ad907507b3341e03f933e8", 1179),
        "json_like_cti_text": (
            "19decaf04bdbcf71578fbb0d894ea3f9dfe74694fa93e8a1720235a8a8c8b366",
            1245,
        ),
        "restricted_projection": (
            "3546e1e361fb51dc985542b1be3f2c97bb73da70de0b3985d31db8b607d3e58d",
            1166,
        ),
        "preflight_refusal": (
            "348ee5968bacd90471a716c0d74b071dc4b5c7b015c1454b7acf7d8c8308eed6",
            1173,
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
        "bb11403cb0de204823b716b33b999d061ac3141fb11149911cff4d9fe0b30c3b"
    )
    assert len(legacy_bytes) == 1204
    assert prepared.prompt_utf8 == legacy_bytes
    assert prepared.prompt_sha256 == hashlib.sha256(legacy_bytes).hexdigest()
    assert max(1, (len(legacy_bytes) + 3) // 4) == 301
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
        "348ee5968bacd90471a716c0d74b071dc4b5c7b015c1454b7acf7d8c8308eed6"
    )
    assert len(encoded) == 1173


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
