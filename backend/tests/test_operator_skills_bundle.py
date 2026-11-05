"""The operator skills must teach what the backend actually accepts, and stay generic."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application import investigation_rule_tools, investigation_signature_bridge_tools
from cti_app.application.advisor_checkpoint import split_advisor_trace_purpose
from cti_app.application.investigation_contracts import AdvisorGuidanceV1
from cti_app.application.investigation_rule_tools import (
    RULE_PROPOSE_BRIDGE_SCHEMA,
    RULE_TEST_SCHEMA,
)
from cti_app.application.investigation_signature_bridge_tools import (
    SURICATA_IMPORT_BRIDGE_CANDIDATE_SCHEMA,
)
from cti_app.application.operator_advisor_guidance import ADVISOR_CONSULTATION_TYPES

_REPOSITORY = Path(__file__).parents[2]
_SKILLS = _REPOSITORY / ".agents" / "skills"
_PLAYBOOK = _REPOSITORY / "docs" / "operations" / "operator-playbook-medium.md"
_MANDATE = _REPOSITORY / "docs" / "operations" / "operator-mandate-template.md"
_COLLABORATION = _SKILLS / "autowork-bridge-collaboration" / "SKILL.md"
_STAGING_SCRIPT = _REPOSITORY / "scripts" / "ops" / "stage_operator_cti_skills.py"


def _operator_skill_names() -> tuple[str, ...]:
    match = re.search(r"SKILL_NAMES = \((.*?)\)", _STAGING_SCRIPT.read_text(), re.DOTALL)
    assert match is not None
    return tuple(re.findall(r'"([a-z0-9-]+)"', match.group(1)))


def _operator_texts() -> dict[str, str]:
    texts = {name: (_SKILLS / name / "SKILL.md").read_text() for name in _operator_skill_names()}
    texts["playbook"] = _PLAYBOOK.read_text()
    texts["mandate"] = _MANDATE.read_text()
    texts["index"] = (_SKILLS / "OPERATOR-CTI.md").read_text()
    return texts


def _assert_minimal_bridge_import_call(text: str, *, tool_name: str, schema: dict) -> None:
    placeholder = "<UUID de l'observation advisor.consult>"
    matches = re.findall(rf"{re.escape(tool_name)}\s*\(\s*(\{{[^\n]*?\}})\s*\)", text)
    assert matches, f"{tool_name} needs an explicit minimal-arguments example"
    required = set(schema["required"])
    assert required == {"bridge_observation_id"}
    for example in matches:
        arguments = json.loads(example.replace(placeholder, str(uuid4())))
        assert set(arguments) == required
        assert set(arguments) <= set(schema["properties"])
        identifier = arguments["bridge_observation_id"]
        pattern = schema["properties"]["bridge_observation_id"].get("pattern")
        if pattern:
            assert re.fullmatch(pattern, identifier)


def test_collaboration_skill_is_staged_for_the_operator() -> None:
    assert "autowork-bridge-collaboration" in _operator_skill_names()
    assert "autowork-bridge-collaboration" in (_SKILLS / "OPERATOR-CTI.md").read_text()


def test_documented_trace_example_is_accepted_by_the_backend_parser() -> None:
    skill = _COLLABORATION.read_text()
    block = re.search(r"```\n(\[\[advisor-trace\.v1\]\].*?)\n```", skill, re.DOTALL)
    assert block is not None
    example = block.group(1).replace("<id advisor.consult>", str(uuid4()))
    example = example.replace("<facultatif>", "mesure d'abord la fonction")
    example = example.replace("<but de l'appel, une phrase>", "Mesurer la fonction ciblée.")

    trace, human_purpose = split_advisor_trace_purpose(example)

    assert human_purpose == "Mesurer la fonction ciblée."
    assert trace is not None and trace["recommendation"]["decision"] == "accepted"
    for decision in ("adapted", "refused", "deferred"):
        payload = json.dumps(
            {"recommendation": {"invocation_id": str(uuid4()), "decision": decision, "reason": "x"}}
        )
        parsed, _ = split_advisor_trace_purpose(f"[[advisor-trace.v1]]{payload}\nBut.")
        assert parsed is not None and parsed["recommendation"]["decision"] == decision


def test_every_consultation_type_named_by_the_skills_exists() -> None:
    named: set[str] = set()
    for text in _operator_texts().values():
        named.update(
            set(re.findall(r"`(\w+_(?:investigation|strategy|draft|text|plan))`", text))
            - {"advisory_text"}
        )
    assert named, "the skills should name the consultation types they rely on"
    assert named <= ADVISOR_CONSULTATION_TYPES


def test_skills_cover_every_collaboration_trigger() -> None:
    skill = _COLLABORATION.read_text()
    for consultation_type in ("plan_investigation", "yara_draft", "pivot_strategy", "draft_text"):
        assert f"`{consultation_type}`" in skill
    for moment in (
        "inventaire initial sont disponibles",
        "Après une découverte CTI importante",
        "pivots VT discriminants",
        "chaque section analytique",
    ):
        assert moment in skill


def test_bridge_rule_imports_use_the_registered_minimal_arguments() -> None:
    assert "rule.propose_bridge" in investigation_rule_tools._TOOL_SCHEMAS
    assert (
        investigation_signature_bridge_tools._SURICATA_IMPORT_TOOL_NAME
        == "rule.import_suricata_bridge_candidate"
    )
    for path in _SKILLS.glob("autowork-*/SKILL.md"):
        text = path.read_text()
        if "rule.propose_bridge" in text:
            _assert_minimal_bridge_import_call(
                text,
                tool_name="rule.propose_bridge",
                schema=RULE_PROPOSE_BRIDGE_SCHEMA,
            )
        if "rule.import_suricata_bridge_candidate" in text:
            _assert_minimal_bridge_import_call(
                text,
                tool_name="rule.import_suricata_bridge_candidate",
                schema=SURICATA_IMPORT_BRIDGE_CANDIDATE_SCHEMA,
            )


def test_yara_testing_uses_the_candidate_id_returned_by_import() -> None:
    assert investigation_rule_tools._TOOL_SCHEMAS["rule.test"] is RULE_TEST_SCHEMA
    assert set(RULE_TEST_SCHEMA["required"]) == {"candidate_id"}
    for name, text in _operator_texts().items():
        if "rule.test" in text:
            assert "candidate_id" in text, f"{name} omits the required rule.test candidate_id"


def test_skills_do_not_add_bridge_call_quotas_or_form_gates() -> None:
    quota_terms = re.compile(r"\b(?:quota|minimum|minimum de|au moins|au maximum)\b", re.I)
    bridge_terms = re.compile(r"\b(?:consultations?|échanges?|tours?)\b", re.I)
    negation = re.compile(r"\b(?:pas|aucun(?:e)?|sans|ni|no|without)\b", re.I)
    format_terms = re.compile(
        r"\b(?:format|forme|parsing|parse_status|trace structurée|JSON)\b", re.I
    )
    gate_terms = re.compile(
        r"\b(?:doit|obligatoire|requis|exige|require[sd]?|must|consigne)\b", re.I
    )
    for path in _SKILLS.glob("autowork-*/SKILL.md"):
        text = re.sub(r"```.*?```", "", path.read_text(), flags=re.DOTALL)
        text = re.sub(r"`[^`]*`", "", text)
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if quota_terms.search(sentence) and bridge_terms.search(sentence):
                assert negation.search(sentence), f"{path}: Bridge quota: {sentence}"
            if bridge_terms.search(sentence) and format_terms.search(sentence):
                assert not gate_terms.search(sentence) or negation.search(sentence), (
                    f"{path}: form gate between Bridge exchanges: {sentence}"
                )


def test_playbook_reads_the_guidance_fields_the_backend_exposes() -> None:
    playbook = _PLAYBOOK.read_text()
    for field in ("checkpoint_due", "reason", "suggested_consultation_type", "last_advice"):
        assert field in AdvisorGuidanceV1.model_fields
        assert field in playbook


@pytest.mark.parametrize("name", ["playbook", "mandate", "index", *_operator_skill_names()])
def test_operator_instructions_are_model_neutral_and_generic(name: str) -> None:
    text = _operator_texts()[name]
    assert not re.search(r"\b(?:Luna|DeepSeek|GPT-\d|Gemini|Qwen|Llama)\b", text, re.I), (
        "the operator model is chosen by the orchestrator"
    )
    assert not re.search(r"\b[0-9a-fA-F]{64}\b", text), "no historical hash in permanent guidance"
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), "no historical address"
    assert not re.search(r"\b(?:M12-?Q|R3|Run ?12)\b", text), "no historical run result"


def test_mandate_template_has_three_to_six_sentences_and_nonblocking_report_list() -> None:
    template = _MANDATE.read_text()
    mandate = re.search(r"## Mandat métier\s+(.*?)\s+## Aide-mémoire", template, re.DOTALL)
    assert mandate is not None
    sentences = re.findall(r"[^.!?]+[.!?]", mandate.group(1))
    assert 3 <= len(sentences) <= 6
    assert "plan" not in mandate.group(1).casefold()
    assert "historique" not in mandate.group(1).casefold()
    assert "non bloquant" in template.casefold()
    for deliverable in ("établi", "incertain", "échantillons", "règles", "Bridge", "frictions"):
        assert deliverable.casefold() in template.casefold()
