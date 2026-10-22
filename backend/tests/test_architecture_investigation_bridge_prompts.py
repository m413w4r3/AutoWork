from __future__ import annotations

import ast
import inspect
import re

from cti_app.application.bridge_investigator import render_investigation_prompt
from cti_app.application.invariant_proposals import _render_prompt as render_p10_prompt
from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool

_FORBIDDEN_FORMAT_REQUEST = re.compile(r"\bjson\b|json\s+object|application/json", re.IGNORECASE)
_CONSULTATION_TYPES = (
    "plan_investigation",
    "pivot_strategy",
    "yara_draft",
    "review_text",
    "draft_text",
    "suricata_draft",
    "figure_plan",
)


def _string_literals(function: object) -> str:
    tree = ast.parse(inspect.getsource(function))
    return "\n".join(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_investigation_bridge_prompts_request_only_text_wire_formats() -> None:
    for consultation_type in _CONSULTATION_TYPES:
        prompt = InvestigationAdvisorTool._prompt(consultation_type, {})
        format_instructions = prompt.split("<UNTRUSTED_ADVISORY_INPUT>", 1)[0]
        assert "advisor-wire.v1" in format_instructions
        assert _FORBIDDEN_FORMAT_REQUEST.search(format_instructions) is None

    for prompt_builder in (render_investigation_prompt, render_p10_prompt):
        format_instructions = _string_literals(prompt_builder)
        assert _FORBIDDEN_FORMAT_REQUEST.search(format_instructions) is None
