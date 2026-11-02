"""The operator skills must teach what the backend actually accepts, and stay generic."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.advisor_checkpoint import split_advisor_trace_purpose
from cti_app.application.investigation_contracts import AdvisorGuidanceV1
from cti_app.application.operator_advisor_guidance import ADVISOR_CONSULTATION_TYPES

_REPOSITORY = Path(__file__).parents[2]
_SKILLS = _REPOSITORY / ".agents" / "skills"
_PLAYBOOK = _REPOSITORY / "docs" / "operations" / "operator-playbook-medium.md"
_COLLABORATION = _SKILLS / "autowork-bridge-collaboration" / "SKILL.md"
_STAGING_SCRIPT = _REPOSITORY / "scripts" / "ops" / "stage_operator_cti_skills.py"


def _operator_skill_names() -> tuple[str, ...]:
    match = re.search(r"SKILL_NAMES = \((.*?)\)", _STAGING_SCRIPT.read_text(), re.DOTALL)
    assert match is not None
    return tuple(re.findall(r'"([a-z0-9-]+)"', match.group(1)))


def _operator_texts() -> dict[str, str]:
    texts = {name: (_SKILLS / name / "SKILL.md").read_text() for name in _operator_skill_names()}
    texts["playbook"] = _PLAYBOOK.read_text()
    texts["index"] = (_SKILLS / "OPERATOR-CTI.md").read_text()
    return texts


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
        named.update(re.findall(r"`(\w+_(?:investigation|strategy|draft|text|plan))`", text))
    assert named, "the skills should name the consultation types they rely on"
    assert named <= ADVISOR_CONSULTATION_TYPES


def test_skills_cover_every_collaboration_trigger() -> None:
    skill = _COLLABORATION.read_text()
    for consultation_type in ("plan_investigation", "yara_draft", "pivot_strategy", "draft_text"):
        assert f"`{consultation_type}`" in skill
    for moment in (
        "avant d'analyser en profondeur",
        "Avant une rétro-ingénierie",
        "Avant une campagne de pivots VT",
        "Résultat contradictoire",
        "Rédaction de chaque section",
    ):
        assert moment in skill


def test_playbook_reads_the_guidance_fields_the_backend_exposes() -> None:
    playbook = _PLAYBOOK.read_text()
    for field in ("checkpoint_due", "reason", "suggested_consultation_type", "last_advice"):
        assert field in AdvisorGuidanceV1.model_fields
        assert field in playbook


@pytest.mark.parametrize("name", ["playbook", "index", *_operator_skill_names()])
def test_operator_instructions_are_model_neutral_and_generic(name: str) -> None:
    text = _operator_texts()[name]
    assert "Luna" not in text, "the operator model is chosen by the orchestrator"
    assert not re.search(r"\b[0-9a-fA-F]{64}\b", text), "no historical hash in permanent guidance"
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), "no historical address"
    assert not re.search(r"\b(?:M12-?Q|R3|Run ?12)\b", text), "no historical run result"
